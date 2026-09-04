"""
Nhận diện trang phục của MỘT người cụ thể trong ảnh nhóm đông người, xác định
người đó bằng cách so khớp khuôn mặt với 1 ảnh selfie tham chiếu.

Pipeline:
1. Detect tất cả người trong ảnh nhóm (Faster R-CNN, COCO person, có sẵn từ
   detect_clothing_yolo.py) -> nhiều box người.
2. Detect khuôn mặt + tính embedding cho ảnh nhóm VÀ ảnh selfie tham chiếu
   (insightface/buffalo_l - ArcFace embedding, chuẩn công nghiệp cho face
   recognition, chạy trên CPU qua onnxruntime).
3. So cosine similarity giữa embedding selfie và từng khuôn mặt trong ảnh
   nhóm -> chọn khuôn mặt khớp nhất (phải vượt FACE_MATCH_THRESHOLD để
   tránh nhận nhầm).
4. Gán khuôn mặt khớp cho đúng người (box người nào chứa tâm khuôn mặt đó).
5. Dùng SAM (facebook/sam-vit-base, qua transformers) với box người làm
   prompt -> lấy mask pixel-chính-xác của riêng người đó. Mask SAM thỉnh
   thoảng có lỗ hổng bị bao kín ở giữa vùng đồ vật màu sáng/ít tương phản
   (đã gặp thực tế: mặt túi sáng màu bị coi là nền, xóa mất 1 mảng lớn giữa
   túi khiến model không nhận ra hình dạng túi -> bag score 0.19). Dùng
   scipy.ndimage.binary_fill_holes để vá các lỗ này (chỉ vá lỗ bị bao kín
   hoàn toàn bởi mask, an toàn, không ảnh hưởng viền mask thật) - đã kiểm
   chứng: bag score tăng từ 0.19 lên 0.68 sau khi vá.
6. "Cô lập" người: thay toàn bộ pixel NGOÀI mask (đã vá lỗ) bằng màu nền
   trung tính (xám), để loại bỏ người khác/nền có thể gây nhiễu detector
   trang phục.
7. Chạy lại pipeline nhận diện trang phục (multi-scale + person-crop +
   resolve_dress_conflict + resolve_ambiguous_pairs) từ detect_clothing_yolo
   trên ảnh đã cô lập.

Usage:
    python detect_clothing_by_face.py --selfie selfie.jpg --group-photo group.jpg
    python detect_clothing_by_face.py --selfie selfie.jpg --images-dir group_photos/
"""

import argparse
import sys
import tempfile
from pathlib import Path

import numpy as np
import torch
from PIL import Image
from scipy.ndimage import binary_fill_holes
from transformers import SamModel, SamProcessor

import detect_clothing_yolo as clothing

if sys.stdout.encoding is not None and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SAM_MODEL_NAME = "facebook/sam-vit-base"
FACE_MATCH_THRESHOLD = 0.35  # cosine similarity ArcFace - duoi nguong nay coi la khong khop
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
# mask SAM phải phủ tối thiểu bao nhiêu % diện tích box người mới được coi
# là hợp lệ (loại các mask "mảnh vỡ" - xem giải thích trong segment_person)
MIN_MASK_AREA_FRAC = 0.2
MASK_BG_COLOR = (128, 128, 128)  # mau nen trung tinh thay cho nguoi khac/background


def load_face_app():
    from insightface.app import FaceAnalysis
    app = FaceAnalysis(name="buffalo_l")
    app.prepare(ctx_id=0, det_size=(640, 640))
    return app


def load_sam(device: str):
    model = SamModel.from_pretrained(SAM_MODEL_NAME).to(device).eval()
    processor = SamProcessor.from_pretrained(SAM_MODEL_NAME)
    return model, processor


def get_all_person_boxes(person_model, person_pre, image: Image.Image):
    x = person_pre(image).unsqueeze(0)
    with torch.no_grad():
        out = person_model(x)[0]
    mask = (out["labels"] == 1) & (out["scores"] > clothing.PERSON_SCORE_THRESHOLD)
    return out["boxes"][mask].tolist()


def find_reference_embedding(face_app, selfie_path: Path):
    img = np.array(Image.open(selfie_path).convert("RGB"))[:, :, ::-1]  # RGB -> BGR cho insightface
    faces = face_app.get(img)
    if not faces:
        raise ValueError(f"Không phát hiện khuôn mặt nào trong ảnh selfie: {selfie_path}")
    # nếu selfie có nhiều mặt (hiếm), lấy mặt lớn nhất (chủ thể chính)
    faces.sort(key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]), reverse=True)
    return faces[0].normed_embedding


