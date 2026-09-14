#!/usr/bin/env python3
"""Terminal test client for the QIE-2511-Extract-Outfit ComfyUI server.

Usage:
    python3 test_extract_outfit.py [--selfie ref.jpg] <input_image> [prompt]
    python3 test_extract_outfit.py --result-image result_v4.png

Uploads the image to ComfyUI, runs it through the Qwen-Image-Edit-2511 (GGUF Q5_K_S)
+ QIE-2511-Extract-Outfit LoRA pipeline, and saves the result next to the input.

The result is a flat-mockup grid of the individual garments, which is then:
  1. split into one square image per item, each segmented off the grid and
     re-composited on a clean background of its own (crop_items), and
  2. run through the Magic Eye wardrobe classifier (classify_crops) to get each
     item's type/category/colour/pattern attributes.

Both steps work on any such grid, not only a freshly generated one: pass
--result-image to skip generation and start from a saved result. That is the way to
exercise them on a machine that cannot host Qwen-Image-Edit-2511 itself.
"""
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import cv2
import numpy as np
import torch
from PIL import Image
from torchvision import transforms as T
from transformers import AutoModelForImageSegmentation

COMFY_URL = "http://127.0.0.1:18188"
UNET_NAME = "qwen-image-edit-2511-Q4_K_M.gguf"
UNET_NAME_FP8 = "qwen_image_edit_2511_fp8mixed.safetensors"
CLIP_NAME = "qwen_2.5_vl_7b_fp8_scaled.safetensors"
VAE_NAME = "qwen_image_vae.safetensors"
LORA_NAME = "QIE-2511-Extract-Outfit-4200.safetensors"
LIGHTNING_LORAS = {
    4: "Qwen-Image-Edit-2511-Lightning-4steps-V1.0-bf16.safetensors",
    8: "Qwen-Image-Edit-2511-Lightning-8steps-V1.0-bf16.safetensors",
}
ITEM_SERVICE_URL = "http://127.0.0.1:18189"
# Wardrobe attribute classifier applied to each cropped item. The trained weights are
# committed to this repo (models/magic_eye/, via Git LFS) rather than read out of the
# separate MS_Model_Magic_Eye project they came from, so this step runs anywhere the
# repo is cloned - see scripts/export_magic_eye_bundle.py for how they get here.
MAGIC_EYE_BUNDLE = Path(__file__).resolve().parent / "models" / "magic_eye"

# Prompt wording for each item flag detect_worn_items() returns, in the order the
# items are laid into the grid.
ITEM_PHRASES = [
    ("headwear", "hat"),
    ("outer", "jacket/outerwear"),
    ("one_piece", "dress"),
    # Named by body region, not by garment type. The detector's whole vocabulary is
    # bag/bottom/dress/hat/outer/shoes/top - it never says "shirt", "pants" or "skirt",
    # so any such word here is a guess the prompt states as fact, which is the one thing
    # the README's prompt-tuning notes say not to do. It was also drawing skirts on male
    # subjects. "garment" has to stay in the phrase though: with a bare "top"/"bottom"
    # the model drew bags and pouches instead of clothes (see build_prompt).
    ("top", "upper-body garment"),
    ("bottom", "lower-body garment"),
    ("bag", "bag"),
    ("footwear", "shoes"),
]


