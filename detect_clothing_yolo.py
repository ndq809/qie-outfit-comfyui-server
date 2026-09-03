"""
Nhận diện các item trang phục trong ảnh bằng model
yainage90/fashion-object-detection (Conditional DETR, object detection thật
sự với bounding box), khác với marqo-fashionSigLIP (zero-shot classification
qua text-image similarity, không có bbox).

Nhãn có sẵn trong model (khớp đúng yêu cầu):
    bag, bottom, dress, hat, outer, shoes, top

Cải tiến so với bản chạy 1 scale mặc định:
- Model mặc định resize ảnh về shortest_edge=800, quá nhỏ so với ảnh gốc
  (thường 3000-4000px từ điện thoại) nên item nhỏ (hat, dây bag...) mất chi
  tiết và bị bỏ sót (đã kiểm chứng: hat 0.44 -> 0.72 khi tăng lên 1200px).
- Nhưng tăng resolution không phải lúc nào cũng tốt hơn: đã thấy trường hợp
  ảnh khác bị mất hẳn 1 nhãn đúng ("outer") khi chỉ chạy ở 1200px do object
  choán gần hết khung hình / ảnh thiếu sáng ban đêm.
- multi-scale TTA: chạy inference ở nhiều scale (800 & 1200) trên ẢNH GỐC,
  gộp box + NMS theo từng class -> lấy ưu điểm của cả 2 scale.
- person-crop TTA: dùng Faster R-CNN (COCO, có sẵn trong torchvision) để
  detect người, crop sát người + đệm 12%, chạy multi-scale detector fashion
  trên vùng crop này rồi cộng dồn vào cùng tập ứng viên (tọa độ box được quy
  đổi lại về ảnh gốc). Đã kiểm chứng: giúp bắt thêm hat/top/bottom trên ảnh
  người chiếm diện tích nhỏ trong khung hình (vd hat 0 -> 0.71, xuất hiện cả
  top/bottom thay vì chỉ "dress" một mình).
  QUAN TRỌNG: nếu chỉ dùng crop để THAY THẾ ảnh gốc (không cộng dồn) thì lại
  làm giảm chất lượng ở ảnh mà người đã chiếm phần lớn khung hình (đã đo
  được: confidence tụt từ 0.66 xuống dưới 0.5, mất hẳn detection) vì crop
  quá sát khiến bố cục lệch khỏi phân bố ảnh mà model fashion được huấn
  luyện. Vì vậy code luôn CỘNG DỒN ứng viên từ cả ảnh gốc lẫn ảnh crop rồi
  NMS chung, không bao giờ chỉ dùng ảnh crop một mình.
- xử lý xung đột dress vs top+bottom bằng màu sắc (hue): khi box "dress" đè
  gần trùng lên vùng hợp của "top"+"bottom" (cùng mô tả 1 outfit theo 2 cách
  loại trừ nhau), so màu trung bình (hue trong HSV, ít bị ảnh hưởng bởi ánh
  sáng/bóng đổ hơn RGB thô) giữa vùng top và vùng bottom. Hue lệch nhiều
  (áo/quần khác màu rõ) -> bằng chứng ủng hộ top+bottom, bỏ dress. Hue gần
  nhau (đồng màu liền mạch) -> có thể là dress thật, ngược lại. Đã kiểm
  chứng trên ảnh mẫu: outfit áo trắng + váy xanh cho hue_diff=159° (rất rõ)
  khi dùng đúng box confidence cao; heuristic RGB thô thử trước đó (so màu
  theo dải % cố định) bị nhiễu bởi ánh sáng nên không dùng.
  LƯU Ý: chỉ tính hue_diff khi CẢ dress, top, bottom đều vượt ngưỡng cuối
  cùng - dress "thật" (1 mảnh) hầu như không bao giờ tạo ra cặp top+bottom
  đủ tin cậy nên xung đột này tự nhiên hiếm khi xảy ra sai.
- xử lý top vs outer cạnh tranh CÙNG 1 món đồ (khác dress-vs-top+bottom là
  2 cách diễn giải cho cả outfit): áo len/áo khoác mỏng khiến model phân vân
  giữa "top" và "outer" cho đúng 1 vùng, cả 2 đều dưới --threshold (vd đo
  được top=0.44 outer=0.48, cả 2 dưới 0.5 nên trước đây bị bỏ sót hoàn
  toàn). Khi box của top và outer đè lên nhau > 50%, coi là 1 quyết định
  duy nhất: lấy nhãn có score cao hơn, so với AMBIGUOUS_FLOOR=0.35 thay vì
  --threshold. Vẫn giữ ngưỡng sàn 0.35 để không output khi cả 2 đều rất
  thấp (vd người không mặc áo nhưng model vẫn "cố đoán" ra top/outer với
  xác suất rất thấp trên vùng da/tay).

Usage:
    python detect_clothing_yolo.py [--images-dir images] [--threshold 0.5]
    python detect_clothing_yolo.py --single-scale   # tắt multi-scale, chạy nhanh hơn
    python detect_clothing_yolo.py --no-person-crop # tắt bước crop người
    python detect_clothing_yolo.py --resolve-dress-conflict  # tự bỏ dress hoặc top+bottom khi xung đột
"""

