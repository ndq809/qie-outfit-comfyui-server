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

Trên **một** instance, 4 service chạy dưới supervisor (không dùng Docker — instance là
container không chạy được Docker lồng bên trong; mọi thành phần nói chuyện qua
`127.0.0.1:port`, địa chỉ nằm trong `/workspace/.env`):

| Service supervisor | Vai trò | Lắng nghe | Ra ngoài |
|---|---|---|---|
| `redis` | job/result queue | 127.0.0.1:6379 | không |
| `minio` | object-storage | 127.0.0.1:9000 | có, qua Caddy, external port `10200` |
| `ai-server` | worker D0b→D1→D3→D2 **trong cùng process** (`d1_engine`, int8 + SageAttention) | 127.0.0.1:18090 | không |
| `data-server` | API public + `/report` | 127.0.0.1:18080 | có, qua Caddy, external port `10100` |

`comfyui` (127.0.0.1:18188) và `item_detector` (127.0.0.1:18189) **không còn cần** với cấu
hình mặc định `D1_ENGINE=fast`: ComfyUI chỉ được dùng như thư viện (mã model + thư mục
`models/`). Hai service đó chỉ cài khi muốn chạy đường cũ `D1_ENGINE=comfyui` (để so sánh
A/B), và khi đó để `autostart=false` — không bao giờ chạy ComfyUI có model nạp sẵn cạnh
ai-server nhanh trên cùng một card (cả hai đều giữ transformer ~20 GB).

**Không có database.** Job và kết quả chờ duyệt nằm trong Redis (có hạn 7 ngày), tủ đồ nằm
trên điện thoại (app lưu những món user xác nhận), bearer token khai trong `API_TOKENS` của
`/workspace/.env`. Khuôn mặt tham chiếu: app gửi **embedding** (`w600k_mbf`, 512 chiều)
kèm mỗi job; job nào không có thì dùng ảnh mặc định `face-image/selfie.jpg`
(`TEST_FIXED_FACE_REF_IMAGE`). Trang report
từng bước xử lý: `wardrobe_report_test/`, phục vụ tại `/report/`.

## Yêu cầu instance

- Image vast.ai **PyTorch** (có `/venv/main`, supervisor, Caddy, `vast-capabilities`).
  Ubuntu 24.04.
- GPU ≥ **40 GB** VRAM cho `D1_ENGINE=fast` (ai-server giữ ~33 GB: transformer int8
  12.7 GB + text encoder fp16 14.5 GB + VAE/D0b/D3). Đã chạy trên RTX 4090 48 GB. Card
  24 GB chỉ chạy được đường cũ `D1_ENGINE=comfyui`. Blackwell cần torch cu128+.
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

## Bước 2 — Gói hệ thống (redis)

```bash
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq redis-server
# apt tự tạo service; supervisor sẽ quản lý thay, nên không để nó chạy
service redis-server stop 2>/dev/null || true
which redis-server redis-cli
```

**Kỳ vọng:** in ra hai đường dẫn. Dòng `invoke-rc.d: policy-rc.d denied execution` trong
log là bình thường. Không cài postgres — server không dùng database.

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
dl lightx2v/Qwen-Image-Edit-2511-Lightning Qwen-Image-Edit-2511-Lightning-8steps-V1.0-bf16.safetensors $CM/loras
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
uv pip install fastapi "uvicorn[standard]" pydantic-settings boto3 redis \
    transformers scipy opencv-python-headless insightface timm einops
