# Hướng dẫn tích hợp Mobile ↔ Wardrobe Server

Tài liệu dành cho app Mobile (Flutter/Android/iOS) kết nối tới **data-server** của hệ
thống Wardrobe. Các endpoint dựa trên [`wardrobe-system-spec.md`](wardrobe-system-spec.md)
Phần 3; các điểm khác spec ghi ở [mục 8](#8-sai-khác-so-với-spec).

Nguyên tắc cần nhớ trước khi đọc chi tiết:

- Mobile **chỉ** gọi data-server và các URL có chữ ký mà data-server trả về. Không bao
  giờ gọi ai-server, redis hay object-storage bằng địa chỉ nội bộ.
- Ảnh **không** đi qua data-server. Data-server chỉ cấp URL; byte ảnh đi thẳng lên
  object-storage để hai server không thành nút nghẽn băng thông.
- Toàn bộ pipeline chạy **bất đồng bộ**. Tạo job xong thì Mobile polling tiến độ.
- **Server không có database và không giữ tủ đồ.** Tủ đồ nằm trên điện thoại. Server
  trả về danh sách món đồ *đề xuất*; user xem, sửa tag nếu cần, rồi xác nhận — response
  xác nhận mang đủ thứ để app tự lưu món đó (ảnh, tag, mô tả, embedding,
  [mục 6.3](#63-post-v1jobsjobidgarmentsconfirm--xác-nhận-món-đồ)). Mọi thứ trên server
  (job, kết quả, ảnh món đồ) tự hết hạn sau **7 ngày**.
- **Embedding khuôn mặt của user do app gửi kèm mỗi job** ([mục 3](#3-khuôn-mặt-tham-chiếu--gửi-embedding-kèm-job)),
  để server biết người nào trong ảnh nhóm là chủ tủ đồ. Ảnh khuôn mặt không rời khỏi máy.

---

## 1. Base URL và xác thực

### 1.1 Base URL

| Môi trường | Base URL |
|---|---|
| Test (máy vast.ai hiện tại) | `http://70.68.84.2:41530` |
| Production | domain thật của data-server |

Đặt giá trị này trong config build, **không hard-code** — chuyển Test ↔ Production chỉ
nên là đổi một hằng số.

> Port `41530` là port public mà vast.ai map cho container port `10100`. Nếu instance
> được tạo lại, con số này đổi — đọc lại bằng `vast-capabilities` trên máy chủ, đừng coi
> `41530` là cố định.

### 1.2 Hai lớp token (chỉ môi trường Test)

Môi trường test nằm sau cổng xác thực của vast.ai (Caddy), nên request phải mang **hai**
token khác nhau. Production chỉ còn lớp thứ hai.

| Lớp | Token | Cách gửi | Mục đích |
|---|---|---|---|
| 1. Edge (Caddy) | Token của instance vast.ai | `?token=<EDGE_TOKEN>` trên URL | Chặn người lạ chạm vào máy test |
| 2. Ứng dụng | Bearer token của user | Header `Authorization: Bearer <APP_TOKEN>` | Xác định account sở hữu batch/job |

Vì sao edge token đi ở **query param** chứ không phải header: cả hai lớp đều dùng header
`Authorization: Bearer`, nên chỉ một trong hai nhét vừa vào đó. Query param dành cho lớp
edge, header để dành cho token ứng dụng.

```
GET http://70.68.84.2:41530/v1/jobs/<jobId>?token=<EDGE_TOKEN>
Authorization: Bearer <APP_TOKEN>
```

**Ở Production bỏ hẳn `?token=`**, chỉ giữ header `Authorization`.

### 1.3 Token test hiện có

Instance test có sẵn một token (thay cho hệ thống đăng nhập thật, chưa nằm trong phạm vi
bản test). Server không có database: danh sách token hợp lệ nằm trong biến `API_TOKENS`
của `${WORKSPACE}/.env` trên máy chủ, dạng `token:accountId,...`.

Hai token đều là **giá trị sống của instance test, cố tình không commit vào repo này** —
lấy trực tiếp trên máy chủ khi cần:

```bash
# APP_TOKEN — bearer token của account test
grep TEST_BEARER_TOKEN ${WORKSPACE:-/workspace}/.env

# EDGE_TOKEN — token của instance vast.ai (lớp Caddy)
echo $OPEN_BUTTON_TOKEN
```

Đặt chúng vào cấu hình build/biến môi trường của app, **không hard-code và không commit
vào repo app**. Token test bị lộ thì bất kỳ ai cũng gửi ảnh và đọc kết quả của account đó.

Tạo thêm token cho một account test mới:

```bash
cd /workspace/qie-outfit-comfyui-server && source /venv/main/bin/activate
python -m server.data_server.scripts.add_api_token   # in token + accountId, thêm vào API_TOKENS
supervisorctl restart data-server
```

`accountId` **không bao giờ** truyền trong body hay query — server tự suy ra từ token, để
client không thể đọc job của người khác.

---

## 2. Luồng gọi tổng thể

```
 (mỗi lần quét thư viện ảnh)
 ┌─ POST /v1/uploads/presign    ──► batchId + N uploadUrl
 ├─ PUT  <uploadUrl> × N        (ảnh gốc ──► object-storage, 4 luồng song song)
 ├─ POST /v1/jobs               (+ embedding khuôn mặt) ──► jobId, status=pending, faceReference.source
 ├─ GET  /v1/jobs/{jobId}       (lặp mỗi 5–10s cho tới completed)
 │   └─ POST /v1/jobs/{jobId}/cancel   (nếu user bấm huỷ)
 │
 │  (màn hình duyệt kết quả — có thể bắt đầu ngay khi job còn đang chạy)
 ├─ GET  /v1/jobs/{jobId}/garments          ──► các món đồ đề xuất + tag của AI
 ├─ POST /v1/jobs/{jobId}/garments/reject   (món user bỏ đi)
 └─ POST /v1/jobs/{jobId}/garments/confirm  (món user giữ, kèm tag đã sửa)
                                             ──► ảnh + tag + embedding ──► app lưu vào tủ đồ trên máy
```

Ở phía server, mỗi ảnh đi qua D0b (tách đúng người bằng khuôn mặt) → D1 (vẽ lại trang
phục thành ảnh mockup) → D3 (gắn tag + sinh embedding) → D2 (bỏ món trùng) → D4 (lưu kết
quả **chờ duyệt**, trong Redis, có hạn 7 ngày). Mobile không cần biết chi tiết, nhưng cần
biết **một ảnh gốc sinh ra nhiều món đồ**.

---

## 3. Khuôn mặt tham chiếu — gửi embedding kèm job

Với ảnh có nhiều người, server phải biết người nào là chủ tủ đồ trước khi tách trang phục
— nếu không sẽ tạo ra món đồ từ quần áo của người khác. Server so khuôn mặt bằng ArcFace
(ngưỡng cosine **0.35**) với **embedding khuôn mặt của user mà app gửi kèm job**.

App vốn đã tính embedding này cho bước A4 (đối chiếu khuôn mặt on-device), bằng **đúng
model server dùng** — InsightFace `w600k_mbf` (MobileFaceNet) — nên hai bên cùng một không
gian embedding và server so thẳng được. Gửi embedding thay vì ảnh: **ảnh khuôn mặt không
rời khỏi điện thoại**, và không cần thêm bước upload nào.

Cách gửi: thêm vào body của `POST /v1/jobs` ([mục 4.3](#43-post-v1jobs)):

```jsonc
"faceReference": {
  "embedding": [0.0123, -0.0456, ...],   // đúng 512 số thực
  "model": "w600k_mbf"
}
```

- `embedding` là vector **512 chiều** lấy ra từ model (cùng embedding app dùng ở A4 — đã
  căn chỉnh khuôn mặt theo 5 điểm mốc như spec §1.1). Không bắt buộc chuẩn hoá L2 sẵn,
  server tự chuẩn hoá; nhưng đừng lượng tử hoá hay cắt bớt.
- `model` bắt buộc và phải là `"w600k_mbf"`. Nếu sau này app đổi model nhận diện khuôn mặt,
  embedding của model khác **không so được** với server — server sẽ từ chối thay vì so sai.
- Mọi ảnh của job đều được so với embedding này. Server **không lưu** embedding: nó chỉ đi
  kèm các vé việc của job và mất cùng job. Mỗi lần quét app gửi lại.
- Chọn embedding đăng ký từ ảnh rõ mặt, chính diện, chỉ một người.

**Embedding không hợp lệ** (sai `model`, không đủ 512 số, toàn số 0, có NaN) **không làm
job thất bại**: job vẫn được tạo, server dùng nguồn dự phòng bên dưới, và response có thêm
`faceReference.warning` nói rõ lý do — app nên ghi log cảnh báo này. Phần tử không phải số
thì trả `422`.

**Không gửi embedding**: job dùng nguồn dự phòng, theo thứ tự —

1. Ảnh khuôn mặt upload kèm batch (dành cho client chưa tính được embedding): thêm
   `"faceReference": {"contentType": "image/jpeg"}` vào `POST /v1/uploads/presign`, PUT ảnh
   vào `faceReference.uploadUrl` như ảnh thường ([mục 4.2](#42-upload-ảnh-lên-object-storage))
   **trước khi** tạo job. Ảnh bị xoá cùng ảnh gốc khi job kết thúc.
2. Ảnh mặc định của server. Ở môi trường Test là `face-image/selfie.jpg`
   (biến `TEST_FIXED_FACE_REF_IMAGE`).
3. Không có gì — D0b lùi về "người lớn nhất khung hình".

Ảnh nào không có người khớp khuôn mặt thì D0b cũng lùi về "người lớn nhất khung hình", và
kết quả là quần áo của người đó.

Response của `POST /v1/jobs` và `GET /v1/jobs/{jobId}` cho biết job đang dùng nguồn nào:
`"faceReference": {"source": "embedding"}` — embedding app gửi; `"image"` — ảnh upload kèm
batch; `"test-fixture"` — ảnh mặc định của server test; `null` — không có.

> **Cần đo trước khi phát hành:** app căn chỉnh khuôn mặt theo landmark của ML Kit, server
> căn chỉnh các khuôn mặt trong ảnh nhóm theo landmark của SCRFD. Hai cách cùng đưa về
> template chuẩn 112×112 nên chênh lệch thường nhỏ, nhưng chưa đo trên app thật. Cách kiểm:
> tính trên app embedding của `face-image/selfie.jpg`, so cosine với embedding server tính
> từ cùng ảnh — nên ≥ 0.9; embedding phía server lấy bằng lệnh dưới (chạy trong
> `/workspace/qie-outfit-comfyui-server`, `source /venv/main/bin/activate`), in ra mảng JSON
> 512 số:
>
> ```bash
> python -c "import json, pathlib, detect_clothing_by_face as b; print(json.dumps([round(float(x), 6) for x in b.find_reference_embedding(b.load_face_app(), pathlib.Path('face-image/selfie.jpg'))]))" 2>/dev/null | tail -1
> ```
>
> Sau đó chạy một job có ảnh nhóm **với embedding của app** và xem "độ giống khuôn mặt"
> trong trang report vẫn trên 0.35 (bằng embedding của server: 0.44–0.60 trên ảnh test).

## 4. Upload ảnh và tạo job

### 4.1 `POST /v1/uploads/presign`

Xin đường dẫn upload cho cả lô ảnh trong một lần gọi.

```jsonc
// request
{
  "items": [
    { "localId": "asset-IMG_9652", "contentType": "image/jpeg", "checksum": "optional-sha256" },
    { "localId": "asset-IMG_9822", "contentType": "image/jpeg" }
  ],
  "faceReference": { "contentType": "image/jpeg" }      // chỉ khi gửi ẢNH khuôn mặt thay cho embedding (mục 3)
}

// response
{
  "batchId": "5245fa0b-351c-4075-85fa-819800e98395",
  "items": [
    {
      "localId":   "asset-IMG_9652",
      "objectKey": "raw/67180bc0-.../5245fa0b-.../asset-IMG_9652.jpg",
      "uploadUrl": "http://70.68.84.2:41783/wardrobe-raw/raw/...?X-Amz-Algorithm=...",
      "expiresAt": "2026-09-11T05:00:00+00:00"
    }
  ],
  "faceReference": {                                    // chỉ có khi request có faceReference
    "objectKey": "face/67180bc0-.../5245fa0b-....jpg",
    "uploadUrl": "http://70.68.84.2:41783/wardrobe-raw/face/...?X-Amz-Algorithm=...",
    "expiresAt": "2026-09-11T05:00:00+00:00"
  }
}
```

`localId` là **id do Mobile tự sinh** (nên dùng asset ID của hệ điều hành). Nó là sợi dây
duy nhất nối kết quả trả về với ảnh gốc trong thư viện, cần cho bước cập nhật index ảnh
đã quét. `contentType` hỗ trợ `image/jpeg`, `image/png`, `image/webp`, `image/heic`.

`uploadUrl` hết hạn sau **15 phút**. Lô rất lớn thì chia nhỏ thành nhiều lần presign thay
vì xin một lần rồi upload dần quá hạn.

### 4.2 Upload ảnh lên object-storage

`PUT` thẳng vào `uploadUrl`, body là **raw bytes** của file (không multipart, không form).
Ảnh khuôn mặt (nếu dùng đường dự phòng ở mục 3) upload đúng cùng cách.

```http
PUT http://70.68.84.2:41783/wardrobe-raw/raw/...?X-Amz-Algorithm=...
Content-Type: image/jpeg
Cookie: C.55850779_auth_token=<EDGE_TOKEN>

<bytes ảnh>
```

Ba điểm bắt buộc, sai một cái là `403 SignatureDoesNotMatch`:

1. **`Content-Type` phải trùng đúng `contentType` đã khai ở bước presign.** Giá trị này
   nằm trong chữ ký.
2. **Không sửa URL.** Không thêm/bớt/sắp xếp lại query param, không encode lại. Mọi tham
   số `X-Amz-*` đều nằm trong chữ ký.
3. **Edge token ở môi trường Test phải đi bằng Cookie, không phải query param** — thêm
   `?token=` vào URL sẽ phá chữ ký. Tên cookie là `<VAST_CONTAINERLABEL>_auth_token`,
   trên instance hiện tại là `C.55850779_auth_token`. Production không cần cookie này.

Thành công trả `200` (body rỗng). Upload **4 ảnh song song** — mỗi lần upload chủ yếu là
chờ mạng chứ không tốn CPU, chạy tuần tự chỉ cộng dồn độ trễ.

### 4.3 `POST /v1/jobs`

Gọi sau khi đã upload xong. Chỉ liệt kê ảnh **upload thành công** — job được phép chỉ chứa
một phần batch, ảnh lỗi để lần quét sau thử lại.

```jsonc
// request
{
  "batchId": "5245fa0b-351c-4075-85fa-819800e98395",
  "uploadedItems": ["asset-IMG_9652", "asset-IMG_9822"],
  "faceReference": { "embedding": [0.0123, -0.0456, ...], "model": "w600k_mbf" }   // mục 3
}

// response
{
  "jobId": "e1f0b194-2110-4278-8462-1d6e5e9bee48",
  "status": "pending",
  "totalItems": 2,
  "createdAt": "2026-09-11T04:44:49.931466+00:00",
  "faceReference": { "source": "embedding" }   // embedding / image / test-fixture / null — mục 3
}
```

Embedding không hợp lệ thì `faceReference` có thêm `"warning": "<lý do>"` và `source` là
nguồn dự phòng đã dùng (mục 3). Server chốt khuôn mặt của job **tại thời điểm gọi endpoint
này**: ảnh khuôn mặt PUT sau đó không được dùng. Nếu `uploadedItems` rỗng hoặc không khớp
batch nào → `400`.

---

## 5. Theo dõi tiến độ và huỷ

### 5.1 `GET /v1/jobs/{jobId}`

```jsonc
{
  "status": "completed",
  "totalItems": 8,
  "processedItems": 8,
  "failedItems": 0,
  "updatedAt": "2026-09-11T04:51:42.686506+00:00",
  "items": [
    { "localId": "asset-IMG_9822", "status": "success", "garmentCount": 3 },
    { "localId": "asset-IMG_9823", "status": "success", "garmentCount": 1, "duplicateCount": 1 },
    { "localId": "asset-IMG_0168", "status": "failed"  }
  ],
  "review": { "pending": 4, "confirmed": 0, "rejected": 0, "duplicate": 1 },
  "faceReference": { "source": "embedding" }
}
```

`garmentCount` là số món đồ tách được từ ảnh đó **cần duyệt** (có thể là `0` nếu ảnh
không có trang phục nào, hoặc mọi món của ảnh đều đã có ở ảnh khác của job).
`duplicateCount` (chỉ có khi > 0) là số món của ảnh đó trùng với món đã tách từ ảnh khác
trong cùng job - xem §6.1. `review` đếm các món đồ của cả job theo trạng thái duyệt —
dùng `review.pending` để hiện badge "còn N món chờ duyệt"; `review.duplicate` không cần
duyệt.

Job và kết quả của nó được giữ **7 ngày** kể từ lần thay đổi gần nhất (xử lý xong một
ảnh, duyệt một món...). Quá hạn thì `GET /v1/jobs/{jobId}` trả `404` và mọi món đồ của
job biến mất khỏi server — món nào user đã xác nhận thì app phải đã lưu về máy từ trước.

`status` của job:

| Giá trị | Ý nghĩa |
|---|---|
| `pending` | Vừa tạo, chưa worker nào nhận |
| `processing` | Đang xử lý |
| `cancelling` | Đã nhận lệnh huỷ, còn ảnh đang chạy dở |
| `cancelled` | Đã dừng hẳn |
| `completed` | Mọi ảnh đã có kết quả (kể cả khi có ảnh lỗi) |
| `failed` | Job hỏng ở mức tổng thể |

Mảng `items[]` **chỉ chứa ảnh đã xử lý xong**; ảnh còn `pending` không xuất hiện — số
phần tử tăng dần theo tiến độ.

**Nhịp polling khuyến nghị:** 5–10s. Thời gian đo trên instance test (RTX 4090 48GB): khi
model đã nạp, **~4–5 giây mỗi ảnh**; lô 10 ảnh mất ~60 giây tính cả lúc nạp model. Ảnh
đầu tiên sau khi server rảnh một lúc chậm hơn (~40s) vì model được nạp lại vào VRAM. Đừng
polling dày hơn 5s — không có kết quả sớm hơn, chỉ tốn pin.

Con số này phụ thuộc phần cứng server, đừng hard-code timeout theo nó.

Mất mạng giữa chừng **không ảnh hưởng** việc xử lý ở server; mở app lại rồi hỏi tiếp
trạng thái là đủ, không mất tiến độ.

### 5.2 `POST /v1/jobs/{jobId}/cancel`

```jsonc
{ "status": "cancelling" }   // còn ảnh đang chạy dở
{ "status": "cancelled"  }   // đã dừng ngay được
```

Ảnh đang xử lý dở vẫn được chạy nốt (GPU đã tốn gần hết công rồi), ảnh chưa bắt đầu bị
bỏ qua và **đếm vào `failedItems`**. Món đồ từ những ảnh đã xử lý xong trước khi huỷ vẫn
xuất hiện ở màn hình duyệt — user có thể xác nhận hoặc bỏ chúng như bình thường. Kết quả
đo thực tế trên job 10 ảnh: huỷ sau 4 giây → `processedItems=2`, `failedItems=8`. UI nên
vẫn polling sau khi gửi lệnh huỷ, tới khi status thành `cancelled`.

### 5.3 Cập nhật index ảnh đã quét

Khi job `completed`, chỉ thêm asset ID của những ảnh có `items[].status == "success"` vào
index cục bộ. Ảnh `failed` **không** được thêm, để lần quét sau thử lại. Ảnh đã xử lý
thành công vẫn vào index kể cả khi user bỏ hết món đồ của nó — nó đã được xem rồi.

---

## 6. Duyệt kết quả và lưu vào tủ đồ

Server **không** giữ tủ đồ. Sau khi tách trang phục, mỗi món đồ nằm ở trạng thái `pending`
(chờ duyệt). User xem từng món trên app rồi:

- **xác nhận** (có thể sửa tag trước) → server trả về ảnh + tag cuối cùng + embedding,
  **app lưu món đó vào tủ đồ trên máy** (tải ảnh về, không giữ URL);
- **bỏ** → ảnh món đồ bị xoá trên server;
- **không làm gì** → món đồ tự hết hạn sau 7 ngày.

```
pending ──POST /v1/jobs/{id}/garments/confirm──► confirmed  (app tự lưu vào tủ đồ)
   └────POST /v1/jobs/{id}/garments/reject───► rejected

duplicate   (server tự gán: món này đã tách được từ một ảnh khác của cùng job -
             không hiện trong danh sách duyệt, xem §6.1)
```

### 6.1 `GET /v1/jobs/{jobId}/garments` — danh sách món đồ để duyệt

Gọi được **ngay khi job còn đang chạy**: danh sách lớn dần theo từng ảnh xử lý xong, nên
app có thể mở màn hình duyệt sớm thay vì chờ cả lô.

| Query param | Kiểu | Mô tả |
|---|---|---|
| `includeEmbeddings` | bool, tuỳ chọn | `true`: kèm `visualEmbedding`/`textEmbedding` của từng món (2 × 768 số) — để app so với tủ đồ trên máy trước khi cho user duyệt, xem [6.4](#64-trùng-với-tủ-đồ-đã-có--app-tự-so) |

```jsonc
{
  "jobId": "bfbf24f3-e2fb-4eaf-9c96-e5705ab6c140",
  "garments": [
    {
      "garmentId": "1dbffca2638349d6918f7e5f930a6faf",
      "localId": "asset-IMG_0168",          // ảnh gốc sinh ra món này
      "reviewStatus": "pending",            // pending / confirmed / rejected (duplicate không được liệt kê)
      "imageUrl": "http://70.68.84.2:41783/wardrobe-items/pending/...?X-Amz-Algorithm=...",
      "tags": {
        "type": "shirts",
        "category": "clothing",
        "sub_category": "shirts",
        "gender": "men",
        "color":   [ { "color": "blue", "confidence": 95.2 } ],
        "neck":    ["spread collar"],
        "sleeve":  ["short sleeve"],
        "pattern": []
      },
      "description": "short sleeve shirts in blue. spread collar. men's clothing.",
      "duplicates": [ { "garmentId": "...", "localId": "asset-IMG_0170", "score": 0.94 } ]
    }
  ]
}
```

- Thứ tự: theo ảnh gốc, rồi theo vị trí món đồ trong ảnh.
- Món đã `confirmed` mang tag user đã chốt, kèm `aiTags` (tag gốc của AI) và `tagsEdited`.
- Món `rejected` có `imageUrl: null` (ảnh đã bị xoá).
- **Trùng giữa các ảnh của cùng job** (cùng một người mặc cùng bộ đồ ở nhiều ảnh): server
  chỉ trả về **một** bản của mỗi món - bản tách được trước. Các bản còn lại có
  `reviewStatus: "duplicate"` và **không có trong `garments`**; món đại diện liệt kê chúng
  ở `duplicates` (để app hiện "cũng có trong N ảnh khác" nếu muốn; không có trường này khi
  món không trùng). App không cần xác nhận/bỏ các bản này; nếu vẫn gửi `garmentId` của
  chúng thì confirm/reject xử lý như món `pending`.

Đọc tags cho đúng:

- `type`, `category`, `sub_category`, `gender` là **đơn nhãn** (string).
- `color` là **đa nhãn kèm độ tin cậy**, đã sắp giảm dần — lấy phần tử đầu làm màu chính.
- `neck`, `sleeve`, `pattern` là **mảng string, có thể rỗng** khi không áp dụng (quần
  không có kiểu tay áo). Luôn kiểm tra rỗng trước khi hiển thị.

**Ảnh trang phục là ảnh được vẽ lại trên nền trắng**, không phải vùng pixel cắt từ ảnh
gốc — không còn khuôn mặt hay hậu cảnh. Chi tiết hoa văn có thể lệch đôi chút so với đồ
thật; đủ dùng để nhận ra "cái áo hoa be tay ngắn", không dùng để đối chiếu chính xác
từng chi tiết sản phẩm. Ảnh luôn là **hình vuông** (PNG), nền trắng phẳng, món đồ nằm giữa
khung, mỗi ảnh chứa **đúng một món** — an toàn để hiển thị thẳng trong lưới.

**`imageUrl` là URL có chữ ký, hết hạn sau 1 giờ** — gọi lại endpoint để lấy URL mới. Ở
môi trường Test, tải ảnh cũng cần gửi kèm Cookie edge token như [mục 4.2](#42-upload-ảnh-lên-object-storage).

### 6.2 `POST /v1/jobs/{jobId}/garments/reject` — bỏ món đồ

```jsonc
// request
{ "garmentIds": ["1dbffca2638349d6918f7e5f930a6faf"] }
// response
{ "rejected": ["1dbffca2638349d6918f7e5f930a6faf"], "notFound": [], "alreadyConfirmed": [] }
```

Gọi lại với cùng id là an toàn (id đã bỏ vẫn nằm trong `rejected`). Món đã xác nhận thì
không bỏ được bằng endpoint này — nó nằm trong `alreadyConfirmed` (món đó đã thuộc tủ đồ
trên máy, xoá nó là việc của app).

### 6.3 `POST /v1/jobs/{jobId}/garments/confirm` — xác nhận món đồ

User giữ những món này. Server ghi nhận trạng thái `confirmed` trong job và trả về **mọi
thứ app cần để lưu món đó vào tủ đồ trên máy**.

```jsonc
// request
{
  "garments": [
    { "garmentId": "95ca02ba7f44..." },                                   // giữ nguyên tag của AI
    { "garmentId": "3abb65512729...", "tags": { "gender": "women",        // sửa tag
                                                 "color": [ { "color": "navy", "confidence": 100 } ] } }
  ]
}
// response
{
  "items": [
    {
      "garmentId": "95ca02ba7f44...",
      "localId": "asset-IMG_0168",
      "reviewStatus": "confirmed",
      "imageUrl": "http://70.68.84.2:41783/wardrobe-items/pending/...?X-Amz-Algorithm=...",
      "tags": { ... },                 // tag cuối cùng (AI + phần user sửa)
      "aiTags": { ... },               // tag gốc của AI
      "tagsEdited": false,
      "description": "short sleeve shirts in blue. spread collar. men's clothing.",
      "visualEmbedding": [ 0.0123, ... ],   // 768 số, đã chuẩn hoá L2
      "textEmbedding":   [ -0.045, ... ]    // 768 số, đã chuẩn hoá L2
    }
  ],
  "errors": [ { "garmentId": "...", "error": "already rejected" } ]
}
```

- **App phải tải ảnh từ `imageUrl` về máy ngay** khi nhận response: URL hết hạn sau 1 giờ,
  và bản thân ảnh trên server bị xoá khi job hết hạn (7 ngày). Lỡ hết hạn URL thì gọi lại
  confirm (hoặc `GET .../garments`) để lấy URL mới, miễn là job còn.
- Lưu cả `visualEmbedding`/`textEmbedding`: đó là đầu vào để so trùng với tủ đồ
  ([6.4](#64-trùng-với-tủ-đồ-đã-có--app-tự-so)) và tìm kiếm trong tủ đồ sau này.
- `tags` là **tuỳ chọn** và chỉ cần gửi những trường user đã sửa; trường không gửi giữ
  nguyên giá trị AI. Gửi đúng kiểu như ở 6.1 (`color` là mảng object, `neck`/`sleeve`/
  `pattern` là mảng string). Khi tag bị sửa, server tự dựng lại `description`.
- Xác nhận được **từng phần**: gửi vài món một lần, lần sau gửi tiếp.
- **Idempotent**: gửi lại một `garmentId` đã xác nhận (ví dụ retry sau khi mất mạng) trả
  về đúng món đó với tag đã chốt. Gửi lại kèm `tags` khác thì tag mới được áp lên tag của
  AI (sửa lại lần nữa).
- `errors[]` liệt kê món không xác nhận được: `not found` (sai id hoặc job đã hết hạn),
  `already rejected`. Các món còn lại trong request vẫn được xử lý bình thường.
- `jobId` không tồn tại, thuộc account khác, hoặc đã hết hạn → `404`.

### 6.4 Trùng với tủ đồ đã có — app tự so

Server chỉ bỏ món trùng **trong cùng một job** (§6.1). Món trùng với đồ đã có trong tủ (từ
các lần quét trước, ví dụ khi index ảnh cục bộ bị mất và ảnh cũ bị quét lại) thì app tự so,
vì tủ đồ nằm trên máy. Dùng đúng quy tắc server dùng cho trùng trong job
(`server/common/embeddings.py`, `same_garment_score`), đo trên dữ liệu thật:

- hai món có **chung ít nhất một màu** trong 2 màu đầu của `tags.color`, **và**
- `dot(visualEmbedding_a, visualEmbedding_b) ≥ 0.85`, **và**
- `(dot(visual_a, visual_b) + dot(textEmbedding_a, textEmbedding_b)) / 2 ≥ 0.87`.

(Embedding đã chuẩn hoá L2 nên cosine similarity chỉ là tích vô hướng.) Lấy embedding của
món đang chờ duyệt bằng `GET .../garments?includeEmbeddings=true`. Nên chỉ **gợi ý** "có vẻ
bạn đã có món này" để user quyết định, không tự bỏ.

### Job xong nhưng không có món nào để duyệt

Ảnh không có trang phục nhận diện được (ảnh chân dung cận mặt...) cho `garmentCount: 0`;
ảnh mà mọi món đều đã có ở ảnh khác của job cũng cho `garmentCount: 0` kèm
`duplicateCount`. **UI không được coi "job xong nhưng không có món nào" là lỗi** — hãy hiển
thị "không tìm thấy trang phục mới".

---

## 7. Health check và lỗi

### `GET /v1/health` — không cần `Authorization`

```jsonc
{ "status": "ok",
  "dependencies": { "objectStorage": "ok", "queue": "ok" } }
```

Ở môi trường Test vẫn cần `?token=<EDGE_TOKEN>` vì lớp edge chặn trước khi tới app.

### Mã lỗi

| Mã | Nguyên nhân | Xử lý phía Mobile |
|---|---|---|
| `401` (body rỗng/HTML) | Thiếu/sai **edge token** | Kiểm tra `?token=` |
| `401 {"detail":"missing bearer token"}` | Thiếu header `Authorization` | Gắn app token |
| `401 {"detail":"invalid token"}` | App token sai/đã bị gỡ khỏi `API_TOKENS` | Đăng nhập lại |
| `400` | `uploadedItems` không khớp batch | Không retry nguyên trạng |
| `422` | Tham số sai kiểu (ví dụ `faceReference.embedding` có phần tử không phải số) | Lỗi lập trình, sửa request |
| `403` (từ object-storage) | URL hết hạn hoặc chữ ký sai | Xin presign mới |
| `404` | `jobId` không tồn tại, thuộc account khác, hoặc đã quá 7 ngày | Không retry |
| `5xx` | Lỗi server | Retry với exponential backoff |

Phân biệt hai loại `401` bằng body: lớp edge trả HTML/rỗng, app trả JSON có `detail`.

Ảnh lỗi lẻ tẻ trong job là **bình thường** — mỗi vé việc được thử lại tối đa 3 lần trước
khi đánh `failed`, và không ảnh hưởng ảnh khác cùng job.

---

## 8. Sai khác so với spec

Các điểm khác [`wardrobe-system-spec.md`](wardrobe-system-spec.md) Phần 3, đều đã hiện thực
và kiểm chứng trên instance test:

1. **Không có database phía server; tủ đồ nằm trên điện thoại.** Spec đặt wardrobe trong
   postgres của data-server (`POST`/`GET /v1/wardrobe/items`). Bản này bỏ hẳn database:
   kết quả chờ duyệt nằm trong Redis có hạn 7 ngày, xác nhận là
   `POST /v1/jobs/{jobId}/garments/confirm` và trả về ảnh + tag + embedding cho app tự
   lưu. `GET /v1/wardrobe/items` không còn; so trùng với tủ đồ chuyển về app ([6.4](#64-trùng-với-tủ-đồ-đã-có--app-tự-so)).

2. **Embedding khuôn mặt tham chiếu gửi kèm mỗi job** ([mục 3](#3-khuôn-mặt-tham-chiếu--gửi-embedding-kèm-job)).
   Spec mô tả D0b "đối chiếu khuôn mặt với embedding tham chiếu đã lưu của account" nhưng
   không định nghĩa API đăng ký; không có database thì server cũng không giữ được nó giữa
   các lần quét. `POST /v1/jobs` vì thế có thêm `faceReference` (embedding), và bản tin job
   queue có thêm trường `faceRefEmbedding` / `faceRefKey` (spec §3.3) — nội bộ server.

3. **Object-storage được publish ra ngoài ở môi trường Test.** Spec §2.3.4 nói chỉ
   data-server mở cổng, nhưng §1.2 D0 lại yêu cầu Mobile upload thẳng lên object-storage —
   trên một máy thì hai điều đó không thể cùng đúng. Object-storage được đặt sau cùng lớp
   token edge, và URL vẫn là presigned có hạn 15 phút. Đây là ràng buộc của môi trường
   test một máy; Production dùng dịch vụ managed nên không có vấn đề này.

4. **Hai lớp token chỉ tồn tại ở Test** ([mục 1.2](#12-hai-lớp-token-chỉ-môi-trường-test)).
   Spec chỉ mô tả một Bearer token; lớp edge là của hạ tầng vast.ai, không phải của hệ thống.

5. **Bước duyệt kết quả trước khi vào tủ đồ** ([mục 6](#6-duyệt-kết-quả-và-lưu-vào-tủ-đồ)):
   đã cập nhật vào spec (§1.2 E1, §2.1 D4, §3.1). Ghi ở đây vì bản mobile cũ giả định
   kết quả tự vào tủ đồ.

Ngoài ra, hệ thống đăng nhập/session mà spec giả định có sẵn **chưa được hiện thực** —
bản test dùng bearer token tĩnh khai trong `API_TOKENS` của `.env`.

---

## 9. Ví dụ hoàn chỉnh

### 9.1 curl

```bash
BASE=http://70.68.84.2:41530
EDGE=<EDGE_TOKEN>
APP=$(grep TEST_BEARER_TOKEN ${WORKSPACE:-/workspace}/.env | cut -d= -f2)
COOKIE="C.55850779_auth_token=$EDGE"

# 0) health
curl -s "$BASE/v1/health?token=$EDGE"

# 1) presign cho ảnh cần xử lý
PRE=$(curl -s -X POST "$BASE/v1/uploads/presign?token=$EDGE" \
  -H "Authorization: Bearer $APP" -H "Content-Type: application/json" \
  -d '{"items":[{"localId":"asset-1","contentType":"image/jpeg"}]}')
BATCH=$(echo "$PRE" | jq -r .batchId)
UP=$(echo "$PRE" | jq -r '.items[0].uploadUrl')

# 2) upload ảnh
curl -s -X PUT --data-binary @photo.jpg -H "Content-Type: image/jpeg" --cookie "$COOKIE" "$UP"

# 3) tạo job kèm embedding khuôn mặt (face.json: mảng 512 số app đã tính) —
#    faceReference.source phải là "embedding"
JOB=$(curl -s -X POST "$BASE/v1/jobs?token=$EDGE" \
  -H "Authorization: Bearer $APP" -H "Content-Type: application/json" \
  -d "{\"batchId\":\"$BATCH\",\"uploadedItems\":[\"asset-1\"],
       \"faceReference\":{\"embedding\":$(cat face.json),\"model\":\"w600k_mbf\"}}")
JID=$(echo "$JOB" | jq -r .jobId); echo "$JOB" | jq .faceReference

# 4) polling
until [ "$(curl -s "$BASE/v1/jobs/$JID?token=$EDGE" -H "Authorization: Bearer $APP" \
          | jq -r .status)" = "completed" ]; do sleep 10; done

# 5) xem các món đồ AI tách được
curl -s "$BASE/v1/jobs/$JID/garments?token=$EDGE" -H "Authorization: Bearer $APP" \
  | jq '.garments[] | {garmentId, reviewStatus, type: .tags.type}'
G1=<garmentId muốn giữ>; G2=<garmentId muốn bỏ>

# 6) bỏ một món, xác nhận một món (sửa gender) — response mang ảnh + tag + embedding để lưu
curl -s -X POST "$BASE/v1/jobs/$JID/garments/reject?token=$EDGE" \
  -H "Authorization: Bearer $APP" -H "Content-Type: application/json" -d "{\"garmentIds\":[\"$G2\"]}"
curl -s -X POST "$BASE/v1/jobs/$JID/garments/confirm?token=$EDGE" \
  -H "Authorization: Bearer $APP" -H "Content-Type: application/json" \
  -d "{\"garments\":[{\"garmentId\":\"$G1\",\"tags\":{\"gender\":\"women\"}}]}" \
  | jq '.items[] | {garmentId, tags, imageUrl}'
```

Trên chính máy chủ có sẵn script chạy toàn bộ luồng này (kể cả embedding khuôn mặt, tính từ
`face-image/selfie.jpg`):
`python scripts/mobile_flow_test.py --storage-host localhost:10200 test-images`
(`--face-mode image` để gửi ảnh khuôn mặt thay embedding, `--face-mode none` để không gửi).

### 9.2 Dart / Flutter

```dart
class WardrobeApi {
  WardrobeApi({required this.baseUrl, required this.appToken, this.edgeToken, this.edgeCookie});

  final String baseUrl;       // http://70.68.84.2:41530
  final String appToken;      // bearer token của user
  final String? edgeToken;    // chỉ Test; Production để null
  final String? edgeCookie;   // 'C.55850779_auth_token'

  Uri _u(String path, [Map<String, String> q = const {}]) => Uri.parse('$baseUrl$path').replace(
        queryParameters: {...q, if (edgeToken != null) 'token': edgeToken!},
      );

  Map<String, String> get _headers => {
        'Authorization': 'Bearer $appToken',
        'Content-Type': 'application/json',
      };

  Future<Map<String, dynamic>> _json(http.Response r) {
    if (r.statusCode >= 400) throw WardrobeApiException(r.statusCode, r.body);
    return Future.value(jsonDecode(r.body) as Map<String, dynamic>);
  }

  Map<String, String> get _edgeCookieHeader =>
      {if (edgeCookie != null && edgeToken != null) 'Cookie': '$edgeCookie=$edgeToken'};

  /// PUT thẳng lên object-storage. KHÔNG sửa uploadUrl và KHÔNG thêm token vào
  /// query — mọi tham số X-Amz-* đều nằm trong chữ ký. Edge token đi bằng Cookie.
  Future<void> uploadBytes(String uploadUrl, Uint8List bytes, String contentType) async {
    final r = await http.put(Uri.parse(uploadUrl), body: bytes,
        headers: {'Content-Type': contentType, ..._edgeCookieHeader});
    if (r.statusCode != 200) throw WardrobeApiException(r.statusCode, r.body);
  }

  /// Tải ảnh món đồ về máy (để lưu vào tủ đồ cục bộ).
  Future<Uint8List> downloadBytes(String imageUrl) async {
    final r = await http.get(Uri.parse(imageUrl), headers: _edgeCookieHeader);
    if (r.statusCode != 200) throw WardrobeApiException(r.statusCode, r.body);
    return r.bodyBytes;
  }

  Future<Map<String, dynamic>> presignUploads(List<({String localId, String contentType})> items) async =>
      _json(await http.post(_u('/v1/uploads/presign'), headers: _headers,
          body: jsonEncode({
            'items': [for (final i in items) {'localId': i.localId, 'contentType': i.contentType}],
          })));

  /// [faceEmbedding]: embedding w600k_mbf (512 số) của user — cùng embedding app dùng ở A4 (mục 3).
  Future<Map<String, dynamic>> createJob(String batchId, List<String> uploadedItems,
          {List<double>? faceEmbedding}) async {
    final job = await _json(await http.post(_u('/v1/jobs'), headers: _headers,
        body: jsonEncode({
          'batchId': batchId,
          'uploadedItems': uploadedItems,
          if (faceEmbedding != null) 'faceReference': {'embedding': faceEmbedding, 'model': 'w600k_mbf'},
        })));
    final warning = (job['faceReference'] as Map?)?['warning'];
    if (warning != null) debugPrint('faceReference bị từ chối: $warning');   // job vẫn chạy với nguồn dự phòng
    return job;
  }

  Future<Map<String, dynamic>> getJob(String jobId) async =>
      _json(await http.get(_u('/v1/jobs/$jobId'), headers: _headers));

  Future<Map<String, dynamic>> cancelJob(String jobId) async =>
      _json(await http.post(_u('/v1/jobs/$jobId/cancel'), headers: _headers));

  /// Các món đồ AI đề xuất (kể cả khi job còn chạy).
  Future<Map<String, dynamic>> jobGarments(String jobId, {bool includeEmbeddings = false}) async =>
      _json(await http.get(_u('/v1/jobs/$jobId/garments',
          {if (includeEmbeddings) 'includeEmbeddings': 'true'}), headers: _headers));

  Future<Map<String, dynamic>> rejectGarments(String jobId, List<String> garmentIds) async =>
      _json(await http.post(_u('/v1/jobs/$jobId/garments/reject'), headers: _headers,
          body: jsonEncode({'garmentIds': garmentIds})));

  /// [edits]: garmentId -> các tag user đã sửa (null/không có = giữ tag của AI).
  /// Response.items: ảnh + tag + embedding — lưu vào tủ đồ trên máy.
  Future<Map<String, dynamic>> confirmGarments(String jobId, List<String> garmentIds,
          {Map<String, Map<String, dynamic>> edits = const {}}) async =>
      _json(await http.post(_u('/v1/jobs/$jobId/garments/confirm'), headers: _headers,
          body: jsonEncode({
            'garments': [
              for (final id in garmentIds) {'garmentId': id, if (edits[id] != null) 'tags': edits[id]}
            ],
          })));
}
```

Upload 4 ảnh song song rồi tạo job kèm embedding khuôn mặt:

```dart
final pre = await api.presignUploads(items);
const batchSize = 4;
final uploaded = <String>[];
for (var i = 0; i < items.length; i += batchSize) {
  final slice = (pre['items'] as List).skip(i).take(batchSize);
  await Future.wait(slice.map((it) async {
    try {
      await api.uploadBytes(it['uploadUrl'], await readBytes(it['localId']), 'image/jpeg');
      uploaded.add(it['localId']);
    } catch (_) {
      // ảnh lỗi bị bỏ qua — lần quét sau thử lại, vì localId chưa vào index
    }
  }));
}
final job = await api.createJob(pre['batchId'], uploaded,
    faceEmbedding: await faceStore.userEmbedding());   // embedding đã đăng ký cho A4
```

Polling tới khi xong:

```dart
Future<Map<String, dynamic>> waitForJob(WardrobeApi api, String jobId) async {
  const terminal = {'completed', 'cancelled', 'failed'};
  while (true) {
    final st = await api.getJob(jobId);
    if (terminal.contains(st['status'])) return st;
    await Future.delayed(const Duration(seconds: 10));
  }
}
```

Xác nhận rồi lưu vào tủ đồ trên máy:

```dart
final res = await api.confirmGarments(jobId, keepIds, edits: edits);
for (final it in res['items'] as List) {
  final bytes = await api.downloadBytes(it['imageUrl']);     // tải ngay: URL hết hạn sau 1 giờ
  await localWardrobe.save(
    id: it['garmentId'], image: bytes, tags: it['tags'], description: it['description'],
    visualEmbedding: it['visualEmbedding'], textEmbedding: it['textEmbedding'],
    sourceAsset: it['localId'],
  );
}
```

---

## 10. Checklist tích hợp

- [ ] Base URL nằm trong config build, không hard-code trong logic
- [ ] Edge token ở query param, app token ở header `Authorization`
- [ ] Upload dùng `PUT` raw bytes, `Content-Type` khớp lúc presign, **không sửa uploadUrl**
- [ ] Edge token khi upload/tải ảnh đi bằng **Cookie**, không phải query param
- [ ] Mỗi `POST /v1/jobs` gửi kèm `faceReference` = embedding `w600k_mbf` 512 chiều của user, `model: "w600k_mbf"`; kiểm tra `faceReference.source == "embedding"`, log `warning` nếu có ([mục 3](#3-khuôn-mặt-tham-chiếu--gửi-embedding-kèm-job))
- [ ] Đo cosine giữa embedding app và embedding server trên cùng ảnh selfie trước khi phát hành (cuối mục 3)
- [ ] Upload 4 ảnh song song, `uploadedItems` chỉ chứa ảnh PUT thành công
- [ ] Polling 5–10s, dừng ở `completed`/`cancelled`/`failed`
- [ ] Chỉ thêm asset ID của ảnh `status == "success"` vào index đã quét
- [ ] `neck`/`sleeve`/`pattern` kiểm tra mảng rỗng trước khi hiển thị
- [ ] Có màn hình duyệt: hiện `GET /v1/jobs/{jobId}/garments`, cho sửa tag, xác nhận/bỏ
- [ ] Xác nhận bằng `POST /v1/jobs/{jobId}/garments/confirm`; retry an toàn (idempotent)
- [ ] **Lưu món đã xác nhận vào tủ đồ trên máy**: tải ảnh ngay (URL hết hạn 1 giờ, ảnh trên server hết hạn cùng job), lưu tag + embedding
- [ ] (Tuỳ chọn) gợi ý trùng với tủ đồ trên máy theo quy tắc ở [6.4](#64-trùng-với-tủ-đồ-đã-có--app-tự-so)
- [ ] Nhắc user còn món chờ duyệt (`review.pending` của job) — quá 7 ngày sẽ mất
- [ ] "Job xong nhưng không có món nào" hiển thị như kết quả bình thường, không báo lỗi