import argparse
import csv
import json
import sys
import time
from pathlib import Path

import numpy as np
import torch
from PIL import Image
from torchvision.models.detection import (
    FasterRCNN_MobileNet_V3_Large_320_FPN_Weights,
    fasterrcnn_mobilenet_v3_large_320_fpn,
)
from torchvision.ops import nms
from transformers import AutoImageProcessor, AutoModelForObjectDetection

if sys.stdout.encoding is not None and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

MODEL_NAME = "yainage90/fashion-object-detection"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}

SCALES_FULL = [
    {"shortest_edge": 800, "longest_edge": 1333},   # mặc định của model
    {"shortest_edge": 1200, "longest_edge": 2000},  # bắt tốt hơn các item nhỏ
]
# longest_edge nâng cao hơn cho vùng crop người vì crop thường có tỷ lệ dọc
# hẹp (rất cao/hẹp) - nếu giữ longest_edge=2000 như trên, ảnh sẽ bị ép nhỏ
# lại DƯỚI cả 800px do longest_edge chặn trước, phản tác dụng (đã đo được).
SCALES_CROP = [
    {"shortest_edge": 800, "longest_edge": 2600},
    {"shortest_edge": 1200, "longest_edge": 3600},
]
PERSON_CROP_PAD_FRAC = 0.12
PERSON_SCORE_THRESHOLD = 0.5

# Ngưỡng thấp để thu thập ứng viên từ mỗi scale trước khi gộp + NMS + lọc
# bằng --threshold ở bước cuối.
COLLECT_THRESHOLD = 0.25
NMS_IOU_THRESHOLD = 0.45

# dress vs top+bottom: box "dress" phải trùng >= tỉ lệ này với vùng hợp của
# top+bottom mới coi là cùng 1 outfit (xung đột thật, không phải 2 vùng khác nhau)
DRESS_CONFLICT_OVERLAP = 0.5
# hue lệch bao nhiêu độ (thang 0-360, hue là đại lượng vòng tròn) thì coi là
# "khác màu rõ ràng" -> ủng hộ top+bottom thay vì dress
HUE_DIFF_THRESHOLD = 40.0
# Ngưỡng sàn cho top/bottom SAU KHI đã xác nhận thắng bằng hue - thấp hơn
# --threshold chính vì bằng chứng màu đã đủ mạnh, không nên để 1 bên (hay
# gặp ở bottom) bị rớt threshold chính dù xung đột đã được phân xử đúng.
DRESS_PAIR_FLOOR = 0.35