def match_face_in_group(face_app, group_image: Image.Image, ref_embedding):
    img = np.array(group_image.convert("RGB"))[:, :, ::-1]
    faces = face_app.get(img)
    if not faces:
        return None, 0.0

    best_face, best_sim = None, -1.0
    for face in faces:
        sim = float(np.dot(face.normed_embedding, ref_embedding))
        if sim > best_sim:
            best_face, best_sim = face, sim
    return best_face, best_sim


def match_face_to_person_box(face_bbox, person_boxes):
    fx = (face_bbox[0] + face_bbox[2]) / 2
    fy = (face_bbox[1] + face_bbox[3]) / 2
    for box in person_boxes:
        if box[0] <= fx <= box[2] and box[1] <= fy <= box[3]:
            return box
    # không box nào chứa tâm mặt (hiếm, do lỗi detect) -> lấy box gần nhất
    def dist(box):
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        return (cx - fx) ** 2 + (cy - fy) ** 2
    return min(person_boxes, key=dist) if person_boxes else None


def _pick_mask(masks, scores, box):
    """Chọn 1 trong 3 mask SAM trả về cho 1 box prompt.

    SAM trả về 3 mask (toàn bộ/1 phần/mảnh nhỏ) kèm điểm iou_score riêng của
    nó, nhưng điểm này thỉnh thoảng bị lệch: đã gặp thực tế 1 ảnh 2 người ngồi
    sát ôm nhau, SAM chấm mask "vài mảnh da rời rạc" (chỉ phủ 9.8% diện tích
    box người) cao hơn 0.01 so với mask body đầy đủ (phủ 38.3%) -> chọn nhầm
    mask gần như trống. Vì vật thể luôn chiếm phần lớn diện tích box của CHÍNH
    NÓ (đúng với cả box người lẫn box món đồ), loại các mask quá nhỏ so với box
    trước khi chọn theo score, để tránh lặp lại lỗi này."""
    box_area = (box[2] - box[0]) * (box[3] - box[1])
    area_fracs = [masks[i].numpy().sum() / box_area for i in range(masks.shape[0])]
    candidates = [i for i, frac in enumerate(area_fracs) if frac >= MIN_MASK_AREA_FRAC]
    if not candidates:
        candidates = range(masks.shape[0])  # không mask nào đủ lớn -> đành lấy hết, chọn theo score
    best = max(candidates, key=lambda i: scores[i].item())
    return masks[best].numpy()  # (H, W) bool


@torch.no_grad()
def segment_boxes(sam_model, sam_processor, image: Image.Image, boxes, device: str):
    """Mask SAM cho NHIỀU box trong MỘT lần gọi -> list[np.ndarray] cùng thứ tự.

    Phải gộp chung 1 lần gọi chứ không lặp từng box: phần đắt nhất của SAM là
    ViT image encoder chạy trên toàn ảnh, còn mask decoder cho thêm 1 box thì
    rất rẻ. Đo trên ảnh 3024x4032 (CPU): 1 box = 2.46s, 5 box gộp 1 lần gọi =
    2.78s, nhưng 5 box gọi riêng từng cái = 12.25s (encoder chạy lại 5 lần)."""
    inputs = sam_processor(image, input_boxes=[list(boxes)], return_tensors="pt").to(device)
    outputs = sam_model(**inputs)
    masks = sam_processor.image_processor.post_process_masks(
        outputs.pred_masks.cpu(), inputs["original_sizes"].cpu(), inputs["reshaped_input_sizes"].cpu()
    )[0]  # (num_boxes, 3, H, W)
    scores = outputs.iou_scores.cpu()[0]  # (num_boxes, 3)
    return [_pick_mask(masks[i], scores[i], box) for i, box in enumerate(boxes)]


def segment_person(sam_model, sam_processor, image: Image.Image, person_box, device: str):
    return binary_fill_holes(
        segment_boxes(sam_model, sam_processor, image, [person_box], device)[0]
    )


def isolate_person(image: Image.Image, mask: np.ndarray) -> Image.Image:
    arr = np.array(image.convert("RGB"))
    out = np.full_like(arr, MASK_BG_COLOR)
    out[mask] = arr[mask]
    return Image.fromarray(out)