def detect_worn_items(image_path, selfie=None, threshold=None, face_threshold=None, save_isolated_to=None):
    """Presence check for every item category the outfit-extraction LoRA is unreliable
    at inferring from pixels alone: headwear, footwear, bag, outerwear, and whether the
    worn outfit is a single one-piece dress vs a separate top+bottom (the LoRA defaults
    to always splitting into "top" + "skirt" even for a genuine one-piece dress unless
    told otherwise). Nothing in the item list build_prompt() constructs is
    hardcoded/assumed present - every category is classified up front so the prompt can
    state a fact ("this person is/isn't wearing X") instead of asking the diffusion
    model to guess presence from the image, which it does inconsistently (see project
    history).

    The detection itself is outfit_items.detect_worn_items(): a real fashion object
    detector (yainage90/fashion-object-detection) with multi-scale + person-crop TTA,
    hue-based dress-vs-top+bottom arbitration, and SAM person isolation for photos with
    more than one person in them - the same stack detect_clothing_by_face.py uses, and
    a replacement for the earlier CLIP ViT-B/32 zero-shot check. It uses the GPU when
    there is one, like detect_clothing_by_face.py does; start the service (or this
    script) with OUTFIT_ITEMS_DEVICE=cpu if that VRAM is needed by ComfyUI instead.

    The item list is whatever the detector actually localises - nothing is assumed
    present, and nothing missing is invented either. Pass --detect-threshold below the
    0.4 default if real garments are still being dropped.

    Talks to the item_detector supervisor service (models loaded once, kept warm - see
    item_detector_service.py) so repeated runs pay only the inference cost instead of
    the model-load cost every time. Falls back to running the detector in this process
    if that service isn't running."""
    try:
        payload = json.dumps({
            "image_path": image_path, "selfie_path": selfie,
            "threshold": threshold, "face_match_threshold": face_threshold,
            "save_isolated_to": save_isolated_to,
        }).encode()
        req = urllib.request.Request(
            f"{ITEM_SERVICE_URL}/detect", data=payload, headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        # The service is up and rejected the request (e.g. no face in the image matches
        # the selfie) - retrying the same work in-process would fail identically.
        # Raise rather than exit: ai-server's worker calls this in-process and must be
        # free to fall back to "largest person in frame" instead of dying.
        raise RuntimeError(f"item detection failed: {e.read().decode()}") from e
    except (urllib.error.URLError, ConnectionError, OSError):
        print("  (item_detector service unreachable, loading the detector in-process instead)")
        import outfit_items
        return outfit_items.detect_worn_items(
            image_path, selfie_path=selfie,
            threshold=threshold, face_match_threshold=face_threshold,
            save_isolated_to=save_isolated_to,
        )


def _grid_positions(n):
    """Exactly n position labels - never more. A previous version always emitted full
    2-column rows (e.g. 4 labels for n=3 items), and callers zip()-truncated the extra
    one away, but the prompt text still read "row 1 left, row 1 right, row 2 left" -
    which implies a 2x2 grid with one cell left unmentioned rather than a 3-item layout.
    The model tended to fill that implied-but-unlabeled cell with whatever else it saw
    in the reference photo (e.g. a bag the detector hadn't flagged as present), instead
    of leaving it empty. Labelling a lone trailing item "center" instead of "left"
    avoids implying an unlabeled partner cell exists."""
    full_rows, remainder = divmod(n, 2)
    labels = []
    for row in range(1, full_rows + 1):
        labels.append(f"row {row} left")
        labels.append(f"row {row} right")
    if remainder:
        labels.append(f"row {full_rows + 1} center")
    return labels


def _grid_shape_desc(n):
    """State the grid's actual size up front (e.g. "a 2x2 grid") instead of leaving the
    model to infer it purely from the per-item position labels. Tried a "do not repeat
    any item" instruction first (measured: zero effect on a real regression - a 1-item
    result still came back with 2 extra hallucinated copies of a bag visible in the
    reference photo) - the model was filling a grid shape it assumed by itself rather
    than disobeying an explicit "don't repeat" instruction, so telling it the shape
    directly should remove the assumption instead of fighting its output after the fact.

    An EVEN item count fills an n/2 x 2 grid exactly (no partial row), so a plain "RxC
    grid" statement is accurate on its own.
    An ODD item count has no rectangle that fits exactly - stating a fixed RxC here would
    require either padding an existing cell or leaving one unlabeled (the exact bug
    _grid_positions() above already fixes for the per-item labels). So the shape is
    spelled out row by row instead: full rows of 2 side by side, then one final row of
    exactly 1 item - the stated cell count always matches len(items), never more."""
    if n == 1:
        return "a single centered item (no grid)"
    rows, remainder = divmod(n, 2)
    if not remainder:
        return f"a {rows}x2 grid"
    row_word = "row" if rows == 1 else "rows"
    return f"a grid: {rows} {row_word} of 2 items side by side, then 1 final row with exactly 1 item centered"


def prompt_items(detected):
    """The item list the prompt asks for, in grid order (row-major). Split out of
    build_prompt() because the crop step needs the same list to label the crops it
    cuts out of the result: cell k of the generated grid holds items[k]."""
    items = [phrase for key, phrase in ITEM_PHRASES if detected.get(key)]
    if not items:
        # The detector localised nothing at all (bad crop, heavy occlusion, threshold
        # too high). Emitting a prompt with an empty item list would be malformed, so
        # fall back to the two garments any clothed person is wearing - this is the one
        # place presence is assumed rather than detected.
        items = [dict(ITEM_PHRASES)["top"], dict(ITEM_PHRASES)["bottom"]]
    return items


def build_prompt(detected):
    """How the top/bottom items are named was measured three ways, 15 paired
    generations each (5 photos x 3 seeds, same detection and seed, only the wording
    differing), counting items dropped and garment slots filled with an accessory:

        "top/shirt" + "bottom (pants/shorts)"      2 dropped,  2 accessories
        "upper-body garment" + "lower-body ..."    3 dropped,  3 accessories
        "top" + "bottom"                           4 dropped,  9 accessories

    So the region wording ties with naming a subtype, while dropping the noun entirely
    loses badly: with a bare "top"/"bottom" the model drew pouches, briefcases and
    backpacks where clothes belonged, and once returned one item out of three. "top"
    and "bottom" are not garment nouns in English, so there is nothing to anchor what
    gets drawn.

    The region wording wins on not lying instead: the detector only reports which body
    region has something on it, so "shirt"/"pants"/"skirt" were guesses the prompt
    stated as fact - and with "skirt" named on every male subject's prompt, skirts got
    drawn on men. Naming the region says exactly what is known and leaves the garment
    type to the model, which also removes gender from this prompt entirely.

    Note what these numbers do *not* cover: the layout metrics alone (asked vs found)
    are blind to this failure - the bare "top"/"bottom" runs had the same item counts
    as the others and simply drew the wrong objects. Judge a wording change by what
    lands in each cell, not by how many cells were filled.

    Gender as a *sentence* was also tried and rejected: appending "Men's clothing." over
    9 photos pushed items drawn 23 -> 25 and count mismatches 1 -> 2, drawing one pouch
    twice and dropping a pair of trousers. This is a LoRA-conditioned diffusion model -
    an extra clause competes with the layout instructions rather than refining them."""
    items = prompt_items(detected)
    n = len(items)
    placements = ", ".join(f"{item} in {pos}" for item, pos in zip(items, _grid_positions(n)))
    item_word = "item" if n == 1 else "items"
    parts = [
        f"Arrange in {_grid_shape_desc(n)}, exactly {n} {item_word} total, one item per "
        f"cell: {placements}. Plain white background, each item placed separately with a "
        "wide gap of clear white space between them, no touching, no overlapping, nothing "
        "else in the frame."
    ]
    # Only describe how the bag should be laid out when a bag was actually detected -
    # naming/describing a category that isn't confirmed present (even to say how it
    # should look) measurably increases the chance the model draws one anyway (see
    # README "Prompt-tuning notes"). This line used to be unconditional, which is why
    # a bag kept appearing in results even when detected["bag"] was False.
    if detected.get("bag"):
        parts.append(
            "The bag lies flat on its own with its strap coiled neatly beside it, not "
            "worn or draped over any other item."
        )
    parts.append("Professional flat mockup photography.")
    return " ".join(parts)


def upload_image(path):
    boundary = uuid.uuid4().hex
    with open(path, "rb") as f:
        data = f.read()
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="image"; filename="{path.split("/")[-1]}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"{COMFY_URL}/upload/image", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    return json.loads(urllib.request.urlopen(req).read())["name"]


def build_workflow(image_name, prompt, seed=42, lightning_steps=None, fp8=False):
    """lightning_steps: None for full quality (40 steps, cfg 4.0), or 4/8 to chain the
    Lightning distilled-speed LoRA on top (steps=lightning_steps, cfg=1.0).
    fp8: use the native fp8mixed checkpoint (fast fp8 tensor-core matmul) instead of
    the GGUF Q5_K_S quant (which dequantizes on the fly and can't use fp8 tensor cores)."""
    steps = lightning_steps or 40
    cfg = 1.0 if lightning_steps else 4.0

    model_chain = "lora_outfit"
    if fp8:
        unet_node = {"class_type": "UNETLoader", "inputs": {"unet_name": UNET_NAME_FP8, "weight_dtype": "default"}}
    else:
        unet_node = {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": UNET_NAME}}
    nodes = {
        "unet_loader": unet_node,
        "clip_loader": {"class_type": "CLIPLoader", "inputs": {"clip_name": CLIP_NAME, "type": "qwen_image", "device": "default"}},
        "vae_loader": {"class_type": "VAELoader", "inputs": {"vae_name": VAE_NAME}},
        "model_sampling": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["unet_loader", 0], "shift": 3.1}},
        "cfg_norm": {"class_type": "CFGNorm", "inputs": {"model": ["model_sampling", 0], "strength": 1.0, "pre_cfg": False}},
        "lora_outfit": {"class_type": "LoraLoaderModelOnly", "inputs": {"model": ["cfg_norm", 0], "lora_name": LORA_NAME, "strength_model": 1.0}},
        "load_image": {"class_type": "LoadImage", "inputs": {"image": image_name}},
        "kontext_scale": {"class_type": "FluxKontextImageScale", "inputs": {"image": ["load_image", 0]}},
        "text_pos": {"class_type": "TextEncodeQwenImageEditPlus", "inputs": {"clip": ["clip_loader", 0], "vae": ["vae_loader", 0], "image1": ["kontext_scale", 0], "prompt": prompt}},
        "ref_pos": {"class_type": "FluxKontextMultiReferenceLatentMethod", "inputs": {"conditioning": ["text_pos", 0], "reference_latents_method": "index_timestep_zero"}},
        "vae_encode": {"class_type": "VAEEncode", "inputs": {"pixels": ["kontext_scale", 0], "vae": ["vae_loader", 0]}},
        "vae_decode": {"class_type": "VAEDecode", "inputs": {"samples": ["ksampler", 0], "vae": ["vae_loader", 0]}},
        "save_image": {"class_type": "SaveImage", "inputs": {"images": ["vae_decode", 0], "filename_prefix": "extract_outfit_test"}},
    }

    if cfg == 1.0:
        # KSampler already skips the uncond forward pass when cfg==1.0 (comfy/samplers.py
        # math.isclose(cond_scale, 1.0) fast path), so the negative branch's *value* never
        # reaches the model. Only its tensor shape needs to be valid -> zero out the already-
        # computed positive conditioning instead of re-running the ~7B VLM text encoder a
        # second time on the same reference image. Saves ~1-3s per request.
        nodes["negative_cond"] = {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["ref_pos", 0]}}
    else:
        nodes["text_neg"] = {"class_type": "TextEncodeQwenImageEditPlus", "inputs": {"clip": ["clip_loader", 0], "vae": ["vae_loader", 0], "image1": ["kontext_scale", 0], "prompt": ""}}
        nodes["negative_cond"] = {"class_type": "FluxKontextMultiReferenceLatentMethod", "inputs": {"conditioning": ["text_neg", 0], "reference_latents_method": "index_timestep_zero"}}

    if lightning_steps:
        nodes["lora_lightning"] = {
            "class_type": "LoraLoaderModelOnly",
            "inputs": {"model": ["lora_outfit", 0], "lora_name": LIGHTNING_LORAS[lightning_steps], "strength_model": 1.0},
        }
        model_chain = "lora_lightning"

    nodes["ksampler"] = {
        "class_type": "KSampler",
        "inputs": {
            "model": [model_chain, 0], "positive": ["ref_pos", 0], "negative": ["negative_cond", 0],
            "latent_image": ["vae_encode", 0], "seed": seed, "steps": steps, "cfg": cfg,
            "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0,
        },
    }
    return nodes