# Các cặp nhãn hay "giành" cùng 1 vùng ảnh (model phân vân giữa 2 cách gọi
# cho CÙNG 1 món đồ, khác với dress-vs-top+bottom là 2 cách diễn giải cho
# CẢ outfit). Khi 2 box của 1 cặp đè lên nhau nhiều, coi đó là 1 quyết định
# duy nhất: lấy nhãn có score cao hơn, so với AMBIGUOUS_FLOOR thay vì
# --threshold thông thường (vì nếu bắt cả 2 độc lập vượt threshold thì hay
# bị mất - vd áo len dày: top=0.44, outer=0.48, cả 2 đều dưới 0.5).
AMBIGUOUS_LABEL_PAIRS = [("top", "outer")]
AMBIGUOUS_OVERLAP_THRESHOLD = 0.5
# Ngưỡng sàn khi 2 nhãn cạnh tranh - PHẢI thấp hơn --threshold để có tác
# dụng, nhưng vẫn đủ cao để không output khi người mặc không có áo (cả top
# và outer đều chỉ lởn vởn ở mức rất thấp do model "cố đoán" trên da/tay).
AMBIGUOUS_FLOOR = 0.35


def _box_iou(a, b):
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _region_hue(image: Image.Image, box):
    x0, y0, x1, y1 = [int(v) for v in box]
    crop = image.crop((x0, y0, x1, y1)).convert("HSV")
    arr = np.array(crop).reshape(-1, 3).astype(float)
    if arr.size == 0:
        return None
    hue = arr[:, 0] / 255 * 360
    sat = arr[:, 1] / 255
    # bỏ pixel ít bão hòa (gần trắng/xám/đen) vì hue vô nghĩa ở đó
    mask = sat > 0.15
    if mask.sum() < 10:
        mask = np.ones_like(sat, dtype=bool)
    ang = np.deg2rad(hue[mask])
    return float(np.rad2deg(np.arctan2(np.sin(ang).mean(), np.cos(ang).mean())) % 360)


def resolve_dress_conflict(candidates: dict, image: Image.Image, auto_resolve: bool):
    """candidates: label -> list[{"score","box"}], đã NMS nhưng CHƯA lọc theo
    --threshold (quan trọng: nếu lọc trước rồi mới xét xung đột thì sẽ dính
    đúng bug đã gặp ở IMG_9882 - "bottom" chỉ 0.485 < threshold=0.5 nên bị
    loại trước khi kịp so với "dress", kết quả còn sót "dress"+"top" thiếu
    "bottom", vi phạm quy tắc top luôn phải đi cùng bottom, dress đứng
    riêng).

    dress vs top+bottom là 2 cách diễn giải loại trừ nhau cho CẢ outfit
    (khác resolve_ambiguous_pairs là cạnh tranh nhãn cho 1 món đồ). Dùng hue
    để phân xử, rồi:
      - Nếu thắng là top+bottom: xoá "dress" khỏi candidates, ép hiện cả
        top VÀ bottom bất kể --threshold chính (dùng floor riêng thấp hơn)
        vì bằng chứng màu đã đủ mạnh để tin cả 2 phía, không nên để bên yếu
        hơn (thường là bottom) bị rớt threshold sau khi đã xác nhận đúng.
      - Nếu thắng là dress: xoá "top" và "bottom" khỏi candidates, dress vẫn
        phải qua --threshold bình thường ở bước sau.
    Trả về (forced_detections, candidates_đã_cập_nhật)."""
    dress_list = candidates.get("dress", [])
    top_list = candidates.get("top", [])
    bottom_list = candidates.get("bottom", [])
    if not (dress_list and top_list and bottom_list):
        return [], candidates

    dress_i = max(range(len(dress_list)), key=lambda i: dress_list[i]["score"])
    top_i = max(range(len(top_list)), key=lambda i: top_list[i]["score"])
    bottom_i = max(range(len(bottom_list)), key=lambda i: bottom_list[i]["score"])
    dress, top, bottom = dress_list[dress_i], top_list[top_i], bottom_list[bottom_i]

    union_box = [
        min(top["box"][0], bottom["box"][0]), min(top["box"][1], bottom["box"][1]),
        max(top["box"][2], bottom["box"][2]), max(top["box"][3], bottom["box"][3]),
    ]
    overlap = _box_iou(dress["box"], union_box)
    if overlap < DRESS_CONFLICT_OVERLAP:
        return [], candidates

    hue_top = _region_hue(image, top["box"])
    hue_bottom = _region_hue(image, bottom["box"])
    hue_diff = None
    if hue_top is not None and hue_bottom is not None:
        d = abs(hue_top - hue_bottom)
        hue_diff = min(d, 360 - d)

    favors_two_piece = hue_diff is not None and hue_diff >= HUE_DIFF_THRESHOLD
    conflict_info = {
        "overlap": round(overlap, 3),
        "hue_diff": round(hue_diff, 1) if hue_diff is not None else None,
        "verdict": "top+bottom" if favors_two_piece else "dress",
    }

    if not auto_resolve:
        dress["dress_conflict"] = conflict_info
        top["dress_conflict"] = conflict_info
        bottom["dress_conflict"] = conflict_info
        return [], candidates

    new_candidates = {label: list(items) for label, items in candidates.items()}
    if favors_two_piece:
        del new_candidates["dress"][dress_i]
        del new_candidates["top"][top_i]
        del new_candidates["bottom"][bottom_i]
        forced = []
        for label, cand in (("top", top), ("bottom", bottom)):
            if cand["score"] >= DRESS_PAIR_FLOOR:
                forced.append({
                    "label": label,
                    "score": round(cand["score"], 4),
                    "box": cand["box"],
                    "dress_conflict": conflict_info,
                })
        return forced, new_candidates

    del new_candidates["top"][top_i]
    del new_candidates["bottom"][bottom_i]
    dress["dress_conflict"] = conflict_info
    return [], new_candidates


