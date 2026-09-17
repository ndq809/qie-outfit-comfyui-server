# Runbook: dựng server test Wardrobe trên một instance vast.ai mới

> **Dành cho AI agent.** Làm **đúng thứ tự** từ Bước 0 đến Bước 12. Mỗi khối lệnh đều
> idempotent (chạy lại không hỏng). Sau mỗi bước có mục **Kỳ vọng** — nếu kết quả khác,
> xem [Xử lý sự cố](#xử-lý-sự-cố) trước khi tự suy luận cách khác. Không hỏi lại người dùng
> trừ khi bị chặn bởi thứ chỉ người dùng cung cấp được (token GitHub).
>
> Kiến trúc và lý do thiết kế: [wardrobe-system-spec.md](wardrobe-system-spec.md) §2.3.
> API cho client: [MOBILE_CLIENT_API.md](MOBILE_CLIENT_API.md). Runbook này **không** giải
> thích lại những thứ đó, chỉ cài đặt.

## Kết quả cuối cùng

Trên **một** instance, 7 service chạy dưới supervisor (không dùng Docker — instance là
container không chạy được Docker lồng bên trong; mọi thành phần nói chuyện qua
`127.0.0.1:port`, địa chỉ nằm trong `/workspace/.env`):

| Service supervisor | Vai trò | Lắng nghe | Ra ngoài |
|---|---|---|---|
| `postgres` | database | 127.0.0.1:5432 | không |
| `redis` | job/result queue | 127.0.0.1:6379 | không |
| `minio` | object-storage | 127.0.0.1:9000 | có, qua Caddy, external port `10200` |
| `comfyui` | D1 sinh ảnh | 127.0.0.1:18188 | không |
| `item_detector` | D0b detector giữ nóng | 127.0.0.1:18189 | không |
| `ai-server` | worker D0b→D1→D3→D2 | 127.0.0.1:18090 | không |
| `data-server` | API public + `/report` | 127.0.0.1:18080 | có, qua Caddy, external port `10100` |

Ảnh khuôn mặt tham chiếu cố định: `face-image/selfie.jpg`. Trang report từng bước xử lý:
`wardrobe_report_test/`, phục vụ tại `/report/`.

## Yêu cầu instance

- Image vast.ai **PyTorch** (có `/venv/main`, supervisor, Caddy, `vast-capabilities`).
  Ubuntu 24.04 (có sẵn gói `postgresql-16` qua apt).
- GPU ≥ 24 GB VRAM (đã chạy trên RTX 3090 24 GB, RTX 4090, L4). Blackwell cần torch cu128+.
- Disk trống ≥ **60 GB** (model ~31 GB + pip + MinIO build).
- **Hai port external kiểu thường (không self-mapped) còn trống**, tốt nhất đúng là
  `10100` và `10200` (khai báo lúc tạo instance). Kiểm tra ở Bước 1.
- Token GitHub có quyền đọc repo `ndq809/qie-outfit-comfyui-server` (người dùng cung cấp).

---

## Bước 0 — Clone repo

```bash
cd /workspace
# GH_TOKEN do người dùng cung cấp. Không ghi token vào file nào.
git clone "https://ndq809:${GH_TOKEN}@github.com/ndq809/qie-outfit-comfyui-server.git"
cd /workspace/qie-outfit-comfyui-server
# Không để token nằm plaintext trong .git/config
git remote set-url origin https://github.com/ndq809/qie-outfit-comfyui-server.git
git lfs install && git lfs pull
head -c 4 models/magic_eye/phase2.pt | od -c | head -1
```

**Kỳ vọng:** dòng cuối bắt đầu bằng `P K` (file zip của torch). Nếu thấy `v e r s`
thì đó là con trỏ LFS → chạy lại `git lfs pull`.

## Bước 1 — Kiểm tra instance và chốt port

```bash
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader
df -h / | tail -1
vast-capabilities | jq -c '.instance.open_ports[] | {container_port, public_port, in_use, self_mapped}'
env | grep -E '^(PUBLIC_IPADDR|VAST_CONTAINERLABEL|VAST_TCP_PORT_10100|VAST_TCP_PORT_10200)='
```

**Kỳ vọng:** GPU ≥ 24 GB, disk trống ≥ 60 GB, có hai dòng `container_port` 10100 và
10200 với `in_use:false`, và cả bốn biến môi trường đều có giá trị.

Nếu **không có** 10100/10200: chọn hai port khác có `in_use:false` và **không**
`self_mapped` (port 8080 thường đã bị Jupyter dùng — đừng chọn). Từ đây trở đi thay
`10100` → port thứ nhất, `10200` → port thứ hai **ở mọi chỗ trong runbook**.

```bash
export DATA_EXT=10100 STORAGE_EXT=10200     # đổi nếu phải chọn port khác
```

## Bước 2 — Gói hệ thống (postgres, redis)

```bash
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq postgresql-16 postgresql-client-16 redis-server
# apt tự tạo service systemd; supervisor sẽ quản lý thay, nên không để chúng chạy
service postgresql stop 2>/dev/null || true
service redis-server stop 2>/dev/null || true
ls /usr/lib/postgresql/16/bin/postgres /etc/postgresql/16/main/postgresql.conf
```

**Kỳ vọng:** hai đường dẫn cuối tồn tại. Dòng `invoke-rc.d: policy-rc.d denied execution`
trong log là bình thường.

## Bước 3 — MinIO: build từ mã nguồn (chạy nền, ~3 phút)

**Không** tải từ `dl.min.io`: MinIO community đã bị archive, mọi bản phát hành trả về
`410 Gone` (file tải về là một đoạn text 405 byte, không phải binary). GitHub release
cũng không còn asset. Phải build:

```bash
mkdir -p /opt/minio /opt/src
nohup bash -c '
set -e
curl -sSL https://go.dev/dl/go1.25.4.linux-amd64.tar.gz | tar -C /usr/local -xz
[ -d /opt/src/minio ] || git clone --depth 1 --branch RELEASE.2025-10-15T17-29-55Z \
    https://github.com/minio/minio.git /opt/src/minio
cd /opt/src/minio
CGO_ENABLED=0 GOPATH=/opt/gopath /usr/local/go/bin/go build -trimpath -ldflags "-s -w" -o /opt/minio/minio .
echo MINIO_BUILD_DONE
' > /tmp/build_minio.log 2>&1 &
```

Không chờ — làm tiếp Bước 4. Kiểm tra ở Bước 7.

## Bước 4 — ComfyUI + model (chạy nền, ~31 GB)

```bash
[ -d /workspace/ComfyUI ] || git clone --depth 1 https://github.com/comfyanonymous/ComfyUI.git /workspace/ComfyUI
nohup bash -c '
set -e
source /venv/main/bin/activate
export HF_HOME=/workspace/.hf_home
CM=/workspace/ComfyUI/models
mkdir -p $CM/diffusion_models $CM/text_encoders $CM/vae $CM/loras
dl () { base=$(basename "$2"); [ -s "$3/$base" ] && { echo "have $base"; return; }
        hf download "$1" "$2" --local-dir /workspace/.hfdl && mv "/workspace/.hfdl/$2" "$3/$base"; }
dl Comfy-Org/Qwen-Image-Edit_ComfyUI split_files/diffusion_models/qwen_image_edit_2511_fp8mixed.safetensors $CM/diffusion_models
dl Comfy-Org/Qwen-Image_ComfyUI split_files/text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors $CM/text_encoders
dl Comfy-Org/Qwen-Image_ComfyUI split_files/vae/qwen_image_vae.safetensors $CM/vae
dl prithivMLmods/QIE-2511-Extract-Outfit QIE-2511-Extract-Outfit-4200.safetensors $CM/loras
dl lightx2v/Qwen-Image-Edit-2511-Lightning Qwen-Image-Edit-2511-Lightning-4steps-V1.0-bf16.safetensors $CM/loras
rm -rf /workspace/.hfdl
echo MODELS_DONE
' > /tmp/models.log 2>&1 &
```

Không chờ — làm tiếp Bước 5. Kiểm tra ở Bước 9.

## Bước 5 — Python dependencies

```bash
source /venv/main/bin/activate
cd /workspace/ComfyUI && uv pip install -r requirements.txt
uv pip install fastapi "uvicorn[standard]" pydantic-settings boto3 redis psycopg2-binary \
    transformers scipy opencv-python-headless insightface onnxruntime timm einops
cd /workspace/qie-outfit-comfyui-server
python -c "
import torch; from transformers import SamModel, SamProcessor, AutoModelForObjectDetection, SiglipModel, AutoModelForImageSegmentation
import insightface, onnxruntime, cv2, boto3, redis, psycopg2, fastapi
print('imports ok, cuda =', torch.cuda.is_available())"
```

**Kỳ vọng:** `imports ok, cuda = True`. **Không đổi phiên bản** `torch` (image đã có bản
khớp driver) — chỉ đổi *bản dựng CUDA* như ngay dưới đây.

### Bước 5b — Đổi torch sang bản dựng cu130 (nếu `driver_max_cuda` ≥ 13.0)

ComfyUI chỉ bật backend CUDA tối ưu của `comfy_kitchen` khi torch được dựng với cu130
trở lên; với cu128 nó ghi `WARNING: You need pytorch with cu130 or higher to use
optimized CUDA operations` rồi rơi về backend `eager` chậm hơn. Đây **không** phải nâng
cấp torch: giữ nguyên số phiên bản, chỉ đổi bản dựng CUDA, nên không kéo theo thay đổi
API nào.

```bash
nvidia-smi --query-gpu=driver_version --format=csv,noheader   # cần driver hỗ trợ CUDA >= 13.0
source /venv/main/bin/activate
python -c "import torch,torchvision;print(torch.__version__,torchvision.__version__)"  # đọc phiên bản đang có
# Thay <T>/<V>/<A>/<C> bằng đúng các phiên bản vừa in ra — KHÔNG nâng số phiên bản.
uv pip install --index-url https://download.pytorch.org/whl/cu130 \
    --reinstall-package torch --reinstall-package torchvision \
    --reinstall-package torchaudio --reinstall-package torchcodec \
    "torch==<T>+cu130" "torchvision==<V>+cu130" "torchaudio==<A>+cu130" "torchcodec==<C>+cu130"
supervisorctl restart comfyui item_detector ai-server data-server
grep -o "backend cuda: {'available': [A-Za-z]*, 'disabled': [A-Za-z]*" /var/log/portal/comfyui.log | tail -1
```

**Kỳ vọng:** `backend cuda: {'available': True, 'disabled': False` và log không còn dòng
`You need pytorch with cu130`. Đo trên RTX 6000 Ada: D1 giảm từ ~9.3s xuống ~8.6s mỗi
ảnh (-7%), cả job 10 ảnh từ 165s xuống 150s. Ảnh grid sinh ra **không** trùng từng pixel
với bản cu128 (kernel khác thì quỹ đạo khuếch tán lệch nhẹ) nhưng cùng số món, cùng bố
cục, và D3a/D3b cho ra cùng crop + cùng phân loại.

Nếu instance dùng driver cũ hơn (`driver_max_cuda` < 13.0) thì bỏ qua bước này — giữ
nguyên bản dựng của image.

## Bước 6 — Tạo `/workspace/.env`

Sinh mật khẩu ngẫu nhiên. `MINIO_ENDPOINT` **phải** là `localhost:9000` (không phải
`127.0.0.1`) — Caddy ghi đè Host header thành đúng chuỗi đó, và chữ ký presigned URL
được kiểm theo Host; sai là `403 SignatureDoesNotMatch`. Không dùng comment cuối dòng.

```bash
python3 - <<'EOF'
import os, secrets, pathlib
p = pathlib.Path("/workspace/.env")
if p.exists() and "POSTGRES_PASSWORD=" in p.read_text() and "MINIO_ROOT_PASSWORD=" in p.read_text():
    print("/workspace/.env đã có, giữ nguyên (xoá file nếu muốn tạo lại từ đầu)"); raise SystemExit
storage_ext = os.environ.get("STORAGE_EXT", "10200")
public_storage = f'{os.environ["PUBLIC_IPADDR"]}:{os.environ["VAST_TCP_PORT_" + storage_ext]}'
pg, mu, mp = secrets.token_urlsafe(24), "wardrobe_minio", secrets.token_urlsafe(24)
p.write_text(f"""# Live test config — KHÔNG commit. Template: .env.test.sample
POSTGRES_HOST=127.0.0.1
POSTGRES_PORT=5432
POSTGRES_DB=wardrobe
POSTGRES_USER=wardrobe_app
POSTGRES_PASSWORD={pg}

MINIO_ROOT_USER={mu}
MINIO_ROOT_PASSWORD={mp}
MINIO_ENDPOINT=localhost:9000
MINIO_PUBLIC_ENDPOINT={public_storage}
MINIO_ACCESS_KEY={mu}
MINIO_SECRET_KEY={mp}
MINIO_RAW_BUCKET=wardrobe-raw
MINIO_ITEMS_BUCKET=wardrobe-items

REDIS_HOST=127.0.0.1
REDIS_PORT=6379

DATA_SERVER_HOST=127.0.0.1
DATA_SERVER_PORT=18080

AI_SERVER_HEALTH_PORT=18090
COMFYUI_URL=http://127.0.0.1:18188
ITEM_DETECTOR_URL=http://127.0.0.1:18189
OUTFIT_ITEMS_DEVICE=cuda

WARDROBE_DEDUP_THRESHOLD=0.90
MAX_JOB_RETRIES=3

TEST_FIXED_FACE_REF_IMAGE=/workspace/qie-outfit-comfyui-server/face-image/selfie.jpg
WARDROBE_REPORT_DIR=/workspace/qie-outfit-comfyui-server/wardrobe_report_test

TEST_ACCOUNT_ID=
TEST_USER_ID=
TEST_BEARER_TOKEN=
""")
p.chmod(0o600)
print("wrote /workspace/.env, MINIO_PUBLIC_ENDPOINT =", public_storage)
EOF
mkdir -p /workspace/qie-outfit-comfyui-server/wardrobe_report_test
```

Hai lưu ý bắt buộc:

- `WARDROBE_REPORT_DIR` **không** được trỏ vào `wardrobe_report_out/` — thư mục đó là
  báo cáo mẫu của người dùng, `build_index` sẽ ghi đè `index.html` của nó.
- Thư mục report phải **tồn tại trước khi** data-server khởi động, nếu không `/report`
  không được mount (chỉ ghi warning).

## Bước 7 — Chạy postgres, redis, minio

```bash
tail -2 /tmp/build_minio.log          # phải thấy MINIO_BUILD_DONE; chưa có thì chờ
/opt/minio/minio --version | head -1  # phải in "minio version ..."
cd /workspace/qie-outfit-comfyui-server
cp scripts/postgres.sh scripts/redis.sh scripts/minio.sh /opt/supervisor-scripts/
chmod +x /opt/supervisor-scripts/{postgres,redis,minio}.sh
cp scripts/postgres.conf scripts/redis.conf scripts/minio.conf /etc/supervisor/conf.d/
supervisorctl reread && supervisorctl update
sleep 8
pg_isready -h 127.0.0.1 -p 5432
redis-cli -h 127.0.0.1 ping
curl -s -o /dev/null -w "minio %{http_code}\n" http://127.0.0.1:9000/minio/health/live
```

**Kỳ vọng:** `accepting connections`, `PONG`, `minio 200`.

## Bước 8 — Database, bucket, account test

```bash
cd /workspace/qie-outfit-comfyui-server
set -a; . /workspace/.env; set +a
su -s /bin/bash postgres -c "psql -tAc \"SELECT 1 FROM pg_roles WHERE rolname='$POSTGRES_USER'\"" | grep -q 1 \
  || su -s /bin/bash postgres -c "psql -c \"CREATE ROLE $POSTGRES_USER LOGIN PASSWORD '$POSTGRES_PASSWORD'\""
su -s /bin/bash postgres -c "psql -tAc \"SELECT 1 FROM pg_database WHERE datname='$POSTGRES_DB'\"" | grep -q 1 \
  || su -s /bin/bash postgres -c "createdb -O $POSTGRES_USER $POSTGRES_DB"
source /venv/main/bin/activate
python -m server.data_server.scripts.init_db

# Seed account test đúng MỘT lần (chạy lại sẽ tạo thêm account)
if ! grep -q '^TEST_BEARER_TOKEN=.\+' /workspace/.env; then
  python -m server.data_server.scripts.seed_test_account "mobile-test" | tee /tmp/seed.txt
  python3 - <<'EOF'
import re, pathlib
seed = dict(re.findall(r"^(account_id|user_id|token):\s+(\S+)", pathlib.Path("/tmp/seed.txt").read_text(), re.M))
p = pathlib.Path("/workspace/.env"); t = p.read_text()
for k, v in (("TEST_ACCOUNT_ID", seed["account_id"]), ("TEST_USER_ID", seed["user_id"]), ("TEST_BEARER_TOKEN", seed["token"])):
    t = re.sub(rf"^{k}=.*$", f"{k}={v}", t, flags=re.M)
p.write_text(t); print("saved test identity to /workspace/.env")
EOF
  rm -f /tmp/seed.txt
fi
grep -c '^TEST_BEARER_TOKEN=.\+' /workspace/.env
```

**Kỳ vọng:** `schema applied ...`, `buckets ready: wardrobe-raw, wardrobe-items`, dòng
cuối là `1`.

## Bước 9 — Đưa data-server và object-storage ra ngoài qua Caddy

```bash
source /venv/main/bin/activate
python - <<'EOF'
import os, yaml
p = "/etc/portal.yaml"; d = yaml.safe_load(open(p)) or {"applications": {}}
d["applications"]["Data Server"] = {"hostname": "localhost", "external_port": int(os.environ.get("DATA_EXT", 10100)),
    "internal_port": 18080, "open_path": "/v1/health", "name": "Data Server"}
d["applications"]["Object Storage"] = {"hostname": "localhost", "external_port": int(os.environ.get("STORAGE_EXT", 10200)),
    "internal_port": 9000, "open_path": "/minio/health/live", "name": "Object Storage"}
yaml.safe_dump(d, open(p, "w"), sort_keys=False); print("portal.yaml updated")
EOF
supervisorctl restart caddy
```

**Kỳ vọng:** `caddy: started`. Không stop `caddy`, `instance_portal`, `tunnel_manager`.

## Bước 10 — Chạy data-server, ComfyUI, item_detector, ai-server

```bash
tail -1 /tmp/models.log        # phải là MODELS_DONE; chưa có thì chờ (kiểm tra lại mỗi phút)
ls -la /workspace/ComfyUI/models/{diffusion_models,text_encoders,vae,loras}/*.safetensors
```

**Kỳ vọng:** 5 file: fp8mixed ~20.5 GB, qwen_2.5_vl ~9.4 GB, vae ~254 MB,
Extract-Outfit ~236 MB, Lightning-4steps ~850 MB.

```bash
cd /workspace/qie-outfit-comfyui-server
cp scripts/data-server.sh scripts/comfyui.sh scripts/item_detector.sh scripts/ai-server.sh /opt/supervisor-scripts/
chmod +x /opt/supervisor-scripts/{data-server,comfyui,item_detector,ai-server}.sh
cp scripts/data-server.conf scripts/comfyui.conf scripts/item_detector.conf scripts/ai-server.conf /etc/supervisor/conf.d/
supervisorctl reread && supervisorctl update
sleep 60
supervisorctl status postgres redis minio data-server comfyui item_detector ai-server
curl -s http://127.0.0.1:18080/v1/health; echo
curl -s http://127.0.0.1:18188/system_stats | head -c 80; echo
curl -s http://127.0.0.1:18189/health; echo
curl -s http://127.0.0.1:18090/health; echo
grep -E "fixture|report mounted|result_consumer thread" /var/log/portal/data-server.log | tail -3
```

**Kỳ vọng:**
- 7 service `RUNNING`.
- data-server: `{"status":"ok","dependencies":{"postgres":"ok","objectStorage":"ok","queue":"ok"}}`
- ComfyUI: bắt đầu bằng `{"system": {`
- item_detector: `{"status": "ok", "device": "cuda"}`
- ai-server: `{"status":"ok","activeWorkers":1,...}`
- log data-server có đủ 3 dòng: `test face fixture uploaded`, `test extraction report mounted at /report`, `result_consumer thread started`.

Lần đầu item_detector/ai-server tự tải model phụ (SAM, detector, insightface, BiRefNet,
SigLIP) vào `HF_HOME` — job đầu tiên sẽ chậm hơn vài chục giây, bình thường.

## Bước 11 — Kiểm thử toàn luồng như app mobile

```bash
cd /workspace/qie-outfit-comfyui-server && source /venv/main/bin/activate
python scripts/mobile_flow_test.py --storage-host localhost:${STORAGE_EXT:-10200} --poll 15 test-images
```

`--storage-host` là **bắt buộc khi chạy script trên chính server**: PUT từ trong
container lên IP public của chính nó đi vòng qua NAT (hairpin) chỉ ~5 KB/s và timeout với
ảnh 6 MB. Cờ này gửi cùng URL đã ký + cùng cookie tới cùng Caddy edge qua địa chỉ nội bộ.
Client thật ở ngoài máy không cần.

**Kỳ vọng** (9 ảnh, ~3.5 phút trên RTX 3090):
- `face-reference {'registered': True, ..., 'source': 'test-fixture'}`
- `upload 9/9 ok`
- `completed processed 9/9 failed 0`
- `wardrobe N item(s)` với N > 0 (lần đo gần nhất: 17), và `imageUrl GET 200`

Kiểm thử huỷ job (spec §2.3.6 bước 8):

```bash
python - <<'EOF'
import os, sys, time
sys.path.insert(0, "scripts"); import mobile_flow_test as m
from pathlib import Path
c = m.Client(f"http://{os.environ['PUBLIC_IPADDR']}:{os.environ['VAST_TCP_PORT_' + os.environ.get('DATA_EXT', '10100')]}",
             os.environ["OPEN_BUTTON_TOKEN"], m.env_file("TEST_BEARER_TOKEN"), f"{os.environ['VAST_CONTAINERLABEL']}_auth_token")
c.storage_host = "localhost:" + os.environ.get("STORAGE_EXT", "10200")
files = sorted(Path("test-images").iterdir())[:3]
pre = c.call("POST", "/v1/uploads/presign", {"items": [{"localId": "cancel-" + m.local_id(f), "contentType": "image/jpeg"} for f in files]})
for it, f in zip(pre["items"], files): c.put(it["uploadUrl"], f, "image/jpeg")
jid = c.call("POST", "/v1/jobs", {"batchId": pre["batchId"], "uploadedItems": [i["localId"] for i in pre["items"]]})["jobId"]
time.sleep(6); print("cancel ->", c.call("POST", f"/v1/jobs/{jid}/cancel"))
for _ in range(20):
    st = c.call("GET", f"/v1/jobs/{jid}"); print(st["status"], st["processedItems"], st["failedItems"])
    if st["status"] in ("cancelled", "completed", "failed"): break
    time.sleep(8)
print("CANCEL_JOB", jid)
EOF
```

**Kỳ vọng:** `cancel -> {'status': 'cancelling'}`, rồi cuối cùng `cancelled 1 2`.

Dọn job huỷ khỏi report và tủ đồ, để report chỉ còn lượt chạy 9 ảnh:

```bash
JID=<giá trị in ra ở dòng CANCEL_JOB>
set -a; . /workspace/.env; set +a
rm -rf "/workspace/qie-outfit-comfyui-server/wardrobe_report_test/$JID"
PGPASSWORD="$POSTGRES_PASSWORD" psql -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "DELETE FROM wardrobe_items WHERE job_id='$JID';"
python -c "from server.common import report; report.build_index('$WARDROBE_REPORT_DIR')"
```

## Bước 12 — Cập nhật tài liệu client và bàn giao

IP, port public và tên cookie **đổi theo từng instance**. Cập nhật
`MOBILE_CLIENT_API.md` bằng cách đọc giá trị cũ ngay trong file (không đoán):

```bash
cd /workspace/qie-outfit-comfyui-server
python3 - <<'EOF'
import os, re, pathlib
p = pathlib.Path("MOBILE_CLIENT_API.md"); t = p.read_text()
old_base = re.search(r"\| Test \(máy vast\.ai hiện tại\) \| `http://([\d.]+:\d+)`", t).group(1)
old_storage = re.search(r'"uploadUrl": "http://([\d.]+:\d+)/', t).group(1)
old_label = re.search(r"(C\.\d+)_auth_token", t).group(1)
ip = os.environ["PUBLIC_IPADDR"]
new_base = f'{ip}:{os.environ["VAST_TCP_PORT_" + os.environ.get("DATA_EXT", "10100")]}'
new_storage = f'{ip}:{os.environ["VAST_TCP_PORT_" + os.environ.get("STORAGE_EXT", "10200")]}'
t = t.replace(old_base, new_base).replace(old_storage, new_storage)
t = t.replace(f"`{old_base.split(':')[1]}`", f"`{new_base.split(':')[1]}`")
t = t.replace(old_label + "_auth_token", os.environ["VAST_CONTAINERLABEL"] + "_auth_token")
p.write_text(t)
print(f"base {old_base} -> {new_base}\nstorage {old_storage} -> {new_storage}\ncookie {old_label} -> {os.environ['VAST_CONTAINERLABEL']}")
EOF
echo "Report: http://$PUBLIC_IPADDR:$VAST_TCP_PORT_${DATA_EXT:-10100}/report/?token=<OPEN_BUTTON_TOKEN>"
curl -s -o /dev/null -w "report qua edge: %{http_code}\n" \
  "http://$PUBLIC_IPADDR:$VAST_TCP_PORT_${DATA_EXT:-10100}/report/?token=$OPEN_BUTTON_TOKEN"
```

**Kỳ vọng:** `report qua edge: 200`.

Bàn giao cho người dùng: base URL, link report (nói họ tự thêm `OPEN_BUTTON_TOKEN`,
**không in token ra**), số ảnh / số trang phục của Bước 11, và nhắc rằng
`vast-capabilities | jq .instance.workspace_is_volume` là `false` thì mọi thứ (kể cả
`/workspace/.env`) mất khi recycle/destroy. **Không commit** `/workspace/.env`,
`wardrobe_report_test/` hay token nào.

---

## Xử lý sự cố

| Triệu chứng | Nguyên nhân | Cách xử lý |
|---|---|---|
| `/opt/minio/minio: line 1: 410: command not found` | Tải MinIO từ `dl.min.io` (đã ngừng phát hành) | Xoá file, làm Bước 3 (build từ source) |
| PUT presigned `403 SignatureDoesNotMatch` | `MINIO_ENDPOINT` không phải `localhost:9000`; hoặc thêm `?token=` vào uploadUrl; hoặc `Content-Type` khác lúc presign | Sửa `.env` rồi `supervisorctl restart data-server ai-server`; edge token đi bằng Cookie `<VAST_CONTAINERLABEL>_auth_token` |
| PUT file lớn treo/timeout, file vài byte thì 200 | Gọi IP public từ bên trong chính máy đó (NAT hairpin ~5 KB/s) | Dùng `--storage-host localhost:10200` khi test trên server |
| `/report/` trả 404 | Thư mục `WARDROBE_REPORT_DIR` chưa tồn tại lúc data-server khởi động | `mkdir -p` thư mục rồi `supervisorctl restart data-server` |
| `/v1/face-reference` trả `source: null` | `TEST_FIXED_FACE_REF_IMAGE` sai đường dẫn hoặc MinIO chưa chạy khi data-server khởi động | Kiểm tra file tồn tại, `supervisorctl restart data-server`, xem `/var/log/portal/data-server.log` |
| Bước D3 báo file `.pt` là LFS pointer | Clone không có LFS | `git lfs install && git lfs pull`, restart ai-server |
| Job đứng ở `processing`, log ai-server có `ComfyUI rejected workflow` | Thiếu/sai tên file model | So tên file ở Bước 10 với bảng "Models required" trong README |
| CUDA OOM ở ai-server hoặc ComfyUI | GPU < 24 GB dùng chung cho detector và ComfyUI | Đặt `OUTFIT_ITEMS_DEVICE=cpu` trong `.env`, restart `item_detector ai-server` (D0b chậm hơn ~4 lần) |
| Ảnh đen / NaN | Có ai thêm `--use-sage-attention` vào ComfyUI | Bỏ cờ đó khỏi `scripts/comfyui.sh` |
| Wardrobe ít item hơn số crop trong report | D2 loại món trùng với đồ đã có trong tủ (ngưỡng 0.90) | Không phải lỗi — report ghi rõ "trùng đồ đã có trong tủ (sim …)" |
| Log service | — | `tail -f /var/log/portal/<service>.log` |

## Thay đổi code/cấu hình về sau

- Sửa code server: `supervisorctl restart ai-server data-server` (job đang chạy dở sẽ
  mất vé việc đang xử lý — chỉ restart khi queue rỗng: `redis-cli llen job_queue` = 0 và
  không có job `processing`).
- Tạo thêm account test: `python -m server.data_server.scripts.seed_test_account "<tên>"`
  (sau khi `set -a; . /workspace/.env; set +a`).
