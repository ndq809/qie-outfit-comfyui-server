# Hướng dẫn tích hợp Mobile ↔ Wardrobe Server

Tài liệu dành cho app Mobile (Flutter/Android/iOS) kết nối tới **data-server** của hệ
thống Wardrobe. Mọi endpoint dưới đây khớp với [`wardrobe-system-spec.md`](wardrobe-system-spec.md)
Phần 3, cộng thêm nhóm `/v1/face-reference` (xem [Sai khác so với spec](#8-sai-khác-so-với-spec))
— nhóm này **Mobile không cần gọi** ở bản test, xem [mục 3](#3-ảnh-khuôn-mặt-tham-chiếu--server-test-đã-cố-định-mobile-bỏ-qua).

Nguyên tắc cần nhớ trước khi đọc chi tiết:

- Mobile **chỉ** gọi data-server và các URL có chữ ký mà data-server trả về. Không bao
  giờ gọi ai-server, postgres, redis hay object-storage bằng địa chỉ nội bộ.
- Ảnh **không** đi qua data-server. Data-server chỉ cấp URL; byte ảnh đi thẳng lên
  object-storage để hai server không thành nút nghẽn băng thông.
- Toàn bộ pipeline chạy **bất đồng bộ**. Tạo job xong là xong phần việc của Mobile;
  kết quả lấy về bằng polling.

---

## 1. Base URL và xác thực

### 1.1 Base URL

| Môi trường | Base URL |
|---|---|
| Test (máy vast.ai hiện tại) | `http://218.49.89.120:40123` |
| Production | domain thật của data-server |

Đặt giá trị này trong config build, **không hard-code** — chuyển Test ↔ Production chỉ
nên là đổi một hằng số.

> Port `40123` là port public mà vast.ai map cho container port `10100`. Nếu instance
> được tạo lại, con số này đổi — đọc lại bằng `vast-capabilities` trên máy chủ, đừng coi
> `40123` là cố định.

### 1.2 Hai lớp token (chỉ môi trường Test)

Môi trường test nằm sau cổng xác thực của vast.ai (Caddy), nên request phải mang **hai**
token khác nhau. Production chỉ còn lớp thứ hai.

| Lớp | Token | Cách gửi | Mục đích |
|---|---|---|---|
| 1. Edge (Caddy) | Token của instance vast.ai | `?token=<EDGE_TOKEN>` trên URL | Chặn người lạ chạm vào máy test |
| 2. Ứng dụng | Bearer token của user | Header `Authorization: Bearer <APP_TOKEN>` | Xác định account/user sở hữu wardrobe |

Vì sao edge token đi ở **query param** chứ không phải header: cả hai lớp đều dùng header
`Authorization: Bearer`, nên chỉ một trong hai nhét vừa vào đó. Query param dành cho lớp
edge, header để dành cho token ứng dụng.

```
GET http://218.49.89.120:40123/v1/wardrobe/items?token=<EDGE_TOKEN>
Authorization: Bearer <APP_TOKEN>
```

**Ở Production bỏ hẳn `?token=`**, chỉ giữ header `Authorization`.

### 1.3 Token test hiện có

Instance test đã seed sẵn một account (thay cho hệ thống đăng nhập thật, chưa nằm trong
phạm vi bản test):

Hai token đều là **giá trị sống của instance test, cố tình không commit vào repo này** —
lấy trực tiếp trên máy chủ khi cần:

```bash
# APP_TOKEN — bearer token của account test đã seed
grep TEST_BEARER_TOKEN ${WORKSPACE:-/workspace}/.env

# EDGE_TOKEN — token của instance vast.ai (lớp Caddy)
echo $OPEN_BUTTON_TOKEN
```

Đặt chúng vào cấu hình build/biến môi trường của app, **không hard-code và không commit
vào repo app**. Token test bị lộ thì bất kỳ ai cũng ghi được vào wardrobe của account đó.

Tạo thêm account test:

```bash
cd /workspace/qie-outfit-comfyui-server && source /venv/main/bin/activate
set -a; . /workspace/.env; set +a
python -m server.data_server.scripts.seed_test_account "ten-account"
```

`user_id`/`account_id` **không bao giờ** truyền trong body hay query — server tự suy ra
từ token, để client không thể mạo danh người khác.

---

## 2. Luồng gọi tổng thể

```
 (mỗi lần quét thư viện ảnh — đây là toàn bộ việc Mobile phải làm)
 ┌─ POST /v1/uploads/presign    ──► batchId + N uploadUrl
 ├─ PUT  <uploadUrl> × N        (ảnh gốc ──► object-storage, 4 luồng song song)
 ├─ POST /v1/jobs               ──► jobId, status=pending
 ├─ GET  /v1/jobs/{jobId}       (lặp mỗi 5–10s cho tới completed)
 │   └─ POST /v1/jobs/{jobId}/cancel   (nếu user bấm huỷ)
 └─ GET  /v1/wardrobe/items?jobId=…    ──► danh sách trang phục + imageUrl
```

Ở phía server, mỗi ảnh đi qua D0b (tách đúng người bằng khuôn mặt) → D1 (vẽ lại trang
phục thành ảnh mockup) → D3 (gắn tag + sinh embedding) → D2 (khử trùng lặp) → D4 (ghi
database). Mobile không cần biết chi tiết, nhưng cần biết **một ảnh gốc sinh ra nhiều
item**, và **một số item sẽ bị loại vì trùng với đồ đã có trong tủ**.

---

## 3. Ảnh khuôn mặt tham chiếu — server test đã cố định, Mobile bỏ qua

**Mobile không phải làm gì ở bước này.** Đọc để hiểu server đang so khớp với khuôn mặt
nào, rồi chuyển thẳng xuống [mục 4](#4-upload-ảnh-và-tạo-job).

Với ảnh có nhiều người, server phải biết người nào là chủ tài khoản trước khi tách trang
phục — nếu không sẽ tạo ra item từ quần áo của người khác. Server so khuôn mặt bằng
ArcFace (`w600k_mbf`, ngưỡng cosine **0.35**).

Ở môi trường Test, server **cố định sẵn** ảnh `face-image/selfie.jpg` làm khuôn mặt tham
chiếu dùng chung cho mọi account (xem [`wardrobe-system-spec.md` §2.3.7](wardrobe-system-spec.md#237-ảnh-khuôn-mặt-tham-chiếu-cố-định-chỉ-môi-trường-test)).
Data-server tự upload ảnh này lên object-storage lúc khởi động và tự gắn vào mọi job, nên
**client không cần gọi endpoint nào** — bắt đầu luôn từ `POST /v1/uploads/presign`.

Hệ quả cần biết khi đọc kết quả test: mọi ảnh nhóm đều được so với **cùng một khuôn mặt**.
Ảnh nào không có người khớp khuôn mặt đó thì D0b lùi về "người lớn nhất khung hình", và
wardrobe sẽ nhận quần áo của người đó. Muốn test bằng khuôn mặt khác thì đổi
`TEST_FIXED_FACE_REF_IMAGE` trong `${WORKSPACE}/.env` rồi
`supervisorctl restart data-server`.

Kiểm tra server đang dùng ảnh nào:

```bash
curl -s "$BASE/v1/face-reference?token=$EDGE" -H "Authorization: Bearer $APP"
# {"registered":true,"objectKey":"face/_test_fixture/reference.jpg","source":"test-fixture"}
```

`source` cho biết ảnh nào đang có hiệu lực: `"test-fixture"` là ảnh cố định của môi trường
test, `"account"` là ảnh do chính account đăng ký, `null` là không có (D0b lùi về người lớn
nhất khung hình).

<details>
<summary>Ba endpoint đăng ký khuôn mặt — dành cho Production, không dùng ở bản test này</summary>

Ở Production mỗi user có ảnh khuôn mặt riêng, upload qua đúng bộ ba dưới đây. Ảnh do
account đăng ký luôn **thắng** ảnh cố định, nên có thể gọi để test riêng luồng này.

- **`POST /v1/face-reference/presign`** — body `{"contentType":"image/jpeg"}`, trả
  `{objectKey, uploadUrl, expiresAt}`. Upload giống hệt [mục 4.2](#42-upload-ảnh-lên-object-storage).
- **`PUT /v1/face-reference`** — body `{"objectKey":"..."}`, gọi **sau khi** PUT thành
  công. Server kiểm tra object thực sự tồn tại rồi mới ghi nhận, nên một lần upload hỏng
  không làm các job sau trỏ vào key rỗng.
- **`GET /v1/face-reference`** — như trên.

Gọi lại `presign` + `PUT` sẽ ghi đè ảnh cũ; mỗi account chỉ có đúng một ảnh tham chiếu.
Chọn ảnh rõ mặt, chính diện, chỉ một người. Ảnh đang dùng cho bản test đo được cosine
similarity **0.6028** với chủ thể trong ảnh nhóm — cao hơn hẳn ngưỡng 0.35.

</details>

## 4. Upload ảnh và tạo job

### 4.1 `POST /v1/uploads/presign`

Xin đường dẫn upload cho cả lô ảnh trong một lần gọi.

```jsonc
// request
{
  "items": [
    { "localId": "asset-IMG_9652", "contentType": "image/jpeg", "checksum": "optional-sha256" },
    { "localId": "asset-IMG_9822", "contentType": "image/jpeg" }
  ]
}

// response
{
  "batchId": "5245fa0b-351c-4075-85fa-819800e98395",
  "items": [
    {
      "localId":   "asset-IMG_9652",
      "objectKey": "raw/67180bc0-.../5245fa0b-.../asset-IMG_9652.jpg",
      "uploadUrl": "http://218.49.89.120:40142/wardrobe-raw/raw/...?X-Amz-Algorithm=...",
      "expiresAt": "2026-09-11T05:00:00+00:00"
    }
  ]
}
```

`localId` là **id do Mobile tự sinh** (nên dùng asset ID của hệ điều hành). Nó là sợi dây
duy nhất nối kết quả trả về với ảnh gốc trong thư viện, cần cho bước cập nhật index ảnh
đã quét. `contentType` hỗ trợ `image/jpeg`, `image/png`, `image/webp`, `image/heic`.

`uploadUrl` hết hạn sau **15 phút**. Lô rất lớn thì chia nhỏ thành nhiều lần presign thay
vì xin một lần rồi upload dần quá hạn.

### 4.2 Upload ảnh lên object-storage

`PUT` thẳng vào `uploadUrl`, body là **raw bytes** của file (không multipart, không form).

```http
PUT http://218.49.89.120:40142/wardrobe-raw/raw/...?X-Amz-Algorithm=...
Content-Type: image/jpeg
Cookie: C.51282346_auth_token=<EDGE_TOKEN>

<bytes ảnh>
```

Ba điểm bắt buộc, sai một cái là `403 SignatureDoesNotMatch`:

1. **`Content-Type` phải trùng đúng `contentType` đã khai ở bước presign.** Giá trị này
   nằm trong chữ ký.
2. **Không sửa URL.** Không thêm/bớt/sắp xếp lại query param, không encode lại. Mọi tham
   số `X-Amz-*` đều nằm trong chữ ký.
3. **Edge token ở môi trường Test phải đi bằng Cookie, không phải query param** — thêm
   `?token=` vào URL sẽ phá chữ ký. Tên cookie là `<VAST_CONTAINERLABEL>_auth_token`,
   trên instance hiện tại là `C.51282346_auth_token`. Production không cần cookie này.

Thành công trả `200` (body rỗng). Upload **4 ảnh song song** — mỗi lần upload chủ yếu là
chờ mạng chứ không tốn CPU, chạy tuần tự chỉ cộng dồn độ trễ.

### 4.3 `POST /v1/jobs`

Gọi sau khi đã upload xong. Chỉ liệt kê ảnh **upload thành công** — job được phép chỉ
chứa một phần batch, ảnh lỗi để lần quét sau thử lại.

```jsonc
// request
{
  "batchId": "5245fa0b-351c-4075-85fa-819800e98395",
  "uploadedItems": ["asset-IMG_9652", "asset-IMG_9822"]
}

// response
{
  "jobId": "e1f0b194-2110-4278-8462-1d6e5e9bee48",
  "status": "pending",
  "totalItems": 2,
  "createdAt": "2026-09-11T04:44:49.931466+00:00"
}
```

Nếu `uploadedItems` rỗng hoặc không khớp batch nào → `400`.

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
    { "localId": "asset-IMG_9822", "status": "success" },
    { "localId": "asset-IMG_0168", "status": "failed"  }
  ]
}
```

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

**Nhịp polling khuyến nghị:** 5–10s. Thời gian đo thực tế trên instance test (RTX 3090
24GB, toàn bộ model chạy GPU): **~24 giây mỗi ảnh**, gần như toàn bộ nằm ở bước D1. Lô 9
ảnh mất 3 phút 41 giây. Đừng polling dày hơn 5s — không có kết quả sớm hơn, chỉ tốn pin.

Con số này phụ thuộc phần cứng server, đừng hard-code timeout theo nó. Ảnh đầu tiên sau
khi server khởi động lại chậm hơn (~50s) vì model chưa nạp vào VRAM.

Mất mạng giữa chừng **không ảnh hưởng** việc xử lý ở server; mở app lại rồi hỏi tiếp
trạng thái là đủ, không mất tiến độ.

### 5.2 `POST /v1/jobs/{jobId}/cancel`

```jsonc
{ "status": "cancelling" }   // còn ảnh đang chạy dở
{ "status": "cancelled"  }   // đã dừng ngay được
```

Ảnh đang xử lý dở vẫn được chạy nốt (GPU đã tốn gần hết công rồi), ảnh chưa bắt đầu bị
bỏ qua và **đếm vào `failedItems`**. Kết quả đo thực tế trên job 3 ảnh: huỷ sau 5 giây →
`processedItems=1`, `failedItems=2`, status `cancelling` → `cancelled` sau khoảng 20s.
UI nên vẫn polling sau khi gửi lệnh huỷ, tới khi status thành `cancelled`.

### 5.3 Cập nhật index ảnh đã quét

Khi job `completed`, chỉ thêm asset ID của những ảnh có `items[].status == "success"` vào
index cục bộ. Ảnh `failed` **không** được thêm, để lần quét sau thử lại.

---

## 6. Lấy wardrobe

### `GET /v1/wardrobe/items`

| Query param | Kiểu | Mô tả |
|---|---|---|
| `jobId` | string, tuỳ chọn | Chỉ lấy item của một job |
| `cursor` | string, tuỳ chọn | Con trỏ phân trang, lấy từ `nextCursor` |
| `limit` | int, tuỳ chọn | Mặc định 50, tối đa 200 |

```jsonc
{
  "items": [
    {
      "imageUrl": "http://218.49.89.120:40142/wardrobe-items/items/...?X-Amz-Algorithm=...",
      "jobId": "e1f0b194-2110-4278-8462-1d6e5e9bee48",
      "tags": {
        "type": "shirts",
        "category": "clothing",
        "sub_category": "shirts",
        "gender": "men",
        "color":   [ { "color": "brown", "confidence": 80.9 },
                     { "color": "beige", "confidence": 70.0 } ],
        "neck":    ["spread collar"],
        "sleeve":  ["short sleeve"],
        "pattern": []
      }
    }
  ],
  "nextCursor": "a1b2c3d4-..."
}
```

Đọc tags cho đúng:

- `type`, `category`, `sub_category`, `gender` là **đơn nhãn** (string).
- `color` là **đa nhãn kèm độ tin cậy**, đã sắp giảm dần — lấy phần tử đầu làm màu chính.
- `neck`, `sleeve`, `pattern` là **mảng string, có thể rỗng** khi không áp dụng (quần
  không có kiểu tay áo). Luôn kiểm tra rỗng trước khi hiển thị.

Phân trang: còn `nextCursor` thì gọi tiếp với `cursor=<nextCursor>`; hết dữ liệu thì
trường này biến mất khỏi response.

**`imageUrl` là URL có chữ ký, hết hạn sau 1 giờ.** Đừng cache vào database cục bộ — cache
file ảnh tải về thì được, cache URL thì sẽ hỏng sau một tiếng. Ở môi trường Test, tải ảnh
này cũng cần gửi kèm Cookie edge token như [mục 4.2](#42-upload-ảnh-lên-object-storage).

**Ảnh trang phục là ảnh được vẽ lại trên nền trắng**, không phải vùng pixel cắt từ ảnh
gốc — không còn khuôn mặt hay hậu cảnh. Chi tiết hoa văn có thể lệch đôi chút so với đồ
thật; đủ dùng để nhận ra "cái áo hoa be tay ngắn", không dùng để đối chiếu chính xác
từng chi tiết sản phẩm. Ảnh luôn là **hình vuông**, nền trắng phẳng, món đồ nằm giữa
khung — an toàn để hiển thị thẳng trong lưới mà không cần crop thêm. Server tách món đồ
theo mặt nạ rồi dựng lại nền, nên mỗi ảnh chứa **đúng một món**, không còn sót dải của
món kế bên như bản trước.

### Vì sao số item ít hơn kỳ vọng

Server tự khử trùng lặp (D2): item mới có cosine similarity ≥ **0.90** với một item đã có
cùng `type` sẽ **không** được tạo bản ghi mới. Trên bộ test 9 ảnh: 26 crop sinh ra, 6 bị
khử, còn **20 item** trong tủ. Đây là hành vi đúng — cùng một chiếc áo chụp ở nhiều ảnh
chỉ nên xuất hiện một lần. Đừng coi chênh lệch giữa số ảnh upload và số item trả về là lỗi.

Trường hợp cực đoan đã đo được: upload lại **đúng một ảnh đã xử lý trước đó** thì job vẫn
`completed` với `processedItems=1`, nhưng `GET /v1/wardrobe/items?jobId=…` trả về **mảng
rỗng** — cả 4 món đều trùng đồ đã có (similarity 0.92–0.98). Đây chính là lưới an toàn cho
trường hợp index ảnh đã quét trên máy bị mất (user cài lại app, đổi máy): ảnh cũ bị quét
lại cũng không sinh ra item trùng. **UI không được coi "job xong nhưng wardrobe rỗng" là
lỗi** — hãy hiển thị "không có trang phục mới" thay vì báo thất bại.

---

## 7. Health check và lỗi

### `GET /v1/health` — không cần `Authorization`

```jsonc
{ "status": "ok",
  "dependencies": { "postgres": "ok", "objectStorage": "ok", "queue": "ok" } }
```

Ở môi trường Test vẫn cần `?token=<EDGE_TOKEN>` vì lớp edge chặn trước khi tới app.

### Mã lỗi

| Mã | Nguyên nhân | Xử lý phía Mobile |
|---|---|---|
| `401` (body rỗng/HTML) | Thiếu/sai **edge token** | Kiểm tra `?token=` |
| `401 {"detail":"missing bearer token"}` | Thiếu header `Authorization` | Gắn app token |
| `401 {"detail":"invalid token"}` | App token sai/đã xoá | Đăng nhập lại |
| `400` | `uploadedItems` không khớp batch, hoặc face-reference chưa upload | Không retry nguyên trạng |
| `403` (từ object-storage) | URL hết hạn hoặc chữ ký sai | Xin presign mới |
| `404` | `jobId` không tồn tại hoặc thuộc account khác | Không retry |
| `422` | Tham số sai kiểu/ngoài khoảng (ví dụ `limit=500` > 200) | Lỗi lập trình, sửa request |
| `5xx` | Lỗi server | Retry với exponential backoff |

Phân biệt hai loại `401` bằng body: lớp edge trả HTML/rỗng, app trả JSON có `detail`.

Ảnh lỗi lẻ tẻ trong job là **bình thường** — mỗi vé việc được thử lại tối đa 3 lần trước
khi đánh `failed`, và không ảnh hưởng ảnh khác cùng job.

---

## 8. Sai khác so với spec

Ba điểm khác [`wardrobe-system-spec.md`](wardrobe-system-spec.md) Phần 3, đều đã hiện thực
và kiểm chứng trên instance test:

1. **Nhóm endpoint `/v1/face-reference` là mới.** Spec mô tả D0b "đối chiếu khuôn mặt với
   embedding tham chiếu đã lưu của account" nhưng không định nghĩa API để đăng ký ảnh
   tham chiếu đó. Ba endpoint ở [mục 3](#3-ảnh-khuôn-mặt-tham-chiếu--server-test-đã-cố-định-mobile-bỏ-qua)
   lấp chỗ trống này, dùng lại đúng hình dạng presign + PUT của `/v1/uploads/presign`.
   Bản tin job queue vì thế có thêm trường `faceRefKey` (spec §3.3) — nội bộ server, Mobile
   không thấy. **Ở bản test hiện tại Mobile không dùng nhóm endpoint này**: server cố định
   sẵn ảnh khuôn mặt theo [spec §2.3.7](wardrobe-system-spec.md#237-ảnh-khuôn-mặt-tham-chiếu-cố-định-chỉ-môi-trường-test).

2. **Object-storage được publish ra ngoài ở môi trường Test.** Spec §2.3.4 nói chỉ
   data-server mở cổng, nhưng §1.2 D0 lại yêu cầu Mobile upload thẳng lên object-storage —
   trên một máy thì hai điều đó không thể cùng đúng. Object-storage được đặt sau cùng lớp
   token edge, và URL vẫn là presigned có hạn 15 phút. Đây là ràng buộc của môi trường
   test một máy; Production dùng dịch vụ managed nên không có vấn đề này.

3. **Hai lớp token chỉ tồn tại ở Test** ([mục 1.2](#12-hai-lớp-token-chỉ-môi-trường-test)).
   Spec chỉ mô tả một Bearer token; lớp edge là của hạ tầng vast.ai, không phải của hệ thống.

Ngoài ra, hệ thống đăng nhập/session mà spec giả định có sẵn **chưa được hiện thực** —
bản test dùng bearer token tĩnh seed sẵn trong database.

---

## 9. Ví dụ hoàn chỉnh

### 9.1 curl

```bash
BASE=http://218.49.89.120:40123
EDGE=<EDGE_TOKEN>
APP=$(grep TEST_BEARER_TOKEN ${WORKSPACE:-/workspace}/.env | cut -d= -f2)
COOKIE="C.51282346_auth_token=$EDGE"

# 0) health
curl -s "$BASE/v1/health?token=$EDGE"

# 1) (không bắt buộc) xem server đang dùng khuôn mặt tham chiếu nào
curl -s "$BASE/v1/face-reference?token=$EDGE" -H "Authorization: Bearer $APP"
# {"registered":true,"objectKey":"face/_test_fixture/reference.jpg","source":"test-fixture"}

# 2) presign cho ảnh cần xử lý
PRE=$(curl -s -X POST "$BASE/v1/uploads/presign?token=$EDGE" \
  -H "Authorization: Bearer $APP" -H "Content-Type: application/json" \
  -d '{"items":[{"localId":"asset-1","contentType":"image/jpeg"}]}')
BATCH=$(echo "$PRE" | jq -r .batchId)
UP=$(echo "$PRE" | jq -r '.items[0].uploadUrl')

# 3) upload ảnh
curl -s -X PUT --data-binary @photo.jpg -H "Content-Type: image/jpeg" --cookie "$COOKIE" "$UP"

# 4) tạo job
JOB=$(curl -s -X POST "$BASE/v1/jobs?token=$EDGE" \
  -H "Authorization: Bearer $APP" -H "Content-Type: application/json" \
  -d "{\"batchId\":\"$BATCH\",\"uploadedItems\":[\"asset-1\"]}")
JID=$(echo "$JOB" | jq -r .jobId)

# 5) polling
until [ "$(curl -s "$BASE/v1/jobs/$JID?token=$EDGE" -H "Authorization: Bearer $APP" \
          | jq -r .status)" = "completed" ]; do sleep 10; done

# 6) lấy wardrobe
curl -s "$BASE/v1/wardrobe/items?jobId=$JID&token=$EDGE" -H "Authorization: Bearer $APP" | jq .
```

### 9.2 Dart / Flutter

```dart
class WardrobeApi {
  WardrobeApi({required this.baseUrl, required this.appToken, this.edgeToken, this.edgeCookie});

  final String baseUrl;       // http://218.49.89.120:40123
  final String appToken;      // bearer token của user
  final String? edgeToken;    // chỉ Test; Production để null
  final String? edgeCookie;   // 'C.51282346_auth_token'

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

  /// PUT thẳng lên object-storage. KHÔNG sửa uploadUrl và KHÔNG thêm token vào
  /// query — mọi tham số X-Amz-* đều nằm trong chữ ký. Edge token đi bằng Cookie.
  Future<void> uploadBytes(String uploadUrl, Uint8List bytes, String contentType) async {
    final r = await http.put(
      Uri.parse(uploadUrl),
      body: bytes,
      headers: {
        'Content-Type': contentType,
        if (edgeCookie != null && edgeToken != null) 'Cookie': '$edgeCookie=$edgeToken',
      },
    );
    if (r.statusCode != 200) throw WardrobeApiException(r.statusCode, r.body);
  }

  Future<Map<String, dynamic>> presignUploads(List<({String localId, String contentType})> items) async =>
      _json(await http.post(_u('/v1/uploads/presign'), headers: _headers,
          body: jsonEncode({
            'items': [for (final i in items) {'localId': i.localId, 'contentType': i.contentType}]
          })));

  Future<Map<String, dynamic>> createJob(String batchId, List<String> uploadedItems) async =>
      _json(await http.post(_u('/v1/jobs'), headers: _headers,
          body: jsonEncode({'batchId': batchId, 'uploadedItems': uploadedItems})));

  Future<Map<String, dynamic>> getJob(String jobId) async =>
      _json(await http.get(_u('/v1/jobs/$jobId'), headers: _headers));

  Future<Map<String, dynamic>> cancelJob(String jobId) async =>
      _json(await http.post(_u('/v1/jobs/$jobId/cancel'), headers: _headers));

  Future<Map<String, dynamic>> wardrobeItems({String? jobId, String? cursor, int limit = 50}) async =>
      _json(await http.get(_u('/v1/wardrobe/items', {
        if (jobId != null) 'jobId': jobId,
        if (cursor != null) 'cursor': cursor,
        'limit': '$limit',
      }), headers: _headers));
}
```

Upload song song 4 ảnh một lúc:

```dart
const batchSize = 4;
final uploaded = <String>[];
for (var i = 0; i < items.length; i += batchSize) {
  final slice = items.skip(i).take(batchSize);
  await Future.wait(slice.map((it) async {
    try {
      await api.uploadBytes(it.uploadUrl, await readBytes(it.localId), 'image/jpeg');
      uploaded.add(it.localId);
    } catch (_) {
      // ảnh lỗi bị bỏ qua — lần quét sau thử lại, vì localId chưa vào index
    }
  }));
}
final job = await api.createJob(batchId, uploaded);
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

---

## 10. Checklist tích hợp

- [ ] Base URL nằm trong config build, không hard-code trong logic
- [ ] Edge token ở query param, app token ở header `Authorization`
- [ ] Upload dùng `PUT` raw bytes, `Content-Type` khớp lúc presign, **không sửa uploadUrl**
- [ ] Edge token khi upload đi bằng **Cookie**, không phải query param
- [ ] Upload 4 ảnh song song, `uploadedItems` chỉ chứa ảnh PUT thành công
- [ ] Polling 5–10s, dừng ở `completed`/`cancelled`/`failed`
- [ ] Chỉ thêm asset ID của ảnh `status == "success"` vào index đã quét
- [ ] `imageUrl` không cache quá 1 giờ (cache file ảnh, không cache URL)
- [ ] `neck`/`sleeve`/`pattern` kiểm tra mảng rỗng trước khi hiển thị
- [ ] Không gọi `/v1/face-reference` — server test đã cố định ảnh khuôn mặt ([mục 3](#3-ảnh-khuôn-mặt-tham-chiếu--server-test-đã-cố-định-mobile-bỏ-qua))
- [ ] UI giải thích được vì sao số item < số ảnh (D2 khử trùng lặp)