def load_model(device: str, multi_scale: bool, person_crop: bool):
    scales_full = SCALES_FULL if multi_scale else SCALES_FULL[:1]
    scales_crop = SCALES_CROP if multi_scale else SCALES_CROP[:1]
    processors_full = [AutoImageProcessor.from_pretrained(MODEL_NAME, size=s) for s in scales_full]
    processors_crop = [AutoImageProcessor.from_pretrained(MODEL_NAME, size=s) for s in scales_crop]
    model = AutoModelForObjectDetection.from_pretrained(MODEL_NAME).to(device).eval()

    person_model = person_pre = None
    if person_crop:
        weights = FasterRCNN_MobileNet_V3_Large_320_FPN_Weights.DEFAULT
        # torchvision cài trong môi trường này là bản CPU-only (mismatch với
        # build CUDA của torch) nên model người phải chạy trên CPU. Dùng biến
        # thể "_320_" (resize nội bộ 320/640 thay vì 800/1333) vì chỉ cần bbox
        # thô để crop, không cần độ chính xác pixel - đã đo được nhanh hơn
        # 2-3 lần (0.4-0.6s -> 0.07-0.24s/ảnh) mà vẫn detect đúng 100%.
        person_model = fasterrcnn_mobilenet_v3_large_320_fpn(weights=weights).eval()
        person_pre = weights.transforms()

    return model, processors_full, processors_crop, person_model, person_pre


@torch.no_grad()
def get_person_crop(person_model, person_pre, image: Image.Image):
    x = person_pre(image).unsqueeze(0)
    out = person_model(x)[0]
    person_idx = 1  # "person" trong COCO
    mask = (out["labels"] == person_idx) & (out["scores"] > PERSON_SCORE_THRESHOLD)
    boxes = out["boxes"][mask]
    if len(boxes) == 0:
        return None

    areas = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
    b = boxes[areas.argmax()].tolist()
    w, h = image.size
    pw, ph = (b[2] - b[0]) * PERSON_CROP_PAD_FRAC, (b[3] - b[1]) * PERSON_CROP_PAD_FRAC
    x0 = max(0, int(b[0] - pw))
    y0 = max(0, int(b[1] - ph))
    x1 = min(w, int(b[2] + pw))
    y1 = min(h, int(b[3] + ph))
    return image.crop((x0, y0, x1, y1)), (x0, y0)