# InsightFace (so khớp khuôn mặt ở D0b) chạy qua onnxruntime: bản `onnxruntime` thường
# chỉ có CPU (đo được 2-6s/ảnh thay vì 0.2-0.4s). Bản GPU từ 1.23 trên PyPI build cho
# CUDA 13; 1.22.0 là bản CUDA 12 cuối, dùng chung cuBLAS/cuDNN mà torch đã cài.
# Driver có driver_max_cuda >= 13.0 thì bỏ ghim phiên bản cũng được.
uv pip uninstall onnxruntime 2>/dev/null || true
uv pip install "onnxruntime-gpu==1.22.0"
cd /workspace/qie-outfit-comfyui-server
python -c "
import torch; from transformers import SamModel, SamProcessor, AutoModelForObjectDetection, SiglipModel, AutoModelForImageSegmentation
import insightface, onnxruntime, cv2, boto3, redis, fastapi
import detect_clothing_by_face as b
print('imports ok, cuda =', torch.cuda.is_available())
print('face providers =', {m.session.get_providers()[0] for m in b.load_face_app().models.values()})" 2>&1 | tail -2
```

**Kỳ vọng:** `imports ok, cuda = True` và `face providers = {'CUDAExecutionProvider'}`.

D0b xử lý ảnh trên GPU (`gpu_image.py`) và phải cho ra **đúng từng bit** như code CPU gốc
(resize, chuẩn hoá, decode). Kiểm tra một lần trên máy mới (~2 phút):

```bash
cd /workspace/qie-outfit-comfyui-server && python scripts/check_gpu_image.py | tail -1
```

**Kỳ vọng:** `ALL IDENTICAL`. Nếu ra `MISMATCH` (thường do torch/transformers/torchvision
đổi phiên bản) thì điểm số D0b có thể lệch so với các ngưỡng đã hiệu chỉnh - báo lại,
đừng bỏ qua.

### Bước 5a — SageAttention 2 (build từ mã nguồn, có bản vá stream)

PyPI chỉ có bản 1.x. Bản 2 phải build (~10 phút với 1 kiến trúc), **và phải áp
`scripts/sageattention-current-stream.patch`**: bản gốc launch kernel trên default stream
bất kể stream hiện tại của torch, mà worker chạy D1 trên stream riêng — không vá thì mọi
ảnh grid ra **đen (NaN)**.

```bash
source /venv/main/bin/activate
mkdir -p /opt/src && cd /opt/src
[ -d SageAttention ] || git clone https://github.com/thu-ml/SageAttention.git
cd SageAttention
git checkout -q d1a57a546c3d       # bản vá được tạo trên commit này
git apply --check /workspace/qie-outfit-comfyui-server/scripts/sageattention-current-stream.patch 2>/dev/null \
  && git apply /workspace/qie-outfit-comfyui-server/scripts/sageattention-current-stream.patch
# 8.9 = Ada (RTX 4090/L4/RTX 6000 Ada); 8.0 = A100, 9.0 = H100
TORCH_CUDA_ARCH_LIST="8.9" EXT_PARALLEL=4 NVCC_APPEND_FLAGS="--threads 8" MAX_JOBS=16 \
  pip install --no-build-isolation .
grep -rn "<<<" csrc/qattn csrc/fused | grep -vc getCurrentCUDAStream   # phải in 0
python -c "import sageattention as s; print('sage', [n for n in dir(s) if n.startswith('sageattn')][:3])"
```

**Kỳ vọng:** dòng `grep` in `0`, dòng cuối in `sage [...]`. Thiếu SageAttention thì
`d1_engine` tự rơi về PyTorch SDPA (chậm hơn ~0.7s/ảnh, log có cảnh báo).
Nếu thấy `CPUExecutionProvider` thì onnxruntime đang là bản CPU hoặc sai bản CUDA (log
có `libcublasLt.so.13` là bản CUDA 13) — cài lại đúng như trên. **Không đổi phiên bản** `torch` (image đã có bản
khớp driver) — chỉ đổi *bản dựng CUDA* như ngay dưới đây.

### Bước 5b — Đổi torch sang bản dựng cu130 (chỉ cho `D1_ENGINE=comfyui`, nếu `driver_max_cuda` ≥ 13.0)

`d1_engine` không dùng backend `comfy_kitchen`, nên với cấu hình mặc định bỏ qua bước này.

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

Sinh mật khẩu MinIO và bearer token test ngẫu nhiên. `API_TOKENS` là danh sách token API
chấp nhận (`token:accountId`, phân cách bằng dấu phẩy) — thay cho bảng token trong database
cũ; `TEST_BEARER_TOKEN`/`TEST_ACCOUNT_ID` là cùng giá trị đó, để script test và người tích
hợp mobile đọc ra. `MINIO_ENDPOINT` **phải** là `localhost:9000` (không phải
`127.0.0.1`) — Caddy ghi đè Host header thành đúng chuỗi đó, và chữ ký presigned URL
được kiểm theo Host; sai là `403 SignatureDoesNotMatch`. Không dùng comment cuối dòng.

```bash
python3 - <<'EOF'
import os, secrets, pathlib, uuid
p = pathlib.Path("/workspace/.env")
if p.exists() and "API_TOKENS=" in p.read_text() and "MINIO_ROOT_PASSWORD=" in p.read_text():
    print("/workspace/.env đã có, giữ nguyên (xoá file nếu muốn tạo lại từ đầu)"); raise SystemExit