def download_output(filename, dest):
    """Pull a generated image out of ComfyUI's output folder onto local disk. The
    crop step needs the pixels, not just the filename the /history entry reports."""
    url = f"{COMFY_URL}/view?filename={urllib.parse.quote(filename)}&type=output"
    with urllib.request.urlopen(url) as r:
        data = r.read()
    Path(dest).write_bytes(data)
    return dest


# --- Step 1: cut the generated grid back into individual item images -------------
#
# The generation prompt lays the items out on a plain white background "with a wide
# gap of clear white space between them, no touching, no overlapping, nothing else in
# the frame" - i.e. it *guarantees* the exact condition that makes plain connected-
# component analysis on a background-difference mask sufficient here. No detector is
# needed (and the fashion detector used for the input photo is trained on garments
# worn by a person, not on flat mockups, so it is the weaker tool on this image).

def _background_color(img):
    """Median of a thin border frame. The mockup background is near-white but not
    pure #ffffff (result_v4.png measures (245, 245, 244)), so thresholding against a
    hardcoded white would leave a halo of "foreground" over the whole background."""
    edge = max(2, min(img.shape[:2]) // 200)
    border = np.concatenate([
        img[:edge].reshape(-1, 3), img[-edge:].reshape(-1, 3),
        img[:, :edge].reshape(-1, 3), img[:, -edge:].reshape(-1, 3),
    ])
    return np.median(border, axis=0)


SEGMENTER_MODEL_NAME = "ZhengPeng7/BiRefNet_lite"
_segmenter_cache = {}


def _load_segmenter(device):
    """Lazy singleton, kept warm for the life of the process (the ai-server worker
    handles many jobs per process; a fresh load per job was measured at ~1s)."""
    key = str(device)
    if key not in _segmenter_cache:
        model = AutoModelForImageSegmentation.from_pretrained(
            SEGMENTER_MODEL_NAME, trust_remote_code=True).to(device).eval()
        transform = T.Compose([
            T.Resize((1024, 1024)),
            T.ToTensor(),
            T.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
        ])
        _segmenter_cache[key] = (model, transform)
    return _segmenter_cache[key]


def _reading_order(items):
    """Group into rows first (two items in the same grid row are never vertically
    aligned to the pixel, so a plain sort by y interleaves the columns), then
    left-to-right within each row. items carry a "box" key."""
    items = sorted(items, key=lambda it: it["box"][1])
    rows, current = [], []
    for it in items:
        # starts below every box in the row
        if current and it["box"][1] > min(c["box"][3] for c in current):
            rows.append(current)
            current = []
        current.append(it)
    if current:
        rows.append(current)
    return [it for row in rows for it in sorted(row, key=lambda it: it["box"][0])]


def _regions_within(labels, a, b, gap):
    """True when a's and b's actual pixels come within `gap` of each other.

    Measured on the gap between their *bounding boxes* instead, two items laid out
    diagonally read as touching: on one real generation a shirt (x 39-595) and the
    trousers beside it (x 545-775) overlapped in both axes, so the box gap was 0 and
    they were merged into a single "item", even though the nearest pixel of one was
    49px from the other and the grid was visually perfect. The box gap is a lower
    bound on the pixel distance, so it still works as a cheap prefilter.
    """
    ba, bb = a["box"], b["box"]
    dx = max(0, max(ba[0], bb[0]) - min(ba[2], bb[2]))
    dy = max(0, max(ba[1], bb[1]) - min(ba[3], bb[3]))
    if dx > gap or dy > gap:
        return False

    # Only the neighbourhood of the two regions matters, and cropping it keeps the
    # distance transform off the rest of the frame.
    pad = int(np.ceil(gap)) + 2
    x0 = max(0, min(ba[0], bb[0]) - pad); y0 = max(0, min(ba[1], bb[1]) - pad)
    x1 = min(labels.shape[1], max(ba[2], bb[2]) + pad)
    y1 = min(labels.shape[0], max(ba[3], bb[3]) + pad)
    sub = labels[y0:y1, x0:x1]

    mask_a = np.isin(sub, a["ids"])
    mask_b = np.isin(sub, b["ids"])
    if not mask_a.any() or not mask_b.any():
        return False
    dist = cv2.distanceTransform((~mask_a).astype(np.uint8), cv2.DIST_L2, 3)
    return float(dist[mask_b].min()) <= gap


@torch.no_grad()
def _segment_items(image, device=None, min_area_frac=0.002, merge_gap_frac=0.02):
    """One (box, alpha) per item on the generated flat-mockup, from a class-agnostic
    foreground segmentation. The alpha matte is what keeps a neighbouring item out of
    this item's picture - see _isolate_on_background.

    The mockup is exactly the case this suits: plain background, items laid flat,
    never overlapping - so "which pixels are an item" is the whole question, and
    "which *kind* of item" is D3's job, not this step's. Two earlier approaches
    were measured on the same 9 real generations and both lost to this one:

      - background-diff + connected components: collapsed separate items into one
        blob whenever their edges touched or shared a faint anti-aliased seam.
      - yainage90/fashion-object-detection (the D0b detector, reused here): 5/9.
        It has to *classify* to detect, and its confidence is badly calibrated on
        this synthetic flat-mockup domain - visually identical sandals scored 0.96
        on one generation and 0.04 on another - so real items kept being dropped
        no matter where the threshold went, and per-class overrides only traded
        one failure for another.

    BiRefNet scored 9/9 on the same set at 100ms/image on GPU, and needs no score
    threshold at all, which is what removed that whole class of tuning problem.

    One *item* is not always one blob: a pair of shoes is two, a bag with its strap
    coiled beside it can be two. Blobs whose pixels come within merge_gap_frac of the
    short side of each other are merged back into one item (_regions_within - the
    distance is between the regions themselves, not between their bounding boxes).
    Measured edge-to-edge on six real generations, the two populations do not overlap:

      within one item (shoe pairs)   0.34%, 0.91%, 1.25%
      between two items (closest)    3.18%, 3.30%, 4.77%, 9.77%, 14.89%

    2% sits between them with ~1.6x margin either way. They separate this cleanly
    because the prompt demands "a wide gap of clear white space between them", so
    between-item spacing is deliberate while a split item's is incidental.

    An earlier attempt used the item count from the prompt instead, folding surplus
    blobs into the grid cells the prompt laid out. That was wrong: the count is what
    was *asked for*, not what was drawn, and the generator misses and adds items in
    both directions. On a job where the detector found one garment and the generation
    drew two, it merged a shirt and a skirt into a single "item".
    """
    device = device or ("cuda" if torch.cuda.is_available() else "cpu")
    model, transform = _load_segmenter(device)

    pred = model(transform(image).unsqueeze(0).to(device))[-1].sigmoid().cpu()[0, 0]
    soft = np.asarray(
        Image.fromarray((pred.numpy() * 255).astype(np.uint8)).resize(image.size),
        dtype=np.float32) / 255.0
    binary = (soft > 0.5).astype(np.uint8)

    h, w = binary.shape
    n, labels, stats, _ = cv2.connectedComponentsWithStats(binary, 8)
    groups = [
        {"ids": [i], "box": (s[cv2.CC_STAT_LEFT], s[cv2.CC_STAT_TOP],
                             s[cv2.CC_STAT_LEFT] + s[cv2.CC_STAT_WIDTH],
                             s[cv2.CC_STAT_TOP] + s[cv2.CC_STAT_HEIGHT])}
        for i, s in ((i, stats[i].tolist()) for i in range(1, n))
        if s[cv2.CC_STAT_AREA] >= min_area_frac * h * w
    ]

    gap = merge_gap_frac * min(w, h)
    merged = True
    while merged:
        merged = False
        for i in range(len(groups)):
            for j in range(i + 1, len(groups)):
                if _regions_within(labels, groups[i], groups[j], gap):
                    a, b = groups[i]["box"], groups[j]["box"]
                    groups[i] = {
                        "ids": groups[i]["ids"] + groups[j]["ids"],
                        "box": (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])),
                    }
                    del groups[j]
                    merged = True
                    break
            if merged:
                break

    for g in groups:
        x0, y0, x1, y1 = g["box"]
        member = np.isin(labels[y0:y1, x0:x1], g["ids"]).astype(np.uint8)
        # Grow 2px before applying the soft matte: connectedComponents only labels
        # pixels over the 0.5 threshold, so an item's anti-aliased rim would be cut
        # away and leave a hard, jagged edge. Items are laid out far further apart
        # than this, so it cannot reach a neighbour.
        member = cv2.dilate(member, np.ones((5, 5), np.uint8))
        g["alpha"] = soft[y0:y1, x0:x1] * member

    return _reading_order(groups)


