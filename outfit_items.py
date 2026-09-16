#!/usr/bin/env python3
"""Decide which garment/accessory categories the person in a photo is actually
wearing, so test_extract_outfit.py can state facts in its prompt instead of
asking the diffusion model to guess presence from pixels.

Replaces the previous CLIP ViT-B/32 zero-shot presence check with the exact
stack detect_clothing_by_face.py uses, which is a strictly better fit here:

- yainage90/fashion-object-detection (Conditional DETR) is a real object
  detector trained on fashion items, with the label set this script needs
  1:1 (bag, bottom, dress, hat, outer, shoes, top), instead of CLIP's
  whole-image text-image similarity. That removes the hand-written
  positive/negative prompt pairs entirely, including the hack of cropping the
  bottom 25% of the frame for footwear (whole-image CLIP kept confusing
  sand/ocean/fabric with shoes) - a detector localises shoes on its own.
- multi-scale + person-crop TTA and the hue-based dress-vs-top+bottom
  arbitration from detect_clothing_yolo.py, so "is this a real one-piece
  dress or a separate top and bottom" is settled by comparing the actual
  colours of the two regions rather than by a CLIP caption comparison.
- insightface (ArcFace) face matching + SAM person isolation from
  detect_clothing_by_face.py, so a photo containing more than one person
  doesn't leak someone else's clothes into the item list. With a selfie the
  target person is picked by face similarity; without one, the largest person
  in the frame is taken as the subject. Either way the other people are
  painted out with SAM before detection, exactly as in
  detect_clothing_by_face.py.

Where the two disagreed, this module follows detect_clothing_by_face.py rather
than the assumptions the old CLIP path in test_extract_outfit.py carried:

- Runs on GPU when one is available (same line detect_clothing_by_face.py
  uses: "cuda" if torch.cuda.is_available() else "cpu"), not CPU-only. Set
  OUTFIT_ITEMS_DEVICE=cpu to force CPU back - worth doing on a box where
  ComfyUI is already paging a ~30GB fp8 unet + text encoder through a 24GB
  card, since the detectors would otherwise compete for that VRAM.
- Thresholds are the ones detect_clothing_by_face.py exposes on its CLI, and
  are overridable per call instead of being baked in.
- Reports exactly what the detector found. The old CLIP path hardcoded
  "top + bottom" whenever the outfit wasn't a one-piece; a detector that
  localises garments is the better authority, so a half it doesn't find is no
  longer invented. If real garments are being missed, lower `threshold`
  rather than assuming them present.

Model objects are cached at module level so item_detector_service.py can keep
them warm across requests (see that file).
"""

import os
import tempfile
import time
from pathlib import Path

import torch
from PIL import Image
from scipy.ndimage import binary_fill_holes

import detect_clothing_by_face as byface
import detect_clothing_yolo as clothing


def _resolve_device():
    forced = os.environ.get("OUTFIT_ITEMS_DEVICE")
    if forced:
        return forced
    return "cuda" if torch.cuda.is_available() else "cpu"


# Fashion detector and SAM run here. The person detector (Faster R-CNN) always
# stays on CPU - detect_clothing_yolo.load_model() never moves it, because the
# torchvision build in this environment is CPU-only (see its comment).
DEVICE = _resolve_device()
# Same knobs detect_clothing_by_face.py runs with: multi-scale + person-crop TTA
# on, dress conflict auto-resolved by hue, and face-match threshold 0.35 - the
# latter taken straight from that module so the two can't drift apart.
#
# The detector floor is deliberately 0.4 rather than the 0.5 that
# detect_clothing_by_face.py still defaults to: 0.5 measurably drops real
# garments off the item list (measured on IMG_0876 - a plainly visible pair of
# trousers scores 0.4218, so at 0.5 the prompt asked for jacket+bag only and the
# generation duplicated the bag to fill the layout). 0.4 keeps those, while
# staying above the ~0.35 range where the standalone script's own floors
# (AMBIGUOUS_FLOOR / DRESS_PAIR_FLOOR) already handle the contested cases.
MULTI_SCALE = True
THRESHOLD = 0.4
FACE_MATCH_THRESHOLD = byface.FACE_MATCH_THRESHOLD
# How much of a garment box must lie inside the subject's person box for the item
# to count as theirs and be added to the isolation mask (see _subject_mask()).
#
# Measured over images/, the subject's own items land at 0.833 / 0.947 / 1.000
# (the 0.833 is a crossbody bag whose box is loose and spills past his
# silhouette) and other people's at 0.431 / 0.160 / 0.000, so the usable gap is
# 0.431-0.833. This sits hard against the TOP of that gap on purpose, not at the
# midpoint: 0.65 was tried and regressed IMG_9653 from
# {shoes, top, bottom, bag} to {shoes, outer}. The single extra box it admitted
# was that subject's own top at 0.754, and its mask only grew the union by 4,535
# px (1.1%) - but the resolve_* steps downstream are winner-take-all, so a
# perturbation that small is enough to flip the top/outer arbitration and take
# bottom and bag down with it. Only union a box that is unambiguously inside the
# subject; a marginal one costs more than it adds.
ITEM_INSIDE_PERSON_FRAC = 0.8