@torch.no_grad()
def run_scales(model, processors, image: Image.Image, device: str, offset=(0, 0)):
    target_sizes = torch.tensor([image.size[::-1]])
    boxes_list, scores_list, labels_list = [], [], []
    for processor in processors:
        inputs = processor(images=image, return_tensors="pt").to(device)
        outputs = model(**inputs)
        res = processor.post_process_object_detection(
            outputs, threshold=COLLECT_THRESHOLD, target_sizes=target_sizes
        )[0]
        boxes = res["boxes"].cpu()
        boxes[:, [0, 2]] += offset[0]
        boxes[:, [1, 3]] += offset[1]
        boxes_list.append(boxes)
        scores_list.append(res["scores"].cpu())
        labels_list.append(res["labels"].cpu())
    return torch.cat(boxes_list), torch.cat(scores_list), torch.cat(labels_list)


def resolve_ambiguous_pairs(candidates: dict, threshold: float):
    """candidates: label -> list[{"score","box"}], đã NMS trong từng class
    nhưng CHƯA lọc theo threshold. Với các cặp nhãn trong AMBIGUOUS_LABEL_PAIRS
    mà box đè lên nhau nhiều (cùng 1 món đồ, model phân vân gọi tên gì), gộp
    thành 1 quyết định: lấy score cao hơn, so với AMBIGUOUS_FLOOR (thấp hơn
    threshold thường). Các box không có đối thủ cạnh tranh (không trùng cặp
    nào) vẫn theo luật threshold bình thường."""
    consumed = {label: set() for label in candidates}
    resolved = []

    for label_a, label_b in AMBIGUOUS_LABEL_PAIRS:
        for i, cand_a in enumerate(candidates.get(label_a, [])):
            if i in consumed[label_a]:
                continue
            best_j, best_iou = None, 0.0
            for j, cand_b in enumerate(candidates.get(label_b, [])):
                if j in consumed[label_b]:
                    continue
                iou = _box_iou(cand_a["box"], cand_b["box"])
                if iou > best_iou:
                    best_j, best_iou = j, iou
            if best_j is None or best_iou < AMBIGUOUS_OVERLAP_THRESHOLD:
                continue

            cand_b = candidates[label_b][best_j]
            consumed[label_a].add(i)
            consumed[label_b].add(best_j)
            if cand_a["score"] >= cand_b["score"]:
                winner, winner_label, loser_label = cand_a, label_a, label_b
            else:
                winner, winner_label, loser_label = cand_b, label_b, label_a
            if winner["score"] < AMBIGUOUS_FLOOR:
                continue
            resolved.append({
                "label": winner_label,
                "score": round(winner["score"], 4),
                "box": winner["box"],
                "ambiguous_pair": {
                    "competing_label": loser_label,
                    "competing_score": round(min(cand_a["score"], cand_b["score"]), 4),
                    "overlap": round(best_iou, 3),
                },
            })

    for label, items in candidates.items():
        for i, cand in enumerate(items):
            if i in consumed[label]:
                continue
            if cand["score"] < threshold:
                continue
            resolved.append({
                "label": label,
                "score": round(cand["score"], 4),
                "box": cand["box"],
            })

    return resolved


@torch.no_grad()
def predict_image(model, processors_full, processors_crop, person_model, person_pre,
                   image_path: Path, device: str, threshold: float, resolve_dress: bool):
    image = Image.open(image_path).convert("RGB")

    boxes, scores, labels = run_scales(model, processors_full, image, device)

    if person_model is not None:
        crop_info = get_person_crop(person_model, person_pre, image)
        if crop_info is not None:
            crop, offset = crop_info
            cb, cs, cl = run_scales(model, processors_crop, crop, device, offset)
            boxes = torch.cat([boxes, cb])
            scores = torch.cat([scores, cs])
            labels = torch.cat([labels, cl])

    # NMS theo từng class trước, CHƯA áp --threshold vội - để resolve_ambiguous_pairs
    # có đủ ứng viên (kể cả điểm thấp) mà xét các cặp nhãn cạnh tranh cùng vùng.
    candidates = {}  # label -> list[{"score":..., "box":...}]
    for label_id in labels.unique():
        mask = labels == label_id
        cls_boxes, cls_scores = boxes[mask], scores[mask]
        keep = nms(cls_boxes, cls_scores, NMS_IOU_THRESHOLD)
        label = model.config.id2label[label_id.item()]
        candidates[label] = [
            {"score": cls_scores[i].item(), "box": [round(v, 1) for v in cls_boxes[i].tolist()]}
            for i in keep
        ]

    forced_dress, candidates = resolve_dress_conflict(candidates, image, resolve_dress)
    detections = resolve_ambiguous_pairs(candidates, threshold)
    detections = forced_dress + detections
    detections.sort(key=lambda d: -d["score"])
    return detections