def find_images(images_dir: Path):
    # loại các file do chính script này tạo ra ở lần chạy trước (--save-isolated)
    # để tránh quét lặp lại "isolated_isolated_..." khi chạy lại trên cùng thư mục
    return sorted(
        p for p in images_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS
        and not p.name.startswith(("isolated_", ".tmp_isolated_"))
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selfie", type=Path, required=True, help="Ảnh selfie tham chiếu của người cần tìm")
    parser.add_argument("--group-photo", type=Path, help="1 ảnh nhóm cụ thể")
    parser.add_argument("--images-dir", type=Path, help="Thư mục chứa nhiều ảnh nhóm")
    parser.add_argument("--threshold", type=float, default=0.5)
    parser.add_argument("--face-match-threshold", type=float, default=FACE_MATCH_THRESHOLD)
    parser.add_argument("--save-isolated", action="store_true",
                         help="Lưu ảnh đã cô lập người để kiểm tra trực quan")
    parser.add_argument("--isolated-dir", type=Path, default=Path("isolated_output"),
                         help="Thư mục lưu ảnh cô lập (tách riêng khỏi thư mục ảnh gốc, mặc định: isolated_output/)")
    args = parser.parse_args()

    if not args.group_photo and not args.images_dir:
        parser.error("Cần --group-photo hoặc --images-dir")

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Device: {device}")

    print("Đang tải face recognition (insightface/buffalo_l) ...")
    face_app = load_face_app()
    ref_embedding = find_reference_embedding(face_app, args.selfie)
    print(f"Đã lấy embedding từ selfie: {args.selfie.name}")

    print("Đang tải SAM (facebook/sam-vit-base) ...")
    sam_model, sam_processor = load_sam(device)

    print("Đang tải model nhận diện trang phục ...")
    cloth_model, procs_full, procs_crop, person_model, person_pre = clothing.load_model(
        device, multi_scale=True, person_crop=True
    )

    images = [args.group_photo] if args.group_photo else find_images(args.images_dir)
    images = [p for p in images if p]

    if args.save_isolated:
        args.isolated_dir.mkdir(parents=True, exist_ok=True)

    for path in images:
        image = Image.open(path).convert("RGB")

        person_boxes = get_all_person_boxes(person_model, person_pre, image)
        if not person_boxes:
            print(f"{path.name}: không phát hiện người nào trong ảnh")
            continue

        face, sim = match_face_in_group(face_app, image, ref_embedding)
        if face is None or sim < args.face_match_threshold:
            print(f"{path.name}: không tìm thấy khuôn mặt khớp (similarity cao nhất={sim:.3f}, cần >= {args.face_match_threshold})")
            continue

        person_box = match_face_to_person_box(face.bbox.tolist(), person_boxes)
        print(f"{path.name}: khớp khuôn mặt (similarity={sim:.3f}), person_box={[round(v,1) for v in person_box]}")

        if len(person_boxes) == 1:
            # Chỉ có 1 người trong ảnh -> không ai để lọc bỏ, SAM/tô nền xám
            # là thừa (tốn thời gian + làm lệch nhẹ kết quả so với ảnh gốc,
            # đã đo được: bottom 0.90 -> 0.84 trên cùng 1 ảnh). Chạy thẳng
            # trên ảnh gốc, giống hệt detect_clothing_yolo.py độc lập.
            print("  Chỉ 1 người trong ảnh -> bỏ qua SAM, detect trực tiếp trên ảnh gốc")
            detections = clothing.predict_image(
                cloth_model, procs_full, procs_crop, person_model, person_pre,
                path, device, args.threshold, resolve_dress=True,
            )
        else:
            mask = segment_person(sam_model, sam_processor, image, person_box, device)
            isolated = isolate_person(image, mask)

            if args.save_isolated:
                out_path = args.isolated_dir / f"{path.stem}.png"
                isolated.save(out_path)
                print(f"  Đã lưu ảnh cô lập: {out_path}")

            # Lưu ảnh cô lập ra file tạm (ngoài mọi thư mục ảnh của người dùng)
            # rồi chạy pipeline nhận diện trang phục có sẵn trên file tạm đó.
            with tempfile.TemporaryDirectory() as tmp_dir:
                tmp_path = Path(tmp_dir) / f"{path.stem}.png"
                isolated.save(tmp_path)
                detections = clothing.predict_image(
                    cloth_model, procs_full, procs_crop, person_model, person_pre,
                    tmp_path, device, args.threshold, resolve_dress=True,
                )

        summary = ", ".join(f"{d['label']}({d['score']:.2f})" for d in detections) or "(không phát hiện item nào)"
        print(f"  Trang phục: {summary}")


if __name__ == "__main__":
    main()