# Per-class floors applied on top of THRESHOLD, for classes whose score is not
# calibrated like the rest.
#
# hat: the detector calls hair, a hair flower and a bare head "hat" in a tight band
# just above 0.4. Labelled by eye over 67 real photos, every photo that produced a
# hat item was checked: the four false ones scored 0.401 / 0.415 / 0.422 / 0.462
# (short hair, a bare head, an orchid pinned in hair) and the one real one 0.717 (a
# navy cap and a straw hat). Nothing real was observed between them, so 0.55 sits in
# the gap - closer to the false side on purpose, since it rests on a single positive.
# Hats are rare in this corpus (1 of 67 photos), so a missed hat costs less than the
# junk item a false one puts in the wardrobe.
CLASS_THRESHOLDS = {"hat": 0.55}

# Single-person photos get no SAM isolation (see detect_worn_items), so nothing stops
# the detector reporting an object lying in the background as the subject's. Keeping
# only detections that overlap the subject's person box by this much is enough, and
# unlike isolating it does not touch a pixel of the image.
#
# Measured over all 57 detections on 25 real single-person photos, by how much of the
# garment box lies inside the person box: exactly one falls below 50% - a pair of shoes
# on the floor behind the subject, at 27% - and the lowest real one is 66.1%. 50% sits
# in that gap with 2.4x margin.
#
# Deliberately looser than ITEM_INSIDE_PERSON_FRAC (0.8), which answers a different
# question (what to add to the isolation mask). Four real garments here sit between 50%
# and 80%, one of them an outer whose box is larger than the person box the detector
# drew, so 0.8 would throw them away.
SUBJECT_ITEM_MIN_FRAC = 0.5

# Sàn để NHẬN LẠI một box người đã bị PERSON_SCORE_THRESHOLD loại, và chỉ khi khuôn mặt
# chủ thể nằm trong nó (xem _rescue_person_box_for_face). Người bị che trong ảnh selfie
# đo được 0.4346; các box nhiễu trong cùng ảnh đó đều <= 0.24, nên 0.30 nằm giữa.
PERSON_RESCUE_FLOOR = 0.30


_clothing_models = None
_sam = None
_face_app = None


def load_clothing_models():
    """Fashion detector + person detector (Faster R-CNN). Loaded once, reused."""
    global _clothing_models
    if _clothing_models is None:
        _clothing_models = clothing.load_model(DEVICE, multi_scale=MULTI_SCALE, person_crop=True)
    return _clothing_models


def _load_sam():
    global _sam
    if _sam is None:
        _sam = byface.load_sam(DEVICE)
    return _sam


def _load_face_app():
    global _face_app
    if _face_app is None:
        _face_app = byface.load_face_app()
    return _face_app


def _box_area(box):
    return (box[2] - box[0]) * (box[3] - box[1])


def _dress_vs_pair_loser(effective):
    """A dress and a top/bottom are mutually exclusive readings of one outfit, so one
    of them has to go.

    clothing.resolve_dress_conflict() settles this by comparing the hue of the top and
    bottom regions - but it only runs when dress, top AND bottom are all present, and
    returns untouched otherwise. On a chest-up selfie there is no bottom to detect, so
    a patterned shirt that also scored as a dress left both flags standing and the
    prompt asked for a dress and a shirt at once, putting a "mid length dresses" item
    in a man's wardrobe.

    With no bottom there is no colour evidence to weigh, so fall back to the detector's
    own confidence. This also backstops the three-way case: that arbitration bails out
    when the boxes overlap too little, and nothing downstream noticed.
    """
    dress = effective.get("dress")
    pair = [effective[k] for k in ("top", "bottom") if k in effective]
    if dress is None or not pair:
        return frozenset()
    return frozenset({"top", "bottom"}) if dress >= max(pair) else frozenset({"dress"})