def _isolate_on_background(img, box, alpha, bg, pad_frac=0.08):
    """Lift one item off the grid by its alpha matte and re-composite it, centred, on a
    freshly built background of its own.

    Cutting a square *region* out of the grid instead - the earlier approach - copies
    whatever else falls inside that square. The square has to be as wide as the item is
    tall, so for a narrow upright item it reaches well past the item's own cell: on
    result_v4.png the left sandal's crop pulled in the entire right sandal, and both
    "items" came out as a picture of the same pair. Compositing from the matte makes
    the picture contain exactly one item by construction, whatever the neighbours do.

    Square, and padded rather than cropped tight, because the classifier resizes to a
    fixed 224x224 without preserving aspect ratio (magic_eye's val transform is a plain
    Resize((224, 224))): a tall garment fed in at its own aspect ratio would come out
    squashed horizontally, unlike anything in its training set. The rebuilt background
    is a single flat colour sampled from the grid's own border, so it also drops the
    faint vignetting the generator leaves behind."""
    x0, y0, x1, y1 = box
    a = alpha[..., None]
    blended = img[y0:y1, x0:x1].astype(np.float32) * a + bg * (1.0 - a)

    h, w = blended.shape[:2]
    side = int(round(max(h, w) * (1 + 2 * pad_frac)))
    out = np.empty((side, side, 3), dtype=np.float32)
    out[:] = bg
    top, left = (side - h) // 2, (side - w) // 2
    out[top:top + h, left:left + w] = blended
    return Image.fromarray(out.round().clip(0, 255).astype(np.uint8))