storage_ext = os.environ.get("STORAGE_EXT", "10200")
public_storage = f'{os.environ["PUBLIC_IPADDR"]}:{os.environ["VAST_TCP_PORT_" + storage_ext]}'
mu, mp = "wardrobe_minio", secrets.token_urlsafe(24)
token, account = secrets.token_hex(24), str(uuid.uuid4())
p.write_text(f"""# Live test config — KHÔNG commit. Template: .env.test.sample
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
API_TOKENS={token}:{account}

AI_SERVER_HEALTH_PORT=18090
D1_ENGINE=fast
D1_STEPS=8
D1_CACHE_DIR=/workspace/.cache/d1_engine
COMFYUI_URL=http://127.0.0.1:18188
ITEM_DETECTOR_URL=http://127.0.0.1:18189
OUTFIT_ITEMS_DEVICE=cuda
MODEL_IDLE_UNLOAD_SECONDS=60

WARDROBE_DEDUP_ENABLED=true
MAX_JOB_RETRIES=3

OMP_NUM_THREADS=8
MKL_NUM_THREADS=8
OPENBLAS_NUM_THREADS=8
NUMEXPR_MAX_THREADS=8
OMP_WAIT_POLICY=PASSIVE

TEST_FIXED_FACE_REF_IMAGE=/workspace/qie-outfit-comfyui-server/face-image/selfie.jpg
KEEP_RAW_IMAGES=true
WARDROBE_REPORT_DIR=/workspace/qie-outfit-comfyui-server/wardrobe_report_test
WARDROBE_REPORT_MAX_JOBS=10
WARDROBE_REPORT_PAGE_JOBS=5

TEST_ACCOUNT_ID={account}
TEST_BEARER_TOKEN={token}
""")
p.chmod(0o600)
print("wrote /workspace/.env, MINIO_PUBLIC_ENDPOINT =", public_storage)
EOF
mkdir -p /workspace/qie-outfit-comfyui-server/wardrobe_report_test
```

`COMFYUI_VRAM_ARGS` chỉ có tác dụng với `D1_ENGINE=comfyui` (trên card 48 GB đặt
`"--reserve-vram 8"`, xem `scripts/comfyui.sh`). `D1_CACHE_DIR` giữ trọng số D1 đã chuyển
đổi (~13 GB cho mỗi giá trị `D1_STEPS`, tạo ở lần chạy đầu ~25s; các lần sau đọc trong ~4s).
`D1_STEPS=8` (Lightning 8 bước) vẽ thừa món ít hơn hẳn bản 4 bước nhưng D1 mất ~4.1s/ảnh
thay vì ~2.1s - số đo ở README "8 steps by default". Đặt `4` nếu cần tốc độ hơn.

Giữ server nhẹ để SSH không bị lag/rớt khi mobile đẩy một loạt ảnh:

- `OMP_NUM_THREADS`… giới hạn thread CPU của torch/onnxruntime/OpenCV. Không có chúng,
  mỗi process ML mở một thread cho mỗi core (instance thường 64–128 core) và OpenMP
  spin-wait chiếm hết CPU, sshd không còn lượt chạy.
- `comfyui`, `item_detector`, `ai-server` chạy dưới `nice -n 10 ionice -c2 -n7`.
- Model **không** được giữ nóng trong VRAM: ai-server nạp model ở ảnh đầu tiên, và sau
  `MODEL_IDLE_UNLOAD_SECONDS` giây không có việc thì bỏ toàn bộ model D0b/D1/D3 (đường cũ
  `D1_ENGINE=comfyui`: item_detector tự bỏ model, ai-server gọi `POST /free` của ComfyUI).
  Lúc rảnh GPU chỉ còn ~1 GB CUDA context. Ảnh đầu tiên sau khi rảnh chậm hơn (~40s nạp
  lại model), các ảnh sau trong cùng lô chạy nóng bình thường. Production chạy liên tục
  nên đặt giá trị lớn (hoặc `0` = không bao giờ bỏ).
  `0` = không bao giờ bỏ; `ITEM_DETECTOR_WARMUP=true` = nạp detector lúc khởi động.
- `KEEP_RAW_IMAGES=true`: **chỉ môi trường test** — ảnh gốc mobile upload được giữ lại trong
  bucket `wardrobe-raw` sau khi xử lý (production mặc định `false`, xoá ngay theo spec
  "Bảo mật và vòng đời dữ liệu"). Để điều tra ảnh sai trên đúng ảnh gốc độ phân giải đầy
  đủ, lấy key ở `rawObjectKey` trong `wardrobe_report_test/<jobId>/<localId>/record.json`
  rồi tải về:
  ```bash
  python -c "from pathlib import Path; from server.common import storage; from server.common.config import get_settings as g; storage.download_to(g().minio_raw_bucket, '<rawObjectKey>', Path('/tmp/raw.jpg'))"
  ```
  Thêm dòng này vào `.env` của instance cũ rồi `supervisorctl restart data-server ai-server`.
- `WARDROBE_DEDUP_ENABLED=true`: bật D2 - so trùng trong cùng ảnh và giữa các ảnh của cùng
  một job (món trùng có `reviewStatus: "duplicate"`, không hiện ở danh sách duyệt, xem
  `MOBILE_CLIENT_API.md` §6.1). So với tủ đồ là việc của app (tủ đồ nằm trên máy, §6.4).
  Quy tắc so trùng: `server/common/embeddings.py` (`same_garment_score`).
- `TEST_FIXED_FACE_REF_IMAGE`: khuôn mặt dùng cho job mà app **không** gửi embedding (hay
  ảnh) khuôn mặt hợp lệ. App gửi thì cái của app thắng (`faceReference.source:
  "embedding"` trong response tạo job). Production phải để trống.
- Report chỉ lưu JPEG thu nhỏ của từng bước và trang `/report/` chỉ render khi có
  người mở (có cache), không còn rebuild sau mỗi ảnh.

Hai lưu ý bắt buộc:

- `WARDROBE_REPORT_DIR` **không** được trỏ vào `wardrobe_report_out/` — thư mục đó là
  báo cáo mẫu của người dùng, `build_index` sẽ ghi đè `index.html` của nó.
- Thư mục report phải **tồn tại trước khi** data-server khởi động, nếu không `/report`
  không được mount (chỉ ghi warning).

## Bước 7 — Chạy redis, minio

```bash
tail -2 /tmp/build_minio.log          # phải thấy MINIO_BUILD_DONE; chưa có thì chờ
/opt/minio/minio --version | head -1  # phải in "minio version ..."
cd /workspace/qie-outfit-comfyui-server
cp scripts/redis.sh scripts/minio.sh /opt/supervisor-scripts/
chmod +x /opt/supervisor-scripts/{redis,minio}.sh
cp scripts/redis.conf scripts/minio.conf /etc/supervisor/conf.d/
supervisorctl reread && supervisorctl update
sleep 8
redis-cli -h 127.0.0.1 ping
curl -s -o /dev/null -w "minio %{http_code}\n" http://127.0.0.1:9000/minio/health/live
```

**Kỳ vọng:** `PONG`, `minio 200`.

## Bước 8 — Token test (không còn database)

Không có bước tạo database hay seed account: bucket MinIO do data-server tự tạo lúc khởi
động (Bước 10), token test đã sinh ở Bước 6. Chỉ kiểm tra:

```bash
grep -c '^API_TOKENS=.\+' /workspace/.env
grep -c '^TEST_BEARER_TOKEN=.\+' /workspace/.env
```

**Kỳ vọng:** cả hai dòng in `1`. Thêm token cho account test khác:
`python -m server.data_server.scripts.add_api_token` rồi `supervisorctl restart data-server`.

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

## Bước 10 — Chạy data-server và ai-server

```bash
tail -1 /tmp/models.log        # phải là MODELS_DONE; chưa có thì chờ (kiểm tra lại mỗi phút)
ls -la /workspace/ComfyUI/models/{diffusion_models,text_encoders,vae,loras}/*.safetensors
```

**Kỳ vọng:** 6 file: fp8mixed ~20.5 GB, qwen_2.5_vl ~9.4 GB, vae ~254 MB,
Extract-Outfit ~236 MB, Lightning-8steps ~850 MB, Lightning-4steps ~850 MB.

```bash
cd /workspace/qie-outfit-comfyui-server
cp scripts/data-server.sh scripts/ai-server.sh /opt/supervisor-scripts/
chmod +x /opt/supervisor-scripts/{data-server,ai-server}.sh
cp scripts/data-server.conf scripts/ai-server.conf /etc/supervisor/conf.d/
supervisorctl reread && supervisorctl update
sleep 20
supervisorctl status redis minio data-server ai-server
curl -s http://127.0.0.1:18080/v1/health; echo
curl -s http://127.0.0.1:18090/health; echo
grep -E "fixture|report mounted|result_consumer thread" /var/log/portal/data-server.log | tail -3
grep -E "pipelined worker starting" /var/log/portal/ai-server.log | tail -1
```

**Kỳ vọng:**
- 4 service `RUNNING`.
- data-server: `{"status":"ok","dependencies":{"objectStorage":"ok","queue":"ok"}}`
- ai-server: `{"status":"ok","activeWorkers":1,...}` và log có `pipelined worker starting`.
- log data-server có đủ 3 dòng: `test face fixture uploaded`, `test extraction report mounted at /report`, `result_consumer thread started`.

Model được nạp ở ảnh đầu tiên (và lại sau mỗi `MODEL_IDLE_UNLOAD_SECONDS` rảnh). Lần đầu
tiên trên máy mới còn tải model phụ (SAM, detector, insightface, BiRefNet, SigLIP) vào
`HF_HOME`, chuyển đổi trọng số D1 vào `D1_CACHE_DIR` (~25s) và Triton tự tune kernel
(~40s) — ảnh đầu của job đầu chậm ~2 phút. Các lần nạp sau ~40s. Log ai-server khi xong:
`D1 weights loaded from cache ... D1 engine ready ... D1 engine warm-up ...`.

(Chỉ khi cần đường cũ `D1_ENGINE=comfyui`: cài thêm `comfyui` + `item_detector` như README
"Service setup", đặt `D1_ENGINE=comfyui` trong `.env`, `supervisorctl restart ai-server`.)

## Bước 11 — Kiểm thử toàn luồng như app mobile

```bash
cd /workspace/qie-outfit-comfyui-server && source /venv/main/bin/activate
python scripts/mobile_flow_test.py --storage-host localhost:${STORAGE_EXT:-10200} --poll 15 test-images
```

`--storage-host` là **bắt buộc khi chạy script trên chính server**: PUT từ trong
container lên IP public của chính nó đi vòng qua NAT (hairpin) chỉ ~5 KB/s và timeout với
ảnh 6 MB. Cờ này gửi cùng URL đã ký + cùng cookie tới cùng Caddy edge qua địa chỉ nội bộ.
Client thật ở ngoài máy không cần.

**Kỳ vọng** (10 ảnh: ~4-5s/ảnh sau khi model đã nạp, tức ~45s; cộng ~40s-2 phút nạp
model nếu ai-server đang nguội):
- `upload 10/10 ok`, `job {... 'faceReference': {'source': 'embedding'}}` — script gửi
  embedding của `face-image/selfie.jpg` kèm job như app thật (`--face <file>` để đổi ảnh;
  `--face-mode image` gửi ảnh khuôn mặt → `'image'`; `--face-mode none` → `'test-fixture'`)
- `completed processed 10/10 failed 0`
- `review N garment(s) waiting for the user` với N > 0, `review imageUrl GET 200`
- `reject {'rejected': [<1 id>], ...}`,
  `confirm N-1 returned, errors []; first: tagsEdited True, embeddings 768/768`,
  `confirm again 1 returned (idempotent, same tags: True)`, `imageUrl GET 200`
- `job review {'pending': 0, 'confirmed': N-1, 'rejected': 1, 'duplicate': D}` — D là số
  món trùng giữa các ảnh, bị ẩn khỏi danh sách duyệt (đo được: N = 16, D = 4)

Kiểm thử huỷ job (spec §2.3.6 bước 8):

```bash
python - <<'EOF'
import os, sys, time
sys.path.insert(0, "scripts"); import mobile_flow_test as m
from pathlib import Path
c = m.Client(f"http://{os.environ['PUBLIC_IPADDR']}:{os.environ['VAST_TCP_PORT_' + os.environ.get('DATA_EXT', '10100')]}",
             os.environ["OPEN_BUTTON_TOKEN"], m.env_file("TEST_BEARER_TOKEN"), f"{os.environ['VAST_CONTAINERLABEL']}_auth_token")
c.storage_host = "localhost:" + os.environ.get("STORAGE_EXT", "10200")
files = sorted(Path("test-images").iterdir())[5:8]
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

**Kỳ vọng:** `cancel -> {'status': 'cancelling'}`, rồi cuối cùng `cancelled N M` với
N + M = 3 và M ≥ 1: ảnh đang ở giữa pipeline (đã qua bước kiểm tra huỷ trước D1) vẫn chạy
xong, phần còn lại báo `failed`/"cancelled". Ảnh xử lý nhanh (~4-5s/ảnh), nên gửi lệnh huỷ
ngay sau khi tạo job; nếu job đã `completed` trước khi lệnh huỷ tới, tăng số ảnh lên.

Dọn job huỷ khỏi report:

```bash
JID=<giá trị in ra ở dòng CANCEL_JOB>
set -a; . /workspace/.env; set +a
rm -rf "/workspace/qie-outfit-comfyui-server/wardrobe_report_test/$JID"
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
| Tạo job trả `faceReference.source: null` | App không gửi khuôn mặt **và** `TEST_FIXED_FACE_REF_IMAGE` sai đường dẫn, hoặc MinIO chưa chạy khi data-server khởi động | Kiểm tra file tồn tại, `supervisorctl restart data-server`, xem `/var/log/portal/data-server.log` |
| App gửi embedding nhưng job báo `source: "test-fixture"` kèm `faceReference.warning` | Embedding bị từ chối: sai `model` (phải `w600k_mbf`), không đủ 512 số, toàn 0, NaN | Đọc `warning` trong response (cũng có trong log data-server: `job face embedding rejected`), sửa phía app |
| Ảnh nhóm tách nhầm người dù gửi embedding | Embedding của app lệch embedding server (căn chỉnh ML Kit vs SCRFD), độ giống mặt xuống dưới 0.35 | Xem "độ giống khuôn mặt" trong report; đo theo cuối mục 3 `MOBILE_CLIENT_API.md` |
| App gửi ảnh khuôn mặt nhưng job báo `source: "test-fixture"` | PUT ảnh khuôn mặt hỏng/chưa xong khi gọi `POST /v1/jobs` (server chốt khuôn mặt lúc tạo job) | PUT `faceReference.uploadUrl` trả 200 rồi mới tạo job |
| `401 {"detail":"invalid token"}` | Token không có trong `API_TOKENS` của `.env`, hoặc sửa `.env` mà chưa restart | Thêm token (`add_api_token`), `supervisorctl restart data-server` |
| Bước D3 báo file `.pt` là LFS pointer | Clone không có LFS | `git lfs install && git lfs pull`, restart ai-server |
| Mọi ảnh grid trong report **đen**, `0 garment(s)` cho mọi ảnh | SageAttention build **không** có `scripts/sageattention-current-stream.patch` (kernel chạy sai stream, ra NaN) | Làm lại Bước 5a (áp patch, build lại), `supervisorctl restart ai-server` |
| Log ai-server: `sageattention not installed - D1 falls back to PyTorch SDPA` | Chưa làm Bước 5a | Vẫn chạy đúng, chỉ chậm hơn ~0.7s/ảnh; làm Bước 5a để nhanh lại |
| ai-server `CUDA out of memory` ở ảnh đầu | GPU < 40 GB, hoặc ComfyUI đang chạy với model nạp sẵn trên cùng card | `supervisorctl stop comfyui item_detector`; card 24 GB thì đặt `D1_ENGINE=comfyui` |
| Job đứng ở `processing`, log ai-server có `ComfyUI rejected workflow` | Thiếu/sai tên file model | So tên file ở Bước 10 với bảng "Models required" trong README |
| CUDA OOM ở ai-server hoặc ComfyUI | GPU < 24 GB dùng chung cho detector và ComfyUI | Đặt `OUTFIT_ITEMS_DEVICE=cpu` trong `.env`, restart `item_detector ai-server` (D0b chậm hơn ~4 lần) |
| Ảnh đen / NaN | Có ai thêm `--use-sage-attention` vào ComfyUI | Bỏ cờ đó khỏi `scripts/comfyui.sh` |
| Wardrobe ít item hơn số crop trong report | D2 loại món trùng trong cùng ảnh, hoặc ẩn món đã có ở ảnh khác của cùng job (`review.duplicate` của job) — chỉ khi `WARDROBE_DEDUP_ENABLED=true` | Không phải lỗi — món trùng nằm ở `duplicates` của món đại diện trong `GET /v1/jobs/{jobId}/garments` |
| D0b chậm (vài giây), "so khớp khuôn mặt" chiếm 2-6s trong report | InsightFace chạy trên CPU (`onnxruntime` bản CPU hoặc bản CUDA 13) | Làm lại phần onnxruntime ở Bước 5, `supervisorctl restart item_detector` |
| SSH lag/rớt kết nối khi đang chạy job | Thread CPU không giới hạn, hoặc service ML chạy cùng độ ưu tiên với sshd | Kiểm tra `.env` có `OMP_NUM_THREADS`/`OMP_WAIT_POLICY` và script dùng `nice`; `supervisorctl restart comfyui item_detector ai-server` |
| Log service | — | `tail -f /var/log/portal/<service>.log` |

## Thay đổi code/cấu hình về sau

- Sửa code server: `supervisorctl restart ai-server data-server` (job đang chạy dở sẽ
  mất vé việc đang xử lý — chỉ restart khi queue rỗng: `redis-cli llen job_queue` = 0 và
  không có job `processing`).
- Tạo thêm account test: `python -m server.data_server.scripts.add_api_token` (thêm vào
  `API_TOKENS` trong `/workspace/.env`), rồi `supervisorctl restart data-server`.