def _flags_from_detections(detections):
    best = {}
    for d in detections:
        if d["score"] > best.get(d["label"], 0.0):
            best[d["label"]] = d["score"]

    # Kept separate from best[] so result["scores"] still reports everything the
    # detector saw - the report page needs that to explain a decision.
    effective = {label: score for label, score in best.items()
                 if score >= CLASS_THRESHOLDS.get(label, 0.0)}
    suppressed = _dress_vs_pair_loser(effective)

    def present(label):
        return label in effective and label not in suppressed

    return {
        "headwear": present("hat"),
        "footwear": present("shoes"),
        "bag": present("bag"),
        "outer": present("outer"),
        # predict_image(resolve_dress=True) already drops whichever side of the
        # dress-vs-top+bottom conflict loses on hue, so a surviving "dress" is
        # the detector's verdict that this outfit is one garment, and a
        # surviving top/bottom pair is its verdict that it is two.
        "one_piece": present("dress"),
        "top": present("top"),
        "bottom": present("bottom"),
        "scores": {label: round(score, 4) for label, score in best.items()},
    }


def _box_inside_frac(box, person_box):
    """Fraction of `box`'s own area that lies inside `person_box`."""
    x0, y0 = max(box[0], person_box[0]), max(box[1], person_box[1])
    x1, y1 = min(box[2], person_box[2]), min(box[3], person_box[3])
    inter = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    area = (box[2] - box[0]) * (box[3] - box[1])
    return inter / area if area > 0 else 0.0


def _box_contains(outer, inner, tol):
    return (outer[0] <= inner[0] + tol and outer[1] <= inner[1] + tol
            and outer[2] >= inner[2] - tol and outer[3] >= inner[3] - tol)


def _rescue_person_box_for_face(person_model, person_pre, image, person_boxes, face_bbox,
                                other_face_bboxes=()):
    """Nhận lại box người bị loại vì điểm thấp, khi nó ôm khuôn mặt chủ thể sát hơn mọi
    box còn sống.

    Người đứng sau trong ảnh selfie hay bị chấm dưới PERSON_SCORE_THRESHOLD=0.5 vì bị
    che. Đo trên ảnh thật: chủ tài khoản đứng sau chấm 0.4346 nên bị loại, chỉ còn box
    của người phụ nữ phía trước (0.9996) - mà khuôn mặt anh ta lại nằm gọn trong box đó,
    nên bước gán mặt→người không có lựa chọn nào khác, `persons` ra 1, không cô lập gì,
    và tủ đồ nhận `tank tops` + `short skirts` của cô ấy.

    Khuôn mặt đã khớp là bằng chứng độc lập và mạnh hơn hẳn điểm của detector người, nên
    khi có một box bị loại vừa chứa khuôn mặt đó vừa NHỎ HƠN box đang thắng, nhận nó lại.
    Đo trên 59 ảnh của 3 job: đúng **1** ảnh thoả - chính ảnh hỏng - không ảnh nào khác
    bị đụng.

    Chỉ cứu khi box đang thắng CHỨA THÊM khuôn mặt của người khác - đó mới là dấu hiệu nó
    thuộc về người đứng trước. Nếu nó chỉ chứa mỗi khuôn mặt chủ thể thì nó đã là box của
    chủ thể, và một box điểm thấp nhỏ hơn thường chỉ là mảnh vỡ của CHÍNH người đó: ảnh
    0646, box đúng [0,1976,871,3586] (0.969) bị thay bằng mảnh [208,1999,872,2942] (0.350)
    cụt vai và thân dưới, SAM chỉ lấy được mặt và bàn tay, D1 vẽ ra đầu người làm "áo".
    Ca gốc (9668) vẫn được cứu: box người phụ nữ chứa cả khuôn mặt cô ấy.
    """
    tol = 0.05 * (face_bbox[3] - face_bbox[1])
    current = sorted((b for b in person_boxes if _box_contains(b, face_bbox, tol)),
                     key=_box_area)
    if current and not any(
            _box_contains(current[0], other, 0.05 * (other[3] - other[1]))
            for other in other_face_bboxes):
        return None
    tighter = [
        b for score, b in byface.scored_person_boxes(person_model, person_pre, image,
                                                     PERSON_RESCUE_FLOOR)
        if score <= clothing.PERSON_SCORE_THRESHOLD
        and _box_contains(b, face_bbox, tol)
        and (not current or _box_area(b) < _box_area(current[0]))
    ]
    return min(tighter, key=_box_area) if tighter else None