def crop_items(result_path, out_dir, items=None, device=None):
    """Lift every item out of the generated grid into its own square PNG.

    Each item is segmented from the grid class-agnostically (_segment_items) and then
    re-composited, centred, onto a clean background of its own
    (_isolate_on_background), so one output image holds exactly one item.

    items: the phrase list build_prompt() asked for, in grid order, used to name the
    crops. Only trusted when the number of items found matches the number asked for -
    if the model dropped or added a cell, positional names are used instead of
    mislabelling e.g. a bag as "footwear"."""
    result_img = Image.open(result_path).convert("RGB")
    img = np.array(result_img)
    t = time.time()
    found = _segment_items(result_img, device=device)
    print(f"  [crop-segmenter model timing] birefnet={time.time() - t:.3f}s")
    if items and len(items) != len(found):
        print(f"  note: prompt asked for {len(items)} items but {len(found)} were found "
              f"in the result - falling back to positional names")
        items = None
    bg = _background_color(img).astype(np.float32)

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # Drop crops left by an earlier run: the classifier step classifies every image in
    # this folder, so a run that finds fewer items than the last one would otherwise
    # report the previous run's leftovers as part of this outfit.
    for stale in out_dir.glob("[0-9][0-9]_*.png"):
        stale.unlink()

    crops = []
    for i, item in enumerate(found):
        label = items[i] if items else f"item {i + 1}"
        slug = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
        path = out_dir / f"{i + 1:02d}_{slug}.png"
        crop = _isolate_on_background(img, item["box"], item["alpha"], bg)
        crop.save(path)
        crops.append({"label": label, "path": path, "box": item["box"], "size": crop.size})
    return crops


