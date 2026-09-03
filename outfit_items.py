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
from pathlib import Path

import torch
from PIL import Image

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
# on, dress conflict auto-resolved by hue, threshold 0.5 and face-match
# threshold 0.35 - the latter taken straight from that module so the two can't
# drift apart.
MULTI_SCALE = True
THRESHOLD = 0.5
FACE_MATCH_THRESHOLD = byface.FACE_MATCH_THRESHOLD

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


def _flags_from_detections(detections):
    best = {}
    for d in detections:
        if d["score"] > best.get(d["label"], 0.0):
            best[d["label"]] = d["score"]

    return {
        "headwear": "hat" in best,
        "footwear": "shoes" in best,
        "bag": "bag" in best,
        "outer": "outer" in best,
        # predict_image(resolve_dress=True) already drops whichever side of the
        # dress-vs-top+bottom conflict loses on hue, so a surviving "dress" is
        # the detector's verdict that this outfit is one garment, and a
        # surviving top/bottom pair is its verdict that it is two.
        "one_piece": "dress" in best,
        "top": "top" in best,
        "bottom": "bottom" in best,
        "scores": {label: round(score, 4) for label, score in best.items()},
    }


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
    person_boxes = byface.get_all_person_boxes(person_model, person_pre, image)

    target_box, face_similarity = None, None
    if selfie_path:
        face_app = _load_face_app()
        ref_embedding = byface.find_reference_embedding(face_app, Path(selfie_path))
        face, face_similarity = byface.match_face_in_group(face_app, image, ref_embedding)
        if face is None or face_similarity < face_match_threshold:
            raise ValueError(
                f"No face in {image_path} matches the selfie "
                f"(best similarity={face_similarity:.3f}, need >= {face_match_threshold})"
            )
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
        sam_model, sam_processor = _load_sam()
        mask = byface.segment_person(sam_model, sam_processor, image, target_box, DEVICE)
        isolated = byface.isolate_person(image, mask)
        if save_isolated_to:
            isolated.save(save_isolated_to)
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir) / "isolated.png"
            isolated.save(tmp_path)
            detections = clothing.predict_image(
                cloth_model, procs_full, procs_crop, person_model, person_pre,
                tmp_path, DEVICE, threshold, resolve_dress=True,
            )
    else:
        detections = clothing.predict_image(
            cloth_model, procs_full, procs_crop, person_model, person_pre,
            Path(image_path), DEVICE, threshold, resolve_dress=True,
        )

    result = _flags_from_detections(detections)
    result["persons"] = len(person_boxes)
    result["isolated_by_sam"] = isolated_by_sam
    result["device"] = DEVICE
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