def _owned_by_subject(item_box, target_box, person_boxes):
    """Món này có phải của chủ thể không, khi nhiều người cùng chứa nó.

    "Nằm >= 80% trong box chủ thể" một mình là không đủ: trong ảnh selfie, người chụp ở
    tiền cảnh có box choán 42% khung hình và NUỐT TRỌN người đứng sau. Đo trên ảnh thật,
    chiếc áo polo của người phía sau nằm 100% trong box chủ thể - nên bị hợp vào mask,
    sống sót qua bước bôi xám, và D1 vẽ lại áo của NGƯỜI KHÁC vào tủ đồ của chủ thể.

    Chủ sở hữu là người có box NHỎ NHẤT còn chứa được món đồ - cùng nguyên tắc
    "box ôm sát nhất thắng" mà match_face_to_person_box() dùng. Cái áo polo đó nằm 91.5%
    trong box của người kia (17% khung hình), nhỏ hơn hẳn box chủ thể, nên thuộc về anh
    ta; còn áo và túi của chính chủ thể chỉ đạt 24-27% trong box người kia nên không bị
    cướp.
    """
    owners = [b for b in person_boxes
              if _box_inside_frac(item_box, b) >= ITEM_INSIDE_PERSON_FRAC]
    if not owners:
        return False
    return min(owners, key=_box_area) == target_box


def _subject_mask(sam_model, sam_processor, image, target_box, item_boxes,
                  person_boxes=None):
    """Pixel mask of the subject INCLUDING the things they are wearing/carrying.

    SAM prompted with a person box returns only the PERSON. Anything carried is a
    separate object to it, so isolate_person() used to paint it out: measured on
    2026_02_18_16_30_05_IMG_0876.JPG, 99.1% of the subject's crossbody bag
    (150,960 of 152,391 px) was painted grey, and binary_fill_holes() could not
    bring it back - the bag sits on the silhouette edge, so the gap it leaves
    connects to the background instead of being an enclosed hole (fill_holes
    recovered 0.17% of the mask). The detector then scored that bag 0.2106 on the
    isolated image against 0.6758 on the original.

    Fix: the caller runs the garment detector on the ORIGINAL image first and
    passes its boxes in here. Every box the subject owns (_owned_by_subject) is
    segmented too and unioned into the person's mask - so the bag survives the
    background paint, while another person's clothes do not.

    All the prompts go in a single batched SAM call: the expensive part is the ViT
    image encoder over the whole frame, and the mask decoder per extra box is
    cheap. Measured (CPU, 3024x4032): 1 box 2.46s, 5 boxes batched 2.78s, but 5
    boxes as separate calls 12.25s."""
    people = person_boxes or [target_box]
    inside = [b for b in item_boxes if _owned_by_subject(b, target_box, people)]
    masks = byface.segment_boxes(sam_model, sam_processor, image, [target_box] + inside, DEVICE)
    mask = binary_fill_holes(masks[0])
    for item_mask in masks[1:]:
        mask |= item_mask
    return mask