# --- Step 2: classify each crop with the Magic Eye model -------------------------

def classify_crops(crop_dir, bundle=MAGIC_EYE_BUNDLE, device=None):
    """Run every crop through the Magic Eye wardrobe classifier and write
    wardrobe_index.json next to them.

    Two models run per crop, both in the bundle: phase2.pt is the classifier proper
    (SigLIP backbone + the 8 attribute heads), and phase3.pt is a head on top of it
    that turns phase 2's visual embedding and predicted attributes into the 768-d
    text embedding wardrobe search matches against - which is why loading one without
    the other is not a thing. See wardrobe_classifier.py."""
    import wardrobe_classifier

    records = wardrobe_classifier.classify_images(
        Path(crop_dir), bundle_dir=Path(bundle), device=device)
    out_json = Path(crop_dir) / "wardrobe_index.json"
    out_json.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
    return records


def extract_and_classify(result_path, out_dir, items=None, classify=True, device=None,
                         bundle=MAGIC_EYE_BUNDLE):
    print(f"\nCropping items out of {result_path} ...")
    crops = crop_items(result_path, out_dir, items=items, device=device)
    for c in crops:
        print(f"  {c['path'].name}: {c['size'][0]}x{c['size'][1]} from box {c['box']}")
    if not crops:
        print("  no items found in the result image - nothing to classify")
        return crops, []
    if not classify:
        return crops, []

    print(f"\nClassifying {len(crops)} crops with {bundle} ...")
    records = classify_crops(out_dir, bundle=bundle, device=device)
    by_name = {r["image_name"]: r for r in records}
    for c in crops:
        r = by_name.get(c["path"].name)
        if not r:
            continue
        colors = ", ".join(f"{x['color']} {x['confidence']}%" for x in r["color"])
        print(f"\n  {c['path'].name}  (prompted as: {c['label']})")
        print(f"    type={r['type']}  category={r['category']}/{r['sub_category']}  gender={r['gender']}")
        print(f"    color=[{colors}]  neck={r['neck']}  sleeve={r['sleeve']}  pattern={r['pattern']}")
        print(f"    description: {r['original_text']}")
    return crops, records