def find_images(images_dir: Path):
    return sorted(
        p for p in images_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--images-dir", type=Path, default=Path("images"))
    parser.add_argument("--threshold", type=float, default=0.5,
                         help="Ngưỡng confidence để giữ lại 1 detection")
    parser.add_argument("--output-json", type=Path, default=Path("results_yolo.json"))
    parser.add_argument("--output-csv", type=Path, default=Path("results_yolo.csv"))
    parser.add_argument("--single-scale", action="store_true",
                         help="Chỉ chạy ở scale mặc định (800px), bỏ qua multi-scale TTA để nhanh hơn")
    parser.add_argument("--no-person-crop", action="store_true",
                         help="Tắt bước detect+crop người trước khi nhận diện trang phục")
    parser.add_argument("--resolve-dress-conflict", action="store_true",
                         help="Tự động bỏ dress hoặc top+bottom khi xung đột, dựa vào chênh lệch màu (hue)")
    args = parser.parse_args()

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Device: {device}")

    images = find_images(args.images_dir)
    if not images:
        print(f"Không tìm thấy ảnh nào trong {args.images_dir}")
        return
    print(f"Tìm thấy {len(images)} ảnh trong {args.images_dir}")

    print(f"Đang tải model {MODEL_NAME} ...")
    model, processors_full, processors_crop, person_model, person_pre = load_model(
        device, multi_scale=not args.single_scale, person_crop=not args.no_person_crop
    )

    results = []
    for path in images:
        if device == "cuda":
            torch.cuda.synchronize()
        t0 = time.perf_counter()
        try:
            detections = predict_image(
                model, processors_full, processors_crop, person_model, person_pre,
                path, device, args.threshold, args.resolve_dress_conflict,
            )
        except Exception as e:
            print(f"[LỖI] {path.name}: {e}")
            continue
        if device == "cuda":
            torch.cuda.synchronize()
        elapsed = time.perf_counter() - t0

        labels = sorted({d["label"] for d in detections})
        results.append({
            "file": str(path.relative_to(args.images_dir)),
            "detected": labels,
            "detections": detections,
            "elapsed_sec": round(elapsed, 3),
        })
        summary = ", ".join(f"{d['label']}({d['score']:.2f})" for d in detections) or "(không phát hiện item nào)"
        print(f"{path.name}: {summary}  [{elapsed:.2f}s]")

    args.output_json.write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    all_labels = ["bag", "bottom", "dress", "hat", "outer", "shoes", "top"]
    with args.output_csv.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["file", "detected", "elapsed_sec"] + all_labels)
        for r in results:
            best = {}
            for d in r["detections"]:
                if d["label"] not in best or d["score"] > best[d["label"]]:
                    best[d["label"]] = d["score"]
            writer.writerow(
                [r["file"], "|".join(r["detected"]), r["elapsed_sec"]]
                + [best.get(label, 0) for label in all_labels]
            )

    total_time = sum(r["elapsed_sec"] for r in results)
    avg_time = total_time / len(results) if results else 0
    print(f"\nTổng thời gian xử lý: {total_time:.2f}s, trung bình {avg_time:.2f}s/ảnh")
    print(f"Đã lưu kết quả: {args.output_json} và {args.output_csv}")


if __name__ == "__main__":
    main()