def detect_worn_items(image_path, selfie_path=None, threshold=None,
                      face_match_threshold=None, save_isolated_to=None):
    """Return the item flags build_prompt() needs, plus the raw per-label scores.

    selfie_path: optional reference photo of the person whose outfit should be
        extracted. Only needed when the input holds several people and the
        subject isn't the biggest one in frame.
    threshold: detector confidence floor (default THRESHOLD).
    face_match_threshold: minimum ArcFace cosine similarity for the selfie match
        (default FACE_MATCH_THRESHOLD). Below it no face is accepted as the
        subject, rather than risking someone else's clothes.
    save_isolated_to: optional path to write the SAM-isolated image to, for
        checking the mask by eye (the --save-isolated of the other module)."""
    threshold = THRESHOLD if threshold is None else threshold
    face_match_threshold = (
        FACE_MATCH_THRESHOLD if face_match_threshold is None else face_match_threshold
    )

    cloth_model, procs_full, procs_crop, person_model, person_pre = load_clothing_models()
    image = Image.open(image_path).convert("RGB")
    timing = {}

    t = time.time()
    person_boxes = byface.get_all_person_boxes(person_model, person_pre, image)
    timing["person_detect"] = time.time() - t

    target_box, face_similarity = None, None
    if selfie_path:
        t = time.time()
        face_app = _load_face_app()
        timing["face_load"] = time.time() - t
        t = time.time()
        # Cached per selfie file inside find_reference_embedding, so this is only
        # paid once per worker process rather than once per image.
        ref_embedding = byface.find_reference_embedding(face_app, Path(selfie_path))
        timing["face_ref_embed"] = time.time() - t
        t = time.time()
        face, face_similarity, all_faces = byface.match_face_in_group(
            face_app, image, ref_embedding, return_all=True)
        timing["face_match_group"] = time.time() - t
        if face is None or face_similarity < face_match_threshold:
            raise ValueError(
                f"No face in {image_path} matches the selfie "
                f"(best similarity={face_similarity:.3f}, need >= {face_match_threshold})"
            )
        rescued = _rescue_person_box_for_face(
            person_model, person_pre, image, person_boxes, face.bbox.tolist(),
            [f.bbox.tolist() for f in all_faces if f is not face])
        if rescued is not None:
            person_boxes = person_boxes + [rescued]
        target_box = byface.match_face_to_person_box(face.bbox.tolist(), person_boxes)
    elif len(person_boxes) > 1:
        # No reference face: assume the subject of an outfit-extraction photo is
        # the person occupying the most of the frame.
        target_box = max(person_boxes, key=_box_area)

    isolated_by_sam = target_box is not None and len(person_boxes) > 1
    if isolated_by_sam:
        # Same reasoning as detect_clothing_by_face.py: with a single person there
        # is nobody to filter out, and painting the background grey measurably
        # shifts the detector's scores, so SAM only runs when it can actually help.
        t = time.time()
        sam_model, sam_processor = _load_sam()
        timing["sam_load"] = time.time() - t
        # First pass on the ORIGINAL image, only to locate the subject's items so
        # the isolation mask can keep them - see _subject_mask(). Detection proper
        # still runs on the isolated image below, because a clean grey background
        # scores better than a crowded one.
        t = time.time()
        pre = clothing.predict_image(
            cloth_model, procs_full, procs_crop, person_model, person_pre,
            Path(image_path), DEVICE, threshold, resolve_dress=True,
        )
        timing["fashion_detect_locate"] = time.time() - t
        t = time.time()
        mask = _subject_mask(sam_model, sam_processor, image, target_box,
                             [d["box"] for d in pre], person_boxes)
        timing["sam_segment"] = time.time() - t
        isolated = byface.isolate_person(image, mask)
        if save_isolated_to:
            isolated.save(save_isolated_to)
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir) / "isolated.png"
            isolated.save(tmp_path)
            t = time.time()
            detections = clothing.predict_image(
                cloth_model, procs_full, procs_crop, person_model, person_pre,
                tmp_path, DEVICE, threshold, resolve_dress=True,
            )
            timing["fashion_detect_final"] = time.time() - t
    else:
        t = time.time()
        detections = clothing.predict_image(
            cloth_model, procs_full, procs_crop, person_model, person_pre,
            Path(image_path), DEVICE, threshold, resolve_dress=True,
        )
        timing["fashion_detect_final"] = time.time() - t
        # Nobody to paint out here, but the background still holds objects the
        # detector will happily report as worn - shoes on the floor behind the
        # subject, for one. Drop whatever does not sit on the subject.
        # SAM isolation was measured as the alternative and lost: greying the
        # background shifted scores enough to change the flags on 12 of these 25
        # photos, losing real garments (a top at 0.642, a dress at 0.494) as well as
        # the false shoes. Filtering by box changes 1 of 25 - only the false one.
        subject_box = target_box or (person_boxes[0] if len(person_boxes) == 1 else None)
        if subject_box is not None:
            detections = [
                d for d in detections
                if _box_inside_frac(d["box"], subject_box) >= SUBJECT_ITEM_MIN_FRAC
            ]

    result = _flags_from_detections(detections)
    result["persons"] = len(person_boxes)
    result["isolated_by_sam"] = isolated_by_sam
    result["device"] = DEVICE
    result["timing"] = {k: round(v, 3) for k, v in timing.items()}
    if face_similarity is not None:
        result["face_similarity"] = round(float(face_similarity), 4)
    return result


def warm_up():
    """Load the always-used models up front (SAM and insightface stay lazy - they
    are only needed for multi-person / selfie inputs)."""
    cloth_model, procs_full, _, _, _ = load_clothing_models()
    with torch.no_grad():
        blank = Image.new("RGB", (512, 768), (128, 128, 128))
        clothing.run_scales(cloth_model, procs_full, blank, DEVICE)