def main():
    args = sys.argv[1:]
    lightning_steps = None
    if "--lightning4" in args:
        lightning_steps = 4
        args.remove("--lightning4")
    elif "--lightning8" in args:
        lightning_steps = 8
        args.remove("--lightning8")
    fp8 = "--fp8" in args
    if fp8:
        args.remove("--fp8")
    seed = 42
    if "--seed" in args:
        i = args.index("--seed")
        seed = int(args[i + 1])
        del args[i:i + 2]
    selfie = None
    if "--selfie" in args:
        i = args.index("--selfie")
        selfie = args[i + 1]
        del args[i:i + 2]
    threshold = face_threshold = None
    if "--detect-threshold" in args:
        i = args.index("--detect-threshold")
        threshold = float(args[i + 1])
        del args[i:i + 2]
    if "--face-threshold" in args:
        i = args.index("--face-threshold")
        face_threshold = float(args[i + 1])
        del args[i:i + 2]
    # Use an already-generated grid instead of running Qwen-Image-Edit-2511. The
    # extraction model needs ~30GB of weights resident, so on a machine that can't
    # host it the crop + classify steps are still testable against a saved result
    # (e.g. --result-image result_v4.png).
    result_image = None
    if "--result-image" in args:
        i = args.index("--result-image")
        result_image = args[i + 1]
        del args[i:i + 2]
    crop_dir = None
    if "--crop-dir" in args:
        i = args.index("--crop-dir")
        crop_dir = args[i + 1]
        del args[i:i + 2]
    bundle = MAGIC_EYE_BUNDLE
    if "--bundle" in args:
        i = args.index("--bundle")
        bundle = Path(args[i + 1])
        del args[i:i + 2]
    classify = "--no-classify" not in args
    if not classify:
        args.remove("--no-classify")
    classify_device = None
    if "--classify-cpu" in args:
        import torch
        classify_device = torch.device("cpu")
        args.remove("--classify-cpu")

    usage = (f"Usage: {sys.argv[0]} [--lightning4|--lightning8] [--fp8] [--seed N] "
             f"[--selfie ref.jpg] [--detect-threshold F] [--face-threshold F] "
             f"[--crop-dir DIR] [--bundle models/magic_eye] [--no-classify] [--classify-cpu] "
             f"<input_image> [prompt]\n"
             f"       {sys.argv[0]} --result-image result_v4.png [--crop-dir DIR] "
             f"[--no-classify]   (skip generation, crop+classify an existing grid)")

    # Generation skipped: crop and classify the supplied grid and stop. Item names
    # from the prompt aren't available here, so the crops get positional names.
    if result_image:
        out_dir = Path(crop_dir) if crop_dir else Path(result_image).with_name(
            f"{Path(result_image).stem}_items")
        extract_and_classify(result_image, out_dir, classify=classify,
                             device=classify_device, bundle=bundle)
        return

    if len(args) < 1:
        print(usage)
        sys.exit(1)
    image_path = args[0]
    # Where to save the SAM-isolated subject if the photo has more than one person -
    # next to the input image, so it's easy to find and inspect by eye.
    src = Path(image_path)
    isolated_path = src.with_name(f"{src.stem}_isolated.png")

    items = None
    if len(args) > 1:
        prompt = args[1]
        generation_image_path = image_path
    else:
        print("Detecting worn items (fashion-object-detection + SAM)...")
        t_det = time.time()
        try:
            detected = detect_worn_items(
                image_path, selfie=selfie, threshold=threshold, face_threshold=face_threshold,
                save_isolated_to=str(isolated_path),
            )
        except RuntimeError as e:
            print(e)
            sys.exit(1)
        flags = ", ".join(f"{key}={detected.get(key)}" for key, _ in ITEM_PHRASES)
        print(f"  {flags} ({time.time() - t_det:.1f}s on {detected.get('device', '?')})")
        scores = detected.get("scores") or {}
        print(f"  detector: {scores}, persons={detected.get('persons')}"
              + (", isolated with SAM" if detected.get("isolated_by_sam") else "")
              + (f", face similarity={detected['face_similarity']}" if "face_similarity" in detected else ""))
        d0b_timing = detected.get("timing") or {}
        if d0b_timing:
            print("  [D0b model timing] " + ", ".join(f"{k}={v}s" for k, v in d0b_timing.items()))
        items = prompt_items(detected)
        prompt = build_prompt(detected)

        # More than one person in the photo: the extraction model would otherwise see
        # everyone in the reference image and can pull a garment from the wrong
        # person when the prompt only names a category ("bottom (skirt/pants)")
        # without saying whose. Generate from the SAM-isolated single-subject image
        # instead - same one detect_worn_items() just used to decide what's present -
        # so there's no other person's clothes left for it to draw from.
        if detected.get("isolated_by_sam") and isolated_path.exists():
            generation_image_path = str(isolated_path)
            print(f"  {detected['persons']} people in frame - generating from the "
                  f"SAM-isolated subject instead of the original: {generation_image_path}")
        else:
            generation_image_path = image_path

    print(f"Uploading {generation_image_path} ...")
    image_name = upload_image(generation_image_path)
    print(f"Uploaded as {image_name}")

    client_id = uuid.uuid4().hex
    workflow = build_workflow(image_name, prompt, seed=seed, lightning_steps=lightning_steps, fp8=fp8)
    print(f"Using seed={seed} (pass --seed N to retry with a different layout/result)")
    if lightning_steps:
        print(f"Using Lightning {lightning_steps}-step LoRA (cfg=1.0)")
    if fp8:
        print("Using native fp8mixed checkpoint (fp8 tensor-core matmul, no GGUF dequant)")
    payload = json.dumps({"prompt": workflow, "client_id": client_id}).encode()
    req = urllib.request.Request(f"{COMFY_URL}/prompt", data=payload, headers={"Content-Type": "application/json"})

    t0 = time.time()
    try:
        resp = json.loads(urllib.request.urlopen(req).read())
    except urllib.error.HTTPError as e:
        print("ComfyUI rejected the workflow:")
        print(e.read().decode())
        sys.exit(1)
    prompt_id = resp["prompt_id"]
    print(f"Queued prompt_id={prompt_id}, waiting for completion...")

    while True:
        with urllib.request.urlopen(f"{COMFY_URL}/history/{prompt_id}") as r:
            hist = json.loads(r.read())
        if prompt_id in hist:
            entry = hist[prompt_id]
            status = entry.get("status", {})
            if status.get("completed"):
                break
            if status.get("status_str") == "error":
                print("Generation failed:", json.dumps(status, indent=2))
                sys.exit(1)
        time.sleep(2)
        print(f"  ...still running ({time.time() - t0:.0f}s elapsed)")

    elapsed = time.time() - t0
    outputs = entry["outputs"]
    saved = []
    for node_id, out in outputs.items():
        for img in out.get("images", []):
            saved.append(img["filename"])
    print(f"Done in {elapsed:.1f}s. Output image(s): {saved}")
    if not saved:
        print("No image output produced.")
        return

    result_path = src.with_name(f"{src.stem}_result.png")
    download_output(saved[0], result_path)
    print(f"Saved result to {result_path}")

    out_dir = Path(crop_dir) if crop_dir else src.with_name(f"{src.stem}_items")
    extract_and_classify(result_path, out_dir, items=items, classify=classify,
                         device=classify_device, bundle=bundle)


if __name__ == "__main__":
    main()
