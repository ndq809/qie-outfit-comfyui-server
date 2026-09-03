# Wardrobe System — Tài liệu tổng hợp

Tài liệu này gộp nội dung của 4 tài liệu trước đó (pipeline guide, kiến trúc server, kiến trúc test 1 máy, đặc tả API — nay đã gộp và xóa) thành một bản duy nhất, tổ chức theo hai trục: **Client (Mobile) / Server**, và trong phần Server tách tiếp theo **Production / Test**.

Quy ước đánh số: các mục lớn dùng số thập phân (1.1, 2.3.2...) để điều hướng tài liệu. Các mã **A0–A5, B1–B2, C1, D0–D4, E1–E2** là mã giai đoạn của pipeline gốc (Giai đoạn A đến E), chỉ xuất hiện như nhãn mô tả bước xử lý bên trong nội dung — không phải số mục của tài liệu này, để tránh nhầm lẫn thứ tự.

Lưu ý về hai thay đổi so với thiết kế gốc, đã phản ánh trong tài liệu này: (1) **thứ tự chạy thực tế trên mobile không theo thứ tự mã giai đoạn** — B1 chạy trước A2/A3/A4 (xem [1.1](#11-pipeline-xử-lý-on-device)); (2) **B2 và C1 không còn chạy trên mobile** — B2 bị bỏ hẳn, C1 chuyển sang server thành bước D0b (xem [2.1](#21-kiến-trúc-chung)).

## Mục lục

- [0. Mục tiêu và nguyên tắc thiết kế](#0-mục-tiêu-và-nguyên-tắc-thiết-kế)
- [Phần 1 — Client (Mobile)](#phần-1--client-mobile)
  - [1.1 Pipeline xử lý on-device](#11-pipeline-xử-lý-on-device)
  - [1.2 Tương tác với Server](#12-tương-tác-với-server)
  - [1.3 Cấu hình theo môi trường](#13-cấu-hình-theo-môi-trường)
  - [1.4 Công cụ kiểm thử pipeline trên desktop](#14-công-cụ-kiểm-thử-pipeline-trên-desktop)
- [Phần 2 — Server](#phần-2--server)
  - [2.1 Kiến trúc chung](#21-kiến-trúc-chung)
  - [2.2 Triển khai Production](#22-triển-khai-production)
  - [2.3 Triển khai Test (1 máy vast.ai)](#23-triển-khai-test-1-máy-vastai)
  - [2.4 Giấy phép model/dữ liệu](#24-giấy-phép-modeldữ-liệu)
- [Phần 3 — API Spec](#phần-3--api-spec)
  - [3.1 Data server API](#31-data-server-api)
  - [3.2 AI server API](#32-ai-server-api)
  - [3.3 Cấu trúc bản tin Job queue và Result queue](#33-cấu-trúc-bản-tin-job-queue-và-result-queue)

---

## 0. Mục tiêu và nguyên tắc thiết kế

Hệ thống nhận vào ba thiết lập của người dùng (khoảng thời gian quét, chế độ ảnh đơn/nhóm, dữ liệu khuôn mặt) và trả về một wardrobe gồm các item trang phục đã được tách, gắn tag — với quy mô mục tiêu hàng nghìn user, mỗi user khoảng 100 trang phục cần xử lý.

Ba nguyên tắc xuyên suốt:

- **Ưu tiên xử lý trên thiết bị (on-device):** chỉ đẩy dữ liệu lên server khi bắt buộc phải dùng model nặng (tách trang phục). Vừa giảm chi phí server, vừa bảo vệ dữ liệu riêng tư (ảnh cá nhân, khuôn mặt) ở mức tối đa có thể.
- **Lọc rẻ trước, xử lý đắt sau:** các bước không cần AI hoặc dùng model rất nhẹ luôn chạy trước để thu nhỏ tập ảnh, tránh lãng phí tính toán ở các bước AI nặng phía sau.
- **Server tách rời qua hàng đợi:** Data server và AI server không gọi thẳng vào nhau mà giao tiếp gián tiếp qua Object Storage và message queue, để mỗi bên scale độc lập và chịu được tải dồn cục.

---

## Phần 1 — Client (Mobile)

### 1.1 Pipeline xử lý on-device

Gồm giai đoạn A và B của pipeline gốc. Các giai đoạn này không phụ thuộc Production hay Test — logic xử lý giống nhau ở mọi môi trường vì chạy hoàn toàn trên thiết bị, không gọi server.

#### Thứ tự thực thi thực tế

Thứ tự chạy **không** theo đúng thứ tự mã giai đoạn (A0→A5→B1). Áp dụng nguyên tắc "lọc rẻ trước, xử lý đắt sau" ở [mục 0](#0-mục-tiêu-và-nguyên-tắc-thiết-kế), **B1 được đưa lên trước A2/A3/A4**:

```
A1 (quét theo thời gian) → A0 (loại ảnh đã quét)
  → B1 (gom cụm trùng lặp)
  → [mỗi cụm] A2 (chất lượng) + cổng lọc "có mặt người" giá rẻ
  → A3/A4 (đếm người + đối chiếu khuôn mặt, bản chính xác)
  → A5 (lọc theo chế độ đơn/nhóm)
```

Lý do: B1 là CV cổ điển (perceptual hash, không dùng model) nên rất rẻ; chạy trước nghĩa là các bước ML nặng phía sau chỉ chạy **một lần cho mỗi cụm** thay vì một lần cho mỗi ảnh — một loạt 8 ảnh burst gần giống nhau tốn chi phí bằng đúng 1 ảnh.

#### Nguyên tắc chung: xử lý trên thumbnail, không dùng ảnh gốc

Mọi bước phân tích ảnh (A2, B1, A3/A4) đều chạy trên **thumbnail do hệ điều hành sinh ra** (~1280px cạnh dài; riêng B1 dùng thumbnail nhỏ hơn ~240px vì dHash chỉ cần lưới 9x8), **không giải mã ảnh gốc**. Một ảnh gốc từ camera điện thoại có thể 10+ MB / 12+ MP — giải mã bằng decoder thuần Dart tốn hàng giây CPU mỗi ảnh và từng gây treo ANR trên thiết bị thật. Chỉ bước upload (D0) mới đọc file gốc.

Hệ quả cần lưu ý: mọi toạ độ (bounding box khuôn mặt) sinh ra ở các bước này nằm trong **hệ toạ độ của thumbnail**, không phải ảnh gốc — bên tiêu thụ phải tự quy đổi nếu cần dùng trên ảnh gốc.

**Giai đoạn A — Sàng lọc sơ bộ**

- *A0. Loại ảnh đã quét ở lần trước*: Mobile duy trì một index cục bộ (ví dụ SQLite/local storage trên thiết bị) lưu định danh ảnh (asset ID do hệ điều hành cấp, ổn định qua các lần quét) đã từng được xử lý thành công ở các lần quét trước. Ngay sau khi lấy danh sách ảnh theo khoảng thời gian ở A1, các ảnh đã có trong index này bị loại ngay, không đưa vào các bước lọc AI phía sau — tránh xử lý lại ảnh cũ, giảm đáng kể số ảnh cần xử lý ở các lần quét sau lần đầu tiên. Cách index này được cập nhật xem ở [1.2](#12-tương-tác-với-server) (bước E2).
- *A1. Quét ảnh theo khoảng thời gian*: truy vấn metadata thư viện ảnh của hệ điều hành (ngày chụp), thao tác dữ liệu thuần túy, không cần AI.
- *A2. Lọc ảnh không đủ điều kiện (tối, mờ, nhòe)*: dùng CV cổ điển — phương sai Laplacian để phát hiện mờ/nhòe, độ sáng trung bình để phát hiện ảnh quá tối. Không cần model. Chạy trên bản thu nhỏ tiếp (cạnh dài tối đa 256px) của thumbnail, dùng nội suy `nearest` — cả `average` lẫn thao tác grayscale đều có chi phí O(số pixel *nguồn*), nên nếu thumbnail vì lý do nào đó trả về full-resolution thì hai thao tác này đủ để treo UI isolate và kích hoạt ANR.
- *A3. Đếm số người*: **dùng ML Kit Face Detection, không dùng Object Detection**. Object Detector mặc định của ML Kit không có nhãn "person", nên số khuôn mặt phát hiện được đóng vai trò tín hiệu đếm người — chấp nhận được vì ảnh không có khuôn mặt nhìn thấy được thì cũng không thể đối chiếu với người dùng ở A4. Chạy chung một lần gọi detector với A4.
- *A4. Phát hiện khuôn mặt và đối chiếu với khuôn mặt người dùng*: face detection bằng Google ML Kit Face Detection (bật `enableLandmarks`); face recognition bằng **MobileFaceNet (InsightFace `w600k_mbf`)** chạy qua ONNX Runtime Mobile, so khớp bằng cosine similarity với embedding đã đăng ký ở màn hình đăng ký khuôn mặt (ngưỡng khớp **0.45**). Chi tiết tiền xử lý bắt buộc xem [Căn chỉnh khuôn mặt](#căn-chỉnh-khuôn-mặt-bắt-buộc-cho-mobilefacenet) bên dưới.
- *A5. Lọc theo chế độ ảnh đơn/nhóm*: dùng lại số người đã đếm ở A3 — "ảnh đơn" chỉ giữ ảnh đúng 1 người là chính chủ; "ảnh nhóm" giữ toàn bộ ảnh có mặt chính chủ.

**Cổng lọc hai tầng cho A3/A4**: A3/A4 bản chính xác là bước đắt nhất pipeline (detect + crop + embed cho từng khuôn mặt). Trước nó có một cổng lọc rẻ: chạy detector ở chế độ `fast` trên chính thumbnail mà A2 vừa dùng, chỉ cần biết "ảnh này có khả năng có mặt người không". Ảnh không qua cổng bị loại mà không tốn lần chạy embedding nào. Trong mỗi cụm B1, các thành viên được thử lần lượt và **giữ lại ảnh đầu tiên qua được cả A2 lẫn cổng lọc này** làm đại diện; cụm không có thành viên nào đạt sẽ bị loại hoàn toàn.

**Giai đoạn B — Khử trùng lặp**

- *B1. Gom cụm ảnh giống nhau bằng perceptual hashing*: dHash 64-bit (thu nhỏ về lưới xám 9x8, so sánh gradient ngang từng hàng), gom cụm theo khoảng cách Hamming với ngưỡng **`hammingThreshold = 30`** (trên tổng 64 bit).
  - Trước khi hash, ảnh được **chia nhóm theo thời gian chụp** (`burstTimeWindowSeconds = 240`, chỉ dùng metadata `createdAt`, miễn phí): ảnh trùng lặp/burst thật luôn được chụp gần nhau về thời gian, nên ảnh không có "hàng xóm thời gian" nào trong cửa sổ này chắc chắn không thể trùng với ảnh nào khác — bỏ qua hoàn toàn việc fetch + hash cho nó. Chỉ nhóm có từ 2 ảnh trở lên mới thực sự phải hash.
  - Hai hằng số trên là điểm điều chỉnh chính giữa tốc độ và độ chính xác: ngưỡng cao/cửa sổ rộng → gom mạnh hơn, ít ảnh phải qua A3/A4 hơn, nhưng tăng rủi ro gộp nhầm hai ảnh thực sự khác nhau (ảnh bị gộp nhầm sẽ **biến mất khỏi kết quả cuối** vì mỗi cụm chỉ giữ 1 đại diện).
  - **Cảnh báo triển khai**: dHash sinh ra bằng 64 lần dịch trái không mask nên bit dấu bật ngẫu nhiên ~50% số lần → giá trị hash là số âm rất thường xuyên. Khi đếm bit khác nhau phải dùng **dịch phải logic** (`>>>`), không dùng dịch phải số học (`>>`) — với số âm, dịch số học không bao giờ về 0 và gây vòng lặp vô hạn chiếm 100% CPU (đã từng gây ANR trên thiết bị thật, rất khó chẩn đoán vì không ném exception).
- *B2. Chọn ảnh đại diện theo độ phủ trang phục* — **đã gỡ khỏi mobile.** Sau khi B1 chuyển lên trước, mỗi cụm chỉ còn đúng một ứng viên đi tiếp (chọn theo A2 + cổng lọc rẻ), nên không còn gì để "chấm điểm và so sánh giữa nhiều ảnh trong cụm" nữa. Việc chọn đại diện bằng pose estimation (BlazePose) không còn được dùng.

**Giai đoạn C — Phân tích ảnh nhóm: đã chuyển sang server**

- *C1. Tách người dùng khỏi ảnh nhóm, bôi xám phần còn lại* — **không còn chạy trên mobile.** Đã thử triển khai on-device bằng MobileSAM rồi EdgeSAM (prompt bằng box thân người từ pose landmark), nhưng **chất lượng mask không đạt yêu cầu** trên ảnh thật. Trách nhiệm tách người trong ảnh nhóm được chuyển sang server, nơi có thể dùng model mạnh hơn không bị giới hạn tài nguyên thiết bị (xem [2.1](#21-kiến-trúc-chung)).
- Hệ quả: ảnh nhóm được **upload nguyên bản, chưa bôi xám**. Đây là một đánh đổi có ý thức so với thiết kế ban đầu — dữ liệu khuôn mặt người khác trong ảnh nhóm sẽ rời khỏi thiết bị, nên chính sách vòng đời dữ liệu và phạm vi truy cập ở phía server (xem [Bảo mật và vòng đời dữ liệu](#bảo-mật-và-vòng-đời-dữ-liệu)) trở thành lớp bảo vệ chính thay vì việc bôi xám tại nguồn.

#### Căn chỉnh khuôn mặt (bắt buộc cho MobileFaceNet)

MobileFaceNet thuộc dòng ArcFace/InsightFace, **được huấn luyện trên ảnh khuôn mặt đã căn chỉnh theo 5 điểm mốc**, không phải trên ảnh cắt thô theo bounding box. Trước khi đưa vào model, cả lúc đăng ký khuôn mặt lẫn lúc đối chiếu ở A4 đều phải:

1. Lấy 5 landmark từ ML Kit: mắt trái, mắt phải, chân mũi, khoé miệng trái, khoé miệng phải.
2. Ước lượng phép biến đổi similarity (xoay + tỉ lệ + tịnh tiến) đưa 5 điểm đó về đúng template chuẩn ArcFace 112x112.
3. Warp ảnh theo phép biến đổi đó để ra ảnh 112x112 đã căn chỉnh, rồi mới trích embedding.

Nếu bỏ qua bước này và chỉ cắt theo bounding box + padding, độ tương đồng cosine của **cùng một người** bị kéo tụt xuống dưới ngưỡng khớp một cách hệ thống (đo được: mọi cặp đúng đều dưới 0.45, tức là tỉ lệ nhận diện đúng gần như bằng 0). Phép biến đổi similarity có thể giải bằng công thức đóng dạng bình phương tối thiểu trên số phức, không cần thư viện SVD.

Hai điểm cần lưu ý khi triển khai:

- **Cả hai đầu so sánh phải dùng cùng một cách căn chỉnh**: embedding tham chiếu (lúc đăng ký) và embedding ứng viên (lúc quét) phải qua đúng cùng một pipeline căn chỉnh, nếu không sẽ lệch hệ thống dù thuật toán đúng.
- **Độ phân giải đầu vào của detector ảnh hưởng độ chính xác landmark**: chạy detector thẳng trên ảnh gốc rất lớn cho landmark kém chính xác hơn so với chạy trên bản đã thu nhỏ (~1280px) — nên đăng ký khuôn mặt cũng nên detect ở cùng cỡ thumbnail như lúc quét.

#### Ràng buộc triển khai đã kiểm chứng

- **Không dùng ONNX quantize INT8 động** cho các model chạy on-device: `quantize_dynamic` sinh ra toán tử `ConvInteger`, mà **ONNX Runtime Mobile không có kernel cho toán tử này** — model tải được nhưng ném lỗi ngay khi tạo session (`Could not find an implementation for ConvInteger(10)`). Kiểm định độ chính xác bằng Python trên máy desktop **không phát hiện được** lỗi này vì bản ONNX Runtime desktop có đầy đủ kernel. Nếu vẫn muốn giảm kích thước model, phải kiểm chứng trực tiếp trên thiết bị/emulator trước khi tin tưởng.
- **Cẩn thận với hàm lọc ảnh sửa đổi tại chỗ**: một số hàm của thư viện xử lý ảnh (ví dụ `grayscale()` của `package:image`) **sửa trực tiếp lên object truyền vào và trả về chính reference đó**, không tạo bản sao. Gọi một hàm như vậy trên ảnh mà phía gọi còn dùng lại về sau (để lưu, upload, hoặc xử lý tiếp) sẽ âm thầm làm hỏng ảnh đó — đã từng khiến ảnh khuôn mặt lưu lên server bị đen trắng trong khi embedding vẫn đúng (vì embedding tính từ object khác). Luôn clone trước khi gọi nếu ảnh gốc còn cần dùng.

### 1.2 Tương tác với Server

Gồm giai đoạn D (góc nhìn từ Mobile, ký hiệu D0) và giai đoạn E (E1, E2) của pipeline gốc. Sau khi hoàn tất giai đoạn A–B, Mobile chuyển sang giao tiếp với Data server để đẩy ảnh lên xử lý và theo dõi kết quả. Toàn bộ tương tác này đi qua các API public của Data server (chi tiết input/output ở [Phần 3](#phần-3--api-spec)); Mobile không bao giờ gọi trực tiếp AI server.

**D0. Upload ảnh và tạo job xử lý**
1. Mobile gọi `POST /v1/uploads/presign` xin đường dẫn upload có thời hạn cho từng ảnh. Data server tự xác định `user_id`/`account_id` từ session hiện tại (token đăng nhập) để biết wardrobe kết quả thuộc về ai — Mobile không cần tự truyền hai giá trị này.
2. Mobile upload ảnh thẳng lên Object Storage bằng đường dẫn đó — không đi qua Data server hay AI server, tránh biến hai server thành nút nghẽn băng thông. Ảnh gửi lên là **ảnh gốc chưa qua bôi xám** (xem giai đoạn C ở [1.1](#11-pipeline-xử-lý-on-device): việc tách người trong ảnh nhóm đã chuyển sang server). Đây là bước duy nhất trong pipeline đọc file ảnh gốc.
3. Vì mỗi lần upload chủ yếu là chờ độ trễ mạng chứ không tốn CPU, Mobile upload **nhiều ảnh song song theo lô** (mặc định 4 ảnh cùng lúc) thay vì tuần tự từng ảnh, để chồng lấn độ trễ thay vì cộng dồn.
4. Sau khi upload xong, Mobile gọi `POST /v1/jobs` để Data server tạo batch job (gắn với user/account của session hiện tại) và đẩy vé việc vào hàng đợi cho AI server xử lý (chi tiết xử lý phía server xem [Luồng xử lý D0→D4](#luồng-xử-lý-d0d4) ở mục 2.1).

**E1. Theo dõi tiến độ và hiển thị wardrobe**
Việc ghi wardrobe vào database đã do Data server thực hiện ở cuối giai đoạn D (không phải Mobile). Mobile chỉ cần:
- Gọi định kỳ `GET /v1/jobs/{jobId}` (hoặc nhận thông báo đẩy) để theo dõi tiến độ.
- Khi job hoàn tất, gọi `GET /v1/wardrobe/items` để tải dữ liệu wardrobe mới về hiển thị (Data server tự lọc theo user/account của session hiện tại).
- Nếu người dùng muốn dừng giữa chừng, gọi `POST /v1/jobs/{jobId}/cancel`.

**E2. Cập nhật index ảnh đã quét**
Khi job chuyển sang "completed", Mobile đối chiếu danh sách kết quả theo từng ảnh (trường `items[]` trong response của `GET /v1/jobs/{jobId}`, xem [3.1](#31-data-server-api)) để biết ảnh nào xử lý thành công. Chỉ những ảnh thành công mới được thêm asset ID vào index cục bộ mô tả ở [1.1](#11-pipeline-xử-lý-on-device) (bước A0); ảnh xử lý lỗi không được thêm vào, để lần quét sau thử lại. Vì index chỉ tồn tại cục bộ, nếu người dùng gỡ app hoặc đổi thiết bị, index mất và ảnh cũ có thể bị quét lại — cơ chế khử trùng lặp D2 ở server đóng vai trò lưới an toàn cho trường hợp này, tránh tạo item trùng dù ảnh bị xử lý lại.

**Xử lý gián đoạn phía Mobile**: nếu mất mạng hoặc app bị tắt giữa lúc đang upload, chỉ ảnh đã upload xong mới có vé việc; khi mở lại app, Mobile hỏi lại những ảnh nào đã upload thành công để chỉ tải nốt phần thiếu, không tạo lại cả batch. Nếu mất kết nối trong lúc chờ kết quả, quá trình xử lý ở server vẫn tiếp tục bình thường; Mobile chỉ cần hỏi lại trạng thái job khi hoạt động trở lại, không mất tiến độ đã xử lý xong.

### 1.3 Cấu hình theo môi trường

Toàn bộ API và luồng gọi ở trên giống hệt nhau giữa Production và Test — điểm khác duy nhất Mobile cần cấu hình là **base URL** của Data server:

| Môi trường | Base URL của Data server |
|---|---|
| Production | Domain thật của Data server (qua load balancer/DNS) |
| Test | IP public của máy vast.ai, port 8080 (xem [2.3.2](#232-mạng-nội-bộ-và-bảng-phân-bổ-ip)) |

Base URL nên đặt trong cấu hình build/môi trường của app, không hard-code, để chuyển giữa Test và Production chỉ cần đổi 1 giá trị.

### 1.4 Công cụ kiểm thử pipeline trên desktop

Chạy thử pipeline trên thiết bị/emulator rất chậm (một lượt quét ~900 ảnh mất khoảng 15 phút, cộng thêm thời gian build/cài mỗi lần đổi tham số), khiến việc tinh chỉnh các ngưỡng ở [1.1](#11-pipeline-xử-lý-on-device) trở nên tốn kém. Vì vậy có thêm một script Python (`StylistAI_App/test_assets/test_pipeline.py`) **mô phỏng lại đúng các bước A1 → A0 → B1 → A2+cổng lọc → A3/A4 → A5** với cùng các hằng số ngưỡng, chạy trên máy desktop với một thư mục ảnh bất kỳ và một ảnh khuôn mặt tham chiếu. Script in ra số ảnh còn lại sau từng giai đoạn kèm thời gian, copy các ảnh được chọn ra một thư mục để xem trực tiếp, và ghi báo cáo JSON kèm điểm similarity từng ảnh.

Dùng để: kiểm chứng nhanh tác động của việc đổi ngưỡng (`hammingThreshold`, `burstTimeWindowSeconds`, ngưỡng similarity), và đối chiếu xem một ảnh cụ thể bị loại ở bước nào.

**Giới hạn cần biết khi đọc kết quả** — script không thay thế được test trên thiết bị thật:

- Dùng **OpenCV YuNet** thay cho ML Kit Face Detection (ML Kit chỉ có trên Android/iOS), nên độ nhạy phát hiện khuôn mặt khác với app thật.
- "Thumbnail" là ảnh resize bằng thư viện Python, không phải thumbnail do hệ điều hành sinh ra.
- Không mô phỏng A0 (index đã quét) và giới hạn khoảng thời gian của A1 — mọi ảnh trong thư mục đều là ứng viên.
- Dùng chung file model MobileFaceNet mà app đóng gói, và **cùng cách căn chỉnh 5 điểm mốc** như mô tả ở [1.1](#11-pipeline-xử-lý-on-device), nên phần embedding/so khớp là sát với app thật nhất.

---

## Phần 2 — Server

### 2.1 Kiến trúc chung

Áp dụng cho cả Production và Test — chỉ khác nhau ở cách triển khai vật lý (xem [2.2](#22-triển-khai-production) và [2.3](#23-triển-khai-test-1-máy-vastai)).

#### Thành phần chính

- **Data server**: cấp quyền upload, tạo và theo dõi job, là chủ sở hữu duy nhất của database. Hiện thực toàn bộ API public.
- **AI server (worker pool)**: liên tục lấy việc từ job queue, xử lý D1–D3 nối tiếp trên cùng một ảnh, rồi báo kết quả qua result queue. Không có API nghiệp vụ, không đụng trực tiếp vào database.
- **Object Storage**: kho lưu ảnh gốc chờ xử lý và ảnh trang phục đã tách. Mobile và AI server đọc/ghi trực tiếp bằng đường dẫn có giới hạn quyền và thời hạn.
- **Message Queue (job queue)**: nơi Data server đặt vé việc cho AI server lấy về làm.
- **Result Queue**: nơi AI server đặt kết quả xử lý để Data server tiêu thụ và ghi vào database.

Nguyên tắc xuyên suốt: **hai server không gọi thẳng vào nhau**, chỉ giao tiếp gián tiếp qua Object Storage và hai hàng đợi trên — không bên nào phụ thuộc trực tiếp schema hay uptime của bên kia.

#### Luồng xử lý D0→D4

1. **D0 — Upload & tạo job** (Mobile khởi tạo, xem [1.2](#12-tương-tác-với-server)): Data server cấp đường dẫn upload qua `POST /v1/uploads/presign`, Mobile upload thẳng lên Object Storage, rồi Data server tạo job qua `POST /v1/jobs` và đẩy một vé việc/ảnh vào job queue (chỉ chứa đường dẫn ảnh, không chứa nội dung ảnh).
2. **D0b — Tách người dùng khỏi ảnh nhóm** (trách nhiệm mới, trước đây là C1 trên mobile): với ảnh chụp ở chế độ ảnh nhóm, AI server phải cô lập đúng người dùng trước khi tách trang phục, nếu không sẽ tạo ra item từ quần áo của người khác trong ảnh. Bước này chuyển từ mobile sang server vì các model segmentation đủ nhẹ để chạy on-device (MobileSAM, EdgeSAM) cho chất lượng mask không đạt yêu cầu trên ảnh thật; ở server có thể dùng model mạnh hơn không bị giới hạn tài nguyên thiết bị. Do ảnh gửi lên **chưa được bôi xám**, server cần tự xác định đâu là người dùng — cách khả dĩ là đối chiếu khuôn mặt với embedding tham chiếu đã lưu của account (cùng loại embedding mà mobile dùng ở A4), rồi dùng vị trí đó làm prompt cho model segmentation.
3. **D1 — Tách trang phục**: model fashion parsing/clothes segmentation chuyên sâu, cần GPU, không phù hợp chạy trên mobile. Từ 1 ảnh gốc, các trang phục tách ra nằm trên cùng 1 ảnh output nên cần crop để tách riêng từng trang phục.
4. **D2 — Khử trùng lặp giữa các ảnh trang phục đã tách**: nhiều item có thể trùng nhau (cùng một áo xuất hiện ở nhiều ảnh gốc). Dùng embedding tương đồng (CLIP, MIT license) tính cosine similarity giữa các ảnh trang phục để lọc; nếu muốn nhẹ hơn, dùng lại perceptual hashing như B1. Bước này càng quan trọng hơn sau khi B1 trên mobile được nới ngưỡng gom cụm (xem [1.1](#11-pipeline-xử-lý-on-device)) — mobile ưu tiên gom mạnh để giảm tải, phần trùng lặp còn sót lại do server bắt.
5. **D3 — Gắn tag phân loại**: model classification đa nhãn nhận diện loại trang phục, kiểu tay áo, kiểu cổ áo, màu sắc, họa tiết... Chạy ngay sau D1 trên cùng AI server để tránh tải ảnh lên/xuống lần nữa.
6. **D4 — Ghi wardrobe vào database (Data server)**: AI server ghi ảnh trang phục kết quả lên Object Storage rồi đặt kết quả (đường dẫn ảnh + tag) vào result queue — không ghi thẳng database. Data server tiêu thụ result queue để ghi wardrobe vào database và cập nhật tiến độ job (phản ánh qua `GET /v1/jobs/{jobId}`).

Cấu trúc bản tin của job queue và result queue được định nghĩa chi tiết ở [3.3](#33-cấu-trúc-bản-tin-job-queue-và-result-queue).

#### Khả năng chịu tải và mở rộng

Vì khối lượng job có thể tăng đột biến (user mới quét cả thư viện ảnh cùng lúc), hàng đợi đóng vai trò bộ đệm giữa hai server: Data server tạo job nhanh mà không cần chờ AI server xử lý xong; AI server tự điều chỉnh số lượng worker dựa trên độ dài hàng đợi thay vì theo số user đang hoạt động. Hàng đợi dài → thêm worker GPU; hàng đợi ngắn → thu nhỏ worker để tiết kiệm chi phí, kể cả dùng máy GPU giá rẻ có thể bị thu hồi (vì đã có cơ chế thử lại khi gián đoạn).

#### Xử lý lỗi

Mỗi vé việc trong hàng đợi được thử lại độc lập nếu xử lý thất bại (ảnh lỗi, model timeout, worker crash), không ảnh hưởng ảnh khác trong cùng batch. Sau một số lần thất bại liên tiếp, vé việc chuyển sang hàng đợi riêng để xem xét thủ công thay vì lặp vô hạn. Vì vé việc chỉ chứa tham chiếu ảnh, việc thử lại không tốn thêm băng thông.

#### Bảo mật và vòng đời dữ liệu

Đường dẫn upload và đọc ảnh đều có thời hạn, giới hạn theo từng user, không dùng đường dẫn công khai vĩnh viễn. Ảnh gốc chờ xử lý trong Object Storage nên có chính sách tự động xóa sau một khoảng thời gian ngắn kể từ khi xử lý xong, để giảm thiểu dữ liệu riêng tư lưu trữ ngoài thiết bị của user.

**Mức độ quan trọng đã tăng lên** kể từ khi bước bôi xám (C1) chuyển từ mobile sang server: trước đây ảnh nhóm rời khỏi thiết bị đã được che khuôn mặt/thân người khác, nay **ảnh nhóm nguyên bản** được upload. Nghĩa là lớp bảo vệ dữ liệu của người thứ ba trong ảnh giờ nằm hoàn toàn ở phía server, không còn được bảo vệ "tại nguồn" nữa. Hai yêu cầu tối thiểu: (1) ảnh gốc phải bị xóa ngay sau khi tách trang phục xong, không giữ lại quá thời gian cần thiết cho retry; (2) chỉ ảnh trang phục đã tách (không còn khuôn mặt) mới được lưu dài hạn trong wardrobe.

#### Hủy xử lý và sự cố thiết bị giữa chừng

Nguyên tắc quan trọng: một khi ảnh đã upload xong và vé việc đã vào hàng đợi, việc xử lý ở AI server hoàn toàn độc lập với tình trạng kết nối/hoạt động của thiết bị Mobile.

- **Người dùng chủ động hủy**: Mobile gọi `POST /v1/jobs/{jobId}/cancel`, Data server đánh dấu batch job "đang hủy". Không cần rút message khỏi Message Queue — AI server kiểm tra trạng thái batch trước khi xử lý mỗi vé việc, bỏ qua ngay nếu đã bị đánh dấu hủy. Ảnh đang xử lý dở khi lệnh hủy đến vẫn được cho hoàn tất (công sức GPU đã gần như bỏ ra hết). Khi hết vé việc đang chạy, Data server chuyển trạng thái "đã hủy" và dọn ảnh gốc còn sót trong Object Storage.
- **Batch job bị bỏ quên**: nếu Mobile không bao giờ quay lại hỏi trạng thái (gỡ app, đổi máy...), batch job và ảnh gốc liên quan cần có thời hạn tồn tại tối đa; quá hạn, hệ thống tự dọn ảnh gốc và đánh dấu job hết hạn.

(Xem thêm cách Mobile xử lý gián đoạn khi đang upload/đang chờ kết quả ở [1.2](#12-tương-tác-với-server).)

#### Vì sao chọn kiến trúc hàng đợi

Lựa chọn thay thế đơn giản hơn là để AI server gọi thẳng một API của Data server ngay khi xử lý xong từng ảnh. Cách đó dễ hiểu hơn nhưng có hai vấn đề ở quy mô hàng trăm nghìn ảnh: Data server dễ quá tải bởi lượng gọi API dồn dập, và nếu Data server tạm thời không phản hồi được thì kết quả xử lý của AI server có nguy cơ mất. Kiến trúc hàng đợi đánh đổi một chút độ trễ (Mobile cần theo dõi tiến độ thay vì nhận kết quả tức thời) để lấy khả năng chịu tải đột biến, tự phục hồi sau lỗi, và cho phép hai server scale hoàn toàn độc lập.

### 2.2 Triển khai Production

Ở quy mô thật (hàng nghìn user), các thành phần ở [2.1](#21-kiến-trúc-chung) nên nằm trên các máy/dịch vụ vật lý tách biệt:

- **AI server**: một hoặc nhiều máy GPU riêng, autoscale số lượng worker theo độ dài job queue thật (không giới hạn bởi tài nguyên của 1 máy như ở Test).
- **Object Storage**: nên dùng dịch vụ managed (S3, GCS...) thay vì tự vận hành, để có sẵn khả năng chịu lỗi và chính sách vòng đời dữ liệu.
- **postgres-db và queue**: có thể đặt gần Data server (cùng khu vực mạng) để giảm độ trễ giữa hai thành phần này.
- **Khả năng chịu lỗi phần cứng**: vì nhiều máy vật lý độc lập, một máy gặp sự cố không làm sập toàn hệ thống — khác với môi trường Test (xem [2.3.5](#235-khác-biệt-so-với-production)).

Thứ tự ưu tiên khi tách dần từ môi trường Test sang Production: tách **ai-server** ra máy GPU riêng trước (phần đắt và nặng tải nhất), rồi chuyển **object-storage** sang dịch vụ managed.

### 2.3 Triển khai Test (1 máy vast.ai)

Mục tiêu: mô phỏng đúng kiến trúc ở [2.1](#21-kiến-trúc-chung) nhưng gói gọn trên một máy vast.ai duy nhất để test nhanh, tiết kiệm chi phí thuê nhiều máy GPU. Mỗi thành phần vẫn chạy như một "server" độc lập, có địa chỉ IP nội bộ riêng và chỉ gọi nhau qua IP:port — không dùng chung tiến trình, không import code chéo nhau. Nhờ vậy khi tách ra nhiều máy thật, việc cần làm chỉ là đổi giá trị IP trong file cấu hình, không phải viết lại logic.

#### 2.3.1 Giả định về máy vast.ai

Thuê một instance có GPU (phục vụ AI server) và hỗ trợ chạy Docker bên trong — chọn image/template của vast.ai có sẵn Docker, hoặc image Ubuntu có quyền root đầy đủ để tự cài Docker. Nếu instance vốn dĩ tự nó đã là một container không hỗ trợ chạy container lồng bên trong, cần đổi sang loại template khác có hỗ trợ trước khi bắt đầu — đây là điều kiện tiên quyết cần xác nhận đầu tiên.

Máy có một địa chỉ IP public duy nhất do vast.ai cấp, dùng để Mobile/Postman gọi vào khi test. Các thành phần nội bộ dùng IP riêng trong mạng ảo do Docker tạo ra, không lộ ra ngoài.

#### 2.3.2 Mạng nội bộ và bảng phân bổ IP

Tạo một Docker network riêng dạng bridge với subnet cố định (ví dụ 172.28.0.0/24), gán IP tĩnh cho từng container thay vì để Docker tự cấp ngẫu nhiên — để file cấu hình của từng thành phần trỏ đích danh một địa chỉ IP cố định, giống hệt cách sẽ làm khi các thành phần nằm trên các máy vật lý khác nhau.

| Thành phần | Vai trò | IP nội bộ | Port | Expose ra ngoài? |
|---|---|---|---|---|
| data-server | API cho Mobile, quản lý job, ghi database | 172.28.0.10 | 8080 | Có — map ra IP public của vast.ai |
| ai-server | Worker xử lý D1–D3, cần GPU | 172.28.0.20 | 8090 (chỉ health check nội bộ) | Không |
| postgres-db | Database chính (user, job, wardrobe) | 172.28.0.30 | 5432 | Không |
| object-storage (MinIO) | Lưu ảnh gốc và ảnh trang phục đã tách | 172.28.0.40 | 9000 (API), 9001 (console quản trị) | Không, trừ khi tạm mở console để debug thủ công |
| queue (Redis) | Job queue và Result queue (hai danh sách riêng trong cùng một Redis) | 172.28.0.50 | 6379 | Không |

#### 2.3.3 Cấu hình gọi nhau giữa các thành phần

**data-server** cần biết địa chỉ của: postgres-db (ghi wardrobe, cập nhật trạng thái job), object-storage (tạo đường dẫn upload/tải có thời hạn cho Mobile), queue (đẩy vé việc vào job queue, đọc kết quả từ result queue). Đây là thành phần duy nhất có quyền ghi vào database, và là nơi hiện thực toàn bộ API public ở [3.1](#31-data-server-api).

**ai-server** cần biết địa chỉ của: object-storage (tải ảnh về xử lý, ghi ảnh kết quả lên) và queue (lấy vé việc từ job queue, đẩy kết quả vào result queue). ai-server không cần và không nên biết địa chỉ của postgres-db — giữ đúng nguyên tắc AI server không đụng trực tiếp vào database, tách rời hoàn toàn khỏi Data server dù đang chạy chung một máy vật lý.

Mỗi thành phần đọc các địa chỉ IP:port trên từ file cấu hình môi trường riêng của nó, không hard-code trong logic xử lý.

#### 2.3.4 Expose ra bên ngoài để test từ Mobile

Chỉ data-server cần mở cổng ra ngoài (map port 8080 nội bộ ra một port trên IP public của vast.ai) để app Mobile hoặc Postman gọi vào trong lúc test. Các thành phần còn lại (ai-server, postgres-db, object-storage, queue) chỉ lắng nghe trong mạng nội bộ Docker, không map port ra ngoài — đúng nguyên tắc AI server và Object Storage không nên lộ trực tiếp ra internet, kể cả trong môi trường test.

#### 2.3.5 Khác biệt so với Production

Đây là một máy vật lý duy nhất nên không có khả năng chịu lỗi phần cứng thật — nếu máy gặp sự cố, mọi thành phần dừng theo. Chấp nhận được cho môi trường test nhưng không dùng cho production.

Không có autoscale GPU thật vì chỉ có một máy; nếu muốn giả lập worker pool nhiều tiến trình, có thể chạy song song nhiều tiến trình worker trong cùng container ai-server, tất cả cùng đọc chung một job queue.

#### 2.3.6 Checklist triển khai cho AI agent

1. Xác nhận instance vast.ai hỗ trợ chạy Docker/Docker Compose bên trong; nếu không, đổi template trước khi tiếp tục — không cố chạy tiếp nếu bước này chưa xác nhận được.
2. Cài Docker và Docker Compose trên máy nếu chưa có sẵn.
3. Tạo Docker network riêng với subnet cố định như bảng ở [2.3.2](#232-mạng-nội-bộ-và-bảng-phân-bổ-ip).
4. Khởi tạo lần lượt postgres-db, object-storage, queue — gán đúng IP tĩnh cho từng container, kiểm tra từng dịch vụ chạy healthy trước khi sang bước tiếp theo.
5. Build và chạy ai-server, cấu hình trỏ tới IP của object-storage và queue theo bảng 2.3.2; xác nhận container nhận diện đúng GPU của máy.
6. Build và chạy data-server, cấu hình trỏ tới IP của postgres-db, object-storage, queue; map port ra IP public của vast.ai.
7. Test toàn bộ luồng: gọi `POST /v1/uploads/presign` xin đường dẫn upload có thời hạn → upload ảnh thẳng lên object-storage → gọi `POST /v1/jobs` tạo job → gọi `GET /v1/jobs/{jobId}` để xác nhận ai-server đã nhận vé việc từ queue và đang xử lý → xác nhận ai-server ghi ảnh kết quả lên object-storage và đẩy đúng cấu trúc bản tin vào result queue → gọi lại `GET /v1/jobs/{jobId}` xác nhận status chuyển "completed" → gọi `GET /v1/wardrobe/items` xác nhận data-server đã ghi được wardrobe vào postgres-db. Tiện thể thử `POST /v1/jobs/{jobId}/cancel` trên một job khác đang chạy để xác nhận cơ chế hủy hoạt động đúng.
8. Ghi lại toàn bộ giá trị IP/port đã dùng vào một file cấu hình môi trường mẫu, để khi tách sang nhiều máy thật sau này chỉ cần thay giá trị, không cần sửa code nghiệp vụ.

### 2.4 Giấy phép model/dữ liệu

Nhiều model thời trang chất lượng cao trên các nền tảng nghiên cứu (Hugging Face, GitHub) được huấn luyện trên các bộ dữ liệu chỉ cấp phép phi thương mại (DeepFashion, DeepFashion2, ModaNet...). Trước khi đưa bất kỳ model self-host nào (dùng ở D1–D3) vào production thương mại, cần kiểm tra kỹ:

- Giấy phép của kiến trúc/mã nguồn model.
- Giấy phép của bộ trọng số (weights) đã huấn luyện sẵn — thường bị ràng buộc bởi giấy phép của dữ liệu huấn luyện, khác với giấy phép của code.
- Nếu không chắc chắn, ưu tiên dùng các API thương mại đã có hợp đồng/điều khoản sử dụng rõ ràng, hoặc tự huấn luyện lại trên dữ liệu do mình sở hữu/license hợp lệ.

#### Kết quả rà soát các model segmentation đã khảo sát (cho bước tách người trong ảnh nhóm)

Các model dưới đây đã được rà soát khi còn triển khai C1 trên mobile; kết luận vẫn dùng được khi chọn model cho bước tương ứng ở server:

| Model | Giấy phép | Kết luận |
|---|---|---|
| MobileSAM | MIT (bản export ONNX của `Acly/MobileSAM`) | Dùng thương mại tự do. Đã tích hợp thử on-device rồi gỡ vì chất lượng mask không đạt. |
| **EdgeSAM** | **NTU S-Lab License 1.0 — chỉ phi thương mại** | **Không dùng được cho sản phẩm thương mại nếu chưa xin phép tác giả.** Giấy phép ghi rõ muốn dùng thương mại phải liên hệ trực tiếp nhóm tác giả (S-Lab, NTU). Mọi bản weights/ONNX cộng đồng đều kế thừa giấy phép này — người upload lại không có quyền cấp phép lại. |
| RepViT-SAM | Apache-2.0 (repo gốc THU-MIG/RepViT) | Giấy phép ổn, nhưng repo chính thức **không cung cấp bản ONNX nào** (chỉ có checkpoint PyTorch + notebook export CoreML); các file ONNX trên HuggingFace đều là bản convert cá nhân, không có model card/tài liệu. |

Hai bài học rút ra khi chọn model:

- **Kiểm tra file LICENSE thật, không tin nhãn trên trang model**: đã gặp trường hợp một bản upload lại trên HuggingFace gắn nhãn "MIT" cho model mà repo gốc cấp phép phi thương mại.
- **Con số hiệu năng trong paper thường gắn với một runtime cụ thể**: RepViT-SAM công bố "nhanh hơn MobileSAM ~10x", nhưng đó là đo bằng Core ML trên phần cứng Apple; đo lại trên ONNX Runtime CPU (đúng runtime mà app dùng) thì encoder của nó **chậm hơn MobileSAM ~3.2 lần** và file nặng gấp 3. Luôn đo lại trên đúng runtime/phần cứng đích trước khi chọn model theo benchmark của paper.

---

## Phần 3 — API Spec

Dùng chung cho cả Production và Test — toàn bộ endpoint dưới đây giống hệt nhau giữa hai môi trường, khác biệt duy nhất là base URL (xem [1.3](#13-cấu-hình-theo-môi-trường)). Chỉ **data-server** có API public gọi từ Mobile; **ai-server** hoạt động theo mô hình worker kéo việc từ hàng đợi nên không có API nghiệp vụ, chỉ có một endpoint kiểm tra tình trạng nội bộ. Toàn bộ endpoint public xác thực bằng token (Bearer token). Với các request tạo/đọc dữ liệu gắn với người dùng, data-server tự lấy `user_id` (định danh user sở hữu wardrobe) và `account_id` (định danh tài khoản đã đăng nhập) từ session hiện tại ứng với token đó — Mobile không truyền trực tiếp hai giá trị này trong body/query, tránh trường hợp client giả mạo user_id/account_id của người khác.

### 3.1 Data server API

Public, gọi từ Mobile.

**POST /v1/uploads/presign** — xin đường dẫn upload có thời hạn cho một batch ảnh (bước D0).

| Input | Kiểu | Mô tả |
|---|---|---|
| items[].localId | string | Id tạm do Mobile sinh, dùng đối chiếu kết quả |
| items[].contentType | string | Kiểu file ảnh |
| items[].checksum | string (tùy chọn) | Hash nội dung, tránh upload trùng |

| Output | Kiểu | Mô tả |
|---|---|---|
| batchId | string | Id phiên upload, dùng ở bước tạo job |
| items[].objectKey | string | Đường dẫn ảnh trên object-storage |
| items[].uploadUrl | string | URL có chữ ký, thời hạn ngắn |
| items[].expiresAt | thời điểm | Thời điểm uploadUrl hết hạn |

**POST /v1/jobs** — tạo batch job sau khi Mobile đã upload xong toàn bộ ảnh trong batch.

| Input | Kiểu | Mô tả |
|---|---|---|
| batchId | string | Id phiên upload đã xin ở bước presign |
| uploadedItems | danh sách localId | Ảnh đã upload thành công (cho phép job chỉ chứa một phần batch) |

| Output | Kiểu | Mô tả |
|---|---|---|
| jobId | string | Id batch job vừa tạo |
| status | enum | Luôn là "pending" |
| totalItems | số nguyên | Tổng số ảnh sẽ xử lý |
| createdAt | thời điểm | Thời điểm tạo job |

**GET /v1/jobs/{jobId}** — lấy tiến độ xử lý (Mobile dùng để polling).

| Output | Kiểu | Mô tả |
|---|---|---|
| status | enum | pending / processing / cancelling / cancelled / completed / failed |
| totalItems | số nguyên | Tổng số ảnh trong job |
| processedItems | số nguyên | Số ảnh đã xử lý xong |
| failedItems | số nguyên | Số ảnh lỗi |
| updatedAt | thời điểm | Lần cập nhật gần nhất |
| items[].localId | string | Id ảnh phía Mobile (đối chiếu ngược lại asset gốc để cập nhật index quét — xem [1.2](#12-tương-tác-với-server), bước E2) |
| items[].status | enum | "success" / "failed", chỉ có giá trị sau khi ảnh đó đã được AI server xử lý xong |

**POST /v1/jobs/{jobId}/cancel** — người dùng chủ động hủy job đang chạy.

| Output | Kiểu | Mô tả |
|---|---|---|
| status | enum | "cancelling" nếu còn ảnh xử lý dở, "cancelled" nếu dừng ngay được |

**GET /v1/wardrobe/items** — lấy danh sách item trang phục trong tủ đồ (giai đoạn E).

| Input (query param) | Kiểu | Mô tả |
|---|---|---|
| jobId | string (tùy chọn) | Chỉ lấy kết quả của một job cụ thể |
| cursor | string (tùy chọn) | Con trỏ phân trang |
| limit | số nguyên (tùy chọn) | Số item tối đa mỗi trang |

| Output | Kiểu | Mô tả |
|---|---|---|
| items[].imageUrl | string | Đường dẫn ảnh trang phục đã tách |
| items[].tags | danh sách | Loại trang phục, kiểu tay áo, kiểu cổ áo, màu sắc, họa tiết... |
| items[].jobId | string | Job sinh ra item này |
| nextCursor | string (nếu còn dữ liệu) | Dùng cho lần gọi phân trang tiếp theo |

**GET /v1/health** — health-check nội bộ, không cần xác thực.

| Output | Kiểu | Mô tả |
|---|---|---|
| status | enum | "ok" / "error" |
| dependencies | object | Tình trạng kết nối postgres-db, object-storage, queue |

### 3.2 AI server API

Nội bộ, không public. ai-server không nhận request nghiệp vụ trực tiếp từ bất kỳ thành phần nào, kể cả data-server — chỉ có một endpoint giám sát nội bộ.

**GET /health** — xác nhận worker còn sống và còn kết nối được tới queue, object-storage.

| Output | Kiểu | Mô tả |
|---|---|---|
| status | enum | "ok" / "error" |
| activeWorkers | số nguyên | Số tiến trình worker đang chạy |
| dependencies | object | Tình trạng kết nối object-storage, queue |

### 3.3 Cấu trúc bản tin Job queue và Result queue

data-server và ai-server không gọi thẳng nhau qua HTTP — toàn bộ trao đổi đi qua hai hàng đợi mô tả ở [2.1](#21-kiến-trúc-chung). Cấu trúc bản tin đóng vai trò tương đương "input/output" giữa hai server:

**Job queue (data-server → ai-server)**

| Trường | Kiểu | Mô tả |
|---|---|---|
| jobId | string | Job mà ảnh này thuộc về |
| itemId | string | Id riêng của ảnh trong job |
| objectKey | string | Đường dẫn ảnh gốc trên object-storage |

**Result queue (ai-server → data-server)**

| Trường | Kiểu | Mô tả |
|---|---|---|
| jobId | string | Job mà kết quả này thuộc về |
| itemId | string | Đối chiếu với bản tin job queue tương ứng |
| result | enum | "success" / "failed" |
| objectKey | string (nếu success) | Đường dẫn ảnh trang phục đã tách |
| tags | danh sách (nếu success) | Kết quả phân loại từ D3 |
| errorReason | string (nếu failed) | Lý do lỗi, phục vụ debug/retry |
