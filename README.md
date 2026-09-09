# QIE-2511-Extract-Outfit — ComfyUI server

ComfyUI server setup to run [prithivMLmods/QIE-2511-Extract-Outfit](https://huggingface.co/prithivMLmods/QIE-2511-Extract-Outfit),
a LoRA on top of `Qwen-Image-Edit-2511` that extracts garments from a photo into a
clean flat-lay mockup — and the pipeline around it that turns one photo of a person
into a set of individually classified wardrobe items.

## The pipeline

```
 photo of a person
        │
        ▼
 ┌──────────────────────┐  fashion-object-detection + SAM + ArcFace
 │ 1. detect worn items │  → which categories are actually present
 └──────────────────────┘    (hat? outerwear? dress vs top+bottom? bag? shoes?)
        │  item list
        ▼
 ┌──────────────────────┐  Qwen-Image-Edit-2511 + QIE-2511-Extract-Outfit LoRA
 │ 2. extract outfit    │  → one flat-mockup grid, each garment in its own cell
 └──────────────────────┘    on plain white
        │  result grid
        ▼
 ┌──────────────────────┐  BiRefNet_lite foreground segmentation
 │ 3. crop items        │  → one square PNG per item
 └──────────────────────┘
        │  crops
        ▼
 ┌──────────────────────┐  Magic Eye phase 2 + phase 3 (models/magic_eye/)
 │ 4. classify          │  → type, category, colour, pattern, neck, sleeve,
 └──────────────────────┘    gender + 768-d embeddings → wardrobe_index.json
```

All four stages run from one command:

```bash
python3 test_extract_outfit.py --lightning4 --fp8 photo.jpg
```

Stage 1 exists because the LoRA guesses presence badly; stage 3 is cheap because
stage 2's prompt *guarantees* a plain background with wide gaps; stage 4 is what
turns a picture of a garment into a wardrobe record. Each stage is documented in
its own section below.

## Models required

Place these in the corresponding `ComfyUI/models/` subfolders:

| File | Folder | Source |
|---|---|---|
| `qwen_image_edit_2511_fp8mixed.safetensors` | `diffusion_models/` | [Comfy-Org/Qwen-Image-Edit_ComfyUI](https://huggingface.co/Comfy-Org/Qwen-Image-Edit_ComfyUI/blob/main/split_files/diffusion_models/qwen_image_edit_2511_fp8mixed.safetensors) |
| `qwen-image-edit-2511-Q5_K_S.gguf` / `Q4_K_M.gguf` (optional, slower) | `unet/` | [unsloth/Qwen-Image-Edit-2511-GGUF](https://huggingface.co/unsloth/Qwen-Image-Edit-2511-GGUF) |
| `qwen_2.5_vl_7b_fp8_scaled.safetensors` | `text_encoders/` | [Comfy-Org/Qwen-Image_ComfyUI](https://huggingface.co/Comfy-Org/Qwen-Image_ComfyUI/blob/main/split_files/text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors) |
| `qwen_image_vae.safetensors` | `vae/` | [Comfy-Org/Qwen-Image_ComfyUI](https://huggingface.co/Comfy-Org/Qwen-Image_ComfyUI/blob/main/split_files/vae/qwen_image_vae.safetensors) |
| `QIE-2511-Extract-Outfit-4200.safetensors` | `loras/` | [prithivMLmods/QIE-2511-Extract-Outfit](https://huggingface.co/prithivMLmods/QIE-2511-Extract-Outfit) |
| `Qwen-Image-Edit-2511-Lightning-4steps-V1.0-bf16.safetensors` / `8steps` | `loras/` | [lightx2v/Qwen-Image-Edit-2511-Lightning](https://huggingface.co/lightx2v/Qwen-Image-Edit-2511-Lightning) |

Also required: the [ComfyUI-GGUF](https://github.com/city96/ComfyUI-GGUF) custom node (only needed if using the GGUF unet variant).

`test_extract_outfit.py`'s auto-prompt path additionally needs `transformers`,
`torchvision`, `scipy` and `opencv-python` (mostly pulled in by ComfyUI's own
requirements) for the worn-item detector and the crop step, plus `insightface` +
`onnxruntime` for face matching:

```bash
pip install transformers torchvision scipy opencv-python
pip install insightface onnxruntime      # only needed for --selfie
```

The detector models (`yainage90/fashion-object-detection`, `facebook/sam-vit-base`,
`insightface/buffalo_s`) download themselves on first use into `HF_HOME` /
`~/.insightface` — nothing to place by hand. So do `ZhengPeng7/BiRefNet_lite` (the
crop step's segmentation model, loaded with `trust_remote_code`) and
`google/siglip-base-patch16-224`, the backbone architecture the classifier is built
on (its weights are overwritten by `models/magic_eye/phase2.pt`).

### The classifier is in this repo — clone it with Git LFS

The wardrobe classifier's trained weights are committed under `models/magic_eye/`
(203 MB), so stage 4 needs nothing placed by hand — but `phase2.pt` is 195 MB, over
GitHub's 100 MB blob limit, so the two `.pt` files are **Git LFS** objects:

```bash
git lfs install     # once per machine
git clone https://github.com/ndq809/qie-outfit-comfyui-server.git
# already cloned without LFS? →
git lfs pull
```

Without this they arrive as one-line pointer files and the classification step stops
with a message saying so. See [`models/magic_eye/README.md`](models/magic_eye/README.md)
for what each file is.

## Files

| File | What it is |
|---|---|
| `test_extract_outfit.py` | Terminal client, and the whole pipeline: detects the worn items, builds the prompt, runs the ComfyUI workflow, crops the result into items, classifies them. |
| `outfit_items.py` | The worn-item detector used by the auto-prompt path (stage 1). Wraps the two scripts below. |
| `item_detector_service.py` | Keeps those models warm behind `127.0.0.1:18189` so repeat runs skip the load cost. |
| `detect_clothing_yolo.py` | Standalone garment detector (multi-scale + person-crop TTA, hue dress arbitration). |
| `detect_clothing_by_face.py` | Standalone: picks one person out of a group photo by face, isolates them with SAM, then detects their clothes. |
| `wardrobe_classifier.py` | Stage 4: the Magic Eye classifier. Also runnable on any folder of item images on its own. |
| `magic_eye/` | Model definitions for that classifier (`model_phase2.py`, `model_phase3.py`, …), vendored from the MS_Model_Magic_Eye training project. |
| `models/magic_eye/` | Its trained weights + taxonomy — the committed bundle. [Details](models/magic_eye/README.md). |
| `scripts/export_magic_eye_bundle.py` | Regenerates that bundle from the training project after a retrain. |
| `result_v4.png` | A saved extraction result, committed as a fixture so stages 3–4 can be exercised without the generation model. |

`outfit_items.py` imports `detect_clothing_yolo.py` and `detect_clothing_by_face.py`,
so those three must sit in the same folder.

## Service setup (Vast.ai / supervisor)

Two supervisor services:

- **ComfyUI** — `scripts/comfyui.sh` (`/opt/supervisor-scripts/comfyui.sh`), runs
  `python main.py --listen 127.0.0.1 --port 18188`. Config: `scripts/comfyui.conf`.
  Expose via the instance's Caddy auth edge by adding a `ComfyUI` entry to
  `/etc/portal.yaml` (`external_port: 10100`, `internal_port: 18188`), then
  `supervisorctl reread && supervisorctl update`.
- **Worn-item detector** — `scripts/item_detector.sh`
  (`/opt/supervisor-scripts/item_detector.sh`), runs `item_detector_service.py` on
  `127.0.0.1:18189`. No portal entry needed (internal use only by
  `test_extract_outfit.py`). Config: `scripts/item_detector.conf`. It uses the GPU by
  default and therefore holds VRAM while running — on a single-GPU box that is also
  serving ComfyUI, add `export OUTFIT_ITEMS_DEVICE=cpu` to `scripts/item_detector.sh`
  before installing it (see [the timing table](#auto-detected-prompt-no-hardcoded-items)
  for what that costs).

Install both the same way:
```bash
cp scripts/comfyui.sh scripts/item_detector.sh /opt/supervisor-scripts/
chmod +x /opt/supervisor-scripts/comfyui.sh /opt/supervisor-scripts/item_detector.sh
cp scripts/comfyui.conf scripts/item_detector.conf /etc/supervisor/conf.d/
supervisorctl reread && supervisorctl update
```

Check the detector came up (it reports which device it loaded on):
```bash
curl -s http://127.0.0.1:18189/health      # {"status": "ok", "device": "cuda"}
```

If an older install still has the `clip_classifier` service (it used the same port),
remove it first:
```bash
supervisorctl stop clip_classifier
rm /etc/supervisor/conf.d/clip_classifier.conf /opt/supervisor-scripts/clip_classifier.sh
supervisorctl reread && supervisorctl update
```

## Usage

```bash
source /venv/main/bin/activate
python3 test_extract_outfit.py --lightning4 --fp8 [--seed N] [--selfie ref.jpg] \
    [--detect-threshold F] [--face-threshold F] [--crop-dir DIR] [--bundle DIR] \
    [--no-classify] [--classify-cpu] <input_image> ["<custom prompt>"]
```

One run produces, next to the input photo:

| Output | What |
|---|---|
| `<input>_result.png` | the generated flat-mockup grid, downloaded from ComfyUI |
| `<input>_items/NN_<item>.png` | one square crop per item, named after what the prompt asked for in that cell |
| `<input>_items/wardrobe_index.json` | the classified attributes + embeddings, one record per crop |
| `<input>_isolated.png` | the SAM-isolated subject, only for photos with more than one person |

Flags:
- `--fp8` — use the native fp8mixed checkpoint (recommended: fast, numerically stable).
- `--lightning4` / `--lightning8` — chain the Lightning distilled-speed LoRA (4 or 8 steps, cfg 1.0).
- No flag — full 40-step generation (cfg 4.0), GGUF unet, slowest but no distillation LoRA involved.
- `--seed N` — sampler seed (default 42). The LoRA doesn't follow layout/presence
  instructions with 100% reliability on every seed (see below) — re-run with a
  different seed if one result comes out with an overlap or a missed item.
- `--selfie ref.jpg` — reference photo of the person whose outfit should be extracted.
  Only useful when the input contains several people and the subject isn't the largest
  one in frame; the subject is picked by ArcFace face similarity instead.
- `--detect-threshold F` — detector confidence floor (default 0.4). Lower it to ~0.35 if
  a garment that is clearly in the photo is still missing from the item list. The default
  used to be 0.5, which measurably dropped real garments (trousers at 0.4218 on
  `2026_02_18_16_30_05_IMG_0876.JPG` were missing from the item list entirely, and the
  generation duplicated the bag to fill the layout).
- `--face-threshold F` — minimum ArcFace cosine similarity for `--selfie` (default 0.35).
- `--crop-dir DIR` — where the item crops go (default `<input>_items/` next to the photo).
- `--no-classify` — stop after cropping. Useful when only the crops are wanted, or to
  avoid loading the classifier onto a GPU that ComfyUI is already filling.
- `--classify-cpu` — run the classifier on CPU for the same reason (it is a 97M-param
  model; the CPU pass costs a few seconds, not minutes).
- `--bundle DIR` — classifier weights to use (default `models/magic_eye/`).
- `--result-image PATH` — **skip generation** and run stages 3–4 on an existing grid.

### Testing without the GPU pipeline

Stages 3 and 4 don't need Qwen-Image-Edit-2511 — only its output. `result_v4.png` is
committed as exactly that, so the crop + classify half of the pipeline can be
developed and checked on any machine, including one with no ComfyUI at all:

```bash
python3 test_extract_outfit.py --result-image result_v4.png
```

```
Cropping items out of result_v4.png ...
  01_item_1.png: 461x461 from box (32, 145, 425, 542)
  02_item_2.png: 412x412 from box (476, 187, 831, 492)
  03_item_3.png: 399x399 from box (112, 666, 339, 1010)
  04_item_4.png: 328x328 from box (544, 710, 776, 993)

Classifying 4 crops with .../models/magic_eye ...
  models loaded on cuda (gender=5  category=4  sub_category=49  type=122  color=23 ...)

  01_item_1.png  (prompted as: item 1)
    type=shirts  category=clothing/shirts  gender=men
    color=[beige 91.1%, brown 67.7%]  neck=['spread collar']  sleeve=['short sleeve']  pattern=[]
    description: short sleeve shirts in beige and brown. spread collar. men's clothing.
  ... (3 more)
```

Crops get positional names (`item 1`…) on this path, because the prompt's item list
only exists when stage 1 actually ran.

The classifier is also usable on its own, against any folder of item images:

```bash
python3 wardrobe_classifier.py path/to/items -o wardrobe_index.json
```

### Auto-detected prompt (no hardcoded items)

If you don't pass a custom prompt, the script detects what the person is actually
wearing *before* calling ComfyUI, and builds the prompt from that — nothing is assumed
present:

| Detected | Effect |
|---|---|
| `hat` | `hat` included in the item list only if a hat is actually detected |
| `shoes` | `shoes` included only if footwear is actually detected |
| `dress` vs `top`+`bottom` | `dress` as a single item if the outfit is a genuine one-piece, `top/shirt` and `bottom (skirt/pants)` as two items if it isn't. Which one wins is decided by hue (below). Without this check the LoRA defaults to always splitting into top+skirt even for a real one-piece dress. |
| `bag` | `bag` included only if a bag/purse is actually detected |
| `outer` | `jacket/outerwear` included as its own item if a jacket/coat is detected over the top |

Detection (`outfit_items.py`) reuses the stack from `detect_clothing_by_face.py`
instead of the CLIP ViT-B/32 zero-shot classifier this used to run:

- **`yainage90/fashion-object-detection`** (Conditional DETR) — a real fashion object
  detector whose label set (`bag, bottom, dress, hat, outer, shoes, top`) matches what
  the prompt needs 1:1, rather than CLIP's whole-image caption similarity. That removed
  all the hand-tuned positive/negative caption pairs, including the bottom-25%-crop
  hack that footwear needed because whole-image CLIP confused sand/ocean/fabric with
  shoes — a detector localises shoes by itself.
- **multi-scale (800/1200px) + person-crop TTA** — candidates from the full frame and
  from a padded crop around the detected person are pooled and NMS'd together, which is
  what makes small items (hat, bag strap) survive on a phone-resolution photo.
- **hue-based dress-vs-top+bottom arbitration** — when a `dress` box overlaps the union
  of `top`+`bottom`, the mean hue of the top region and the bottom region decides it
  (clearly different colours ⇒ two garments), instead of a caption comparison.
- **SAM person isolation** — if more than one person is in the frame, the subject's
  pixel mask is cut out with `facebook/sam-vit-base` and everyone else is painted
  neutral grey before detection, so another person's clothes can't enter the item list.
  The subject is the largest person in frame, or the ArcFace
  (`insightface/buffalo_s`) match for `--selfie` when given.

  **The mask includes what the subject carries.** SAM prompted with a person box
  returns only the *person* — anything carried is a separate object to it, so the bag
  used to get painted out with the background: measured on
  `2026_02_18_16_30_05_IMG_0876.JPG`, 99.1% of the subject's crossbody bag (150,960 of
  152,391 px) was greyed, and `binary_fill_holes` could not recover it because the bag
  sits on the silhouette edge, so the gap connects to the background rather than being
  an enclosed hole (it recovered 0.17% of the mask). The detector then scored that bag
  0.2106 against 0.6758 on the original, surviving only via `BAG_OVERLAP_FLOOR`.
  So the garment detector now runs on the **original** image first, purely to locate
  the items; every box at least `ITEM_INSIDE_PERSON_FRAC` (0.8) inside the subject's
  person box is segmented too and unioned into the person's mask before the background
  is painted. Detection proper still runs on the isolated image afterwards. Measured
  effect: the bag on that photo goes 0.2097 → **0.8134**, and
  `2025_12_24_19_50_57_IMG_0485.JPG` gains a pair of trousers (0.6837) the old mask was
  clipping away. Cost is one extra detector pass, ~5.3s per photo on CPU.

  That 0.8 is deliberately at the top of the measured gap (own items 0.833–1.000,
  other people's 0.431 and below) rather than mid-gap: 0.65 was tried and regressed
  `2025_07_25_12_43_28_IMG_9653.JPG` from `{shoes, top, bottom, bag}` to
  `{shoes, outer}`. The one extra box it admitted grew the mask by 4,535 px (1.1%),
  which is enough to flip the winner-take-all `resolve_*` arbitration downstream.

  Known limitation: ownership is decided from box geometry, so an item another person
  holds *in front of* the subject counts as the subject's.
- The item list is **exactly what the detector localises** — nothing is assumed present,
  and nothing missing is invented either. The earlier CLIP path hardcoded `top` +
  `bottom` whenever the outfit wasn't a one-piece; a real detector is the better
  authority, so if a garment is being dropped, lower `--detect-threshold` instead
  (measured: a skirt at 0.40 reappears at `--detect-threshold 0.35`; this is also why the
  default floor is 0.4 rather than 0.5). The one remaining
  assumption is the empty-list fallback: if the detector finds *nothing at all*, the
  prompt falls back to `top` + `bottom` rather than being malformed.

Like `detect_clothing_by_face.py`, this runs on the **GPU when one is available**
(`OUTFIT_ITEMS_DEVICE=cpu` forces CPU back). Measured warm, per photo:

| | GPU (GTX 1660 SUPER) | CPU |
|---|---|---|
| single person | ~1.4s | ~7-9s |
| group shot (SAM isolation) | ~6.6s | ~27s |
| service startup (`warm_up`) | ~7s | ~36s |

Scores come out identical either way. The catch is that the service then holds VRAM for
as long as it runs — on a card where ComfyUI is already paging a ~30GB fp8 unet + text
encoder, start it with `OUTFIT_ITEMS_DEVICE=cpu` so the two don't fight over it. Setting
`MULTI_SCALE = False` in `outfit_items.py` is roughly 3x faster again, but costs real
detections (measured on a test photo: `top` went from 0.60 to not detected at all), so
it's off by default.

The resulting item list is placed into an explicit numbered grid (`row 1 left, row 1
right, row 2 left, ...`) so every item lands in its own cell with a wide white gap —
naming positions `top/middle/bottom` instead of `row N` caused occasional duplicated
items (the LoRA doesn't reliably tell "middle" from "bottom" apart in a 2-column
layout).

Detection talks to the `item_detector` supervisor service (models loaded once, kept
warm — see `item_detector_service.py`), so repeat runs pay only inference time instead
of reloading the detectors every run. Falls back to running the detector in-process
automatically if that service isn't running (SAM and insightface are loaded lazily,
only when a multi-person input or `--selfie` actually needs them).

### Stage 3: cropping the grid into items

`crop_items()` cuts the generated mockup back into one square PNG per garment. The
boxes come from **`ZhengPeng7/BiRefNet_lite`**, a class-agnostic foreground
segmentation model: the mockup is a plain background with items laid flat and never
overlapping, so "which pixels are an item" is the whole question here — "which *kind*
of item" is stage 4's job.

Two earlier approaches were measured against this one on the same 9 real generations,
scored on whether the number of boxes matched the number of objects actually rendered:

| Approach | Score |
|---|---|
| background-diff + connected components | collapsed separate items into one blob whenever their edges touched or shared a faint anti-aliased seam |
| `yainage90/fashion-object-detection` (stage 1's detector, reused here) | 5/9 |
| **BiRefNet_lite** | **9/9**, ~100 ms/image on GPU once warm |

The detector lost because it has to *classify* in order to detect, and its confidence
is badly calibrated on this synthetic flat-mockup domain — visually identical sandals
scored 0.96 on one generation and 0.04 on another — so real items kept being dropped
wherever the threshold went, and per-class overrides only traded one failure for
another. A segmentation model needs no score threshold at all, which removes that
whole class of tuning problem.

- **Blobs are merged when the gap between them is under 1% of the short side**,
  because one *item* is not always one blob: a pair of shoes is two, and a bag with
  its strap coiled beside it can be two. 1% sits between the within-item gap and the
  between-item one; 3% (what the connected-components version used) was measured
  merging a shirt into the shorts beside it, only 11 px away on one generation.
- **Reading order is row-major**, computed by grouping boxes into rows first and then
  sorting each row left-to-right. A plain sort by `y` interleaves the two columns,
  since items in one grid row are never aligned to the pixel.
- **Crops are square, padded with the sampled background** rather than cut into the
  garment. The classifier resizes to a fixed 224×224 without preserving aspect ratio,
  so feeding it a tall garment box directly would squash it horizontally into
  something its training images never contained.
- **Crops are named after the item the prompt asked for in that cell** — but only when
  the number found matches the number requested. If the model dropped or added a cell,
  the names fall back to positional (`item 1`, `item 2`, …) rather than confidently
  labelling a bag as footwear. The mismatch is printed.
- Crops from a previous run are deleted first, since stage 4 classifies every image in
  the folder and would otherwise report last run's leftovers as part of this outfit.

### Stage 4: classifying each item

`wardrobe_classifier.py` predicts the eight attributes the wardrobe index is built on
— gender, category, sub_category, type, colour, neck, sleeve, pattern — plus the two
768-d embeddings used for retrieval, and writes `wardrobe_index.json` into the crop
folder. Two trained models run per crop, both in `models/magic_eye/`:

| | |
|---|---|
| `phase2.pt` | SigLIP-base vision backbone + the 8 attribute heads. The classifier proper: image → visual embedding → attribute logits. |
| `phase3.pt` | Text-embedding generator. Consumes phase 2's visual embedding *and* its predicted attributes to produce the text embedding wardrobe search matches against. It is a head on top of phase 2, so the two are always loaded together. |

Multi-label heads (colour, neck, sleeve, pattern) use per-class thresholds tuned
upstream rather than a flat 0.5, which over-predicts common colours and drops rare
ones, and fall back to the top-1 class when nothing clears its threshold — so an item
never comes back with no colour at all.

On `result_v4.png`, all four items and every attribute:

| crop | type | category | colour |
|---|---|---|---|
| 01 | shirts | clothing/shirts | beige 91.1%, brown 67.7% — short sleeve, spread collar |
| 02 | shorts | clothing/shorts | black 99.9% |
| 03 | shoulder bags | bags/shoulder bags | black 99.2% |
| 04 | sandals | shoes/sandals | black 99.8% |

The models come from a separate training project (**MS_Model_Magic_Eye**), which they
are exported *out of* rather than read from — depending on it by absolute path meant
stage 4 only ran on the one machine that had that folder. Upstream's artefacts total
1.15 GB, so `scripts/export_magic_eye_bundle.py` reduces them to 203 MB in three ways,
none of which change a predicted label:

1. **Optimizer state dropped** — Adam keeps two extra fp32 tensors per parameter,
   about two thirds of the 709 MB phase 2 file, and is only needed to resume training.
2. **Weights stored fp16** — matmuls still run in fp32 (`load_state_dict` casts on
   load); this only halves what sits on disk. Measured against the fp32 originals on
   the `result_v4.png` crops: every label identical, colour confidences within 0.1
   percentage points (brown 67.6% → 67.7%).
3. **Taxonomy precomputed** — upstream rebuilds its label maps at every startup by
   streaming a 394 MB, 257k-item anchor corpus and reading scores out of an Excel
   workbook. The result is eight label→index maps, two hierarchy matrices and four
   score look-ups: 40 KB of JSON. This also removes `openpyxl` and ~30s from every run.

Re-run that script after a retrain; don't hand-edit the bundle.

### Running the detectors on their own

Useful for checking what the detector sees before spending a generation on it, or for
tuning `--detect-threshold`.

**Through the service** (fastest — models already warm):
```bash
curl -s -X POST http://127.0.0.1:18189/detect \
    -H 'Content-Type: application/json' \
    -d '{"image_path": "images/photo.jpg"}'

# with overrides
curl -s -X POST http://127.0.0.1:18189/detect \
    -H 'Content-Type: application/json' \
    -d '{"image_path": "group.jpg", "selfie_path": "selfie.jpg",
         "threshold": 0.35, "save_isolated_to": "isolated.png"}'
```

**In Python:**
```bash
python -c "import outfit_items, json; print(json.dumps(outfit_items.detect_worn_items('images/photo.jpg'), indent=2))"
```

**`detect_clothing_by_face.py`** — the standalone version, and the reference this
pipeline follows. Requires `--selfie` plus one of `--group-photo` / `--images-dir`:
```bash
# one photo
python detect_clothing_by_face.py --selfie selfie.jpg --group-photo group.jpg

# a whole folder, saving the SAM-isolated images so the mask can be checked by eye
python detect_clothing_by_face.py --selfie selfie.jpg --images-dir group-images \
    --save-isolated --isolated-dir isolated_output

# loosen both thresholds
python detect_clothing_by_face.py --selfie selfie.jpg --images-dir group-images \
    --threshold 0.35 --face-match-threshold 0.30
```
| Flag | Default | Meaning |
|---|---|---|
| `--selfie` | *(required)* | reference photo of the person to find |
| `--group-photo` / `--images-dir` | — | one image, or a folder (scanned recursively) |
| `--threshold` | 0.5 | garment-detector confidence floor |
| `--face-match-threshold` | 0.35 | minimum ArcFace cosine similarity to accept a face |
| `--save-isolated` | off | write the background-removed image out (only produced for photos with more than one person — with a single person SAM is skipped) |
| `--isolated-dir` | `isolated_output` | where those go |

**`detect_clothing_yolo.py`** — garment detection only, no face matching, over a folder;
writes `results_yolo.json` + `results_yolo.csv`:
```bash
python detect_clothing_yolo.py --images-dir images --threshold 0.5 --resolve-dress-conflict
python detect_clothing_yolo.py --images-dir images --single-scale --no-person-crop   # faster, less accurate
```

### Prompt-tuning notes (what worked / what didn't)

- **Keep it short and concrete.** This is a LoRA-conditioned diffusion model, not an
  LLM — long, multi-clause prompts with competing instructions measurably *reduce*
  reliability (more hallucinated extra items, worse separation) compared to a short,
  direct prompt.
- **Forceful/imperative wording backfires.** Words like `CRITICAL:` / `NEVER` /
  "isolated" made the layout *worse* (bag rendered draped over the garment like it's
  being worn) compared to plain descriptive phrasing ("the bag lies flat on its own
  with its strap coiled neatly beside it"). This model responds to *description*, not
  *commands*.
- **Never name an item category that isn't confirmed present.** Even inside a "don't
  invent X" clause, naming an absent category (e.g. "shoes") in the prompt text
  measurably increases the chance it gets drawn anyway. Detect first, then only
  mention categories that are actually there.
- **No prompt is 100% reliable across every seed.** Even with the current
  auto-detected + grid-positioned prompt, a given seed can occasionally still overlap
  two items or add an unrequested one. Use `--seed N` to retry rather than chasing
  a "perfect" prompt further — this is inherent stochasticity of the 4/8-step
  distilled sampler, not something prompt wording alone fixes.

## Benchmarks

### NVIDIA RTX 4090 (24GB VRAM), torch 2.11.0+cu130, warm cache

| Config | Time/image |
|---|---|
| Lightning 8-step, fp8 native | ~14-18s |
| **Lightning 4-step, fp8 native** | **~8-11s** ⭐ recommended |

Applied speedups vs. a naive setup (details below): **cu130 torch** (unlocks
comfy-kitchen's CUDA fp8 tensor-core kernels — cu128 silently falls back to a slower
eager path on Ada/Hopper+) and **skipping the negative-prompt text encode** at
cfg=1.0 (KSampler already ignores the uncond branch at cfg=1.0, so re-running the ~7B
VLM text encoder on an empty negative prompt was pure waste — replaced with
`ConditioningZeroOut` on the already-computed positive conditioning).

Investigated but **not used**: `torch.compile` (`TorchCompileModel`, both
`cudagraphs` and `inductor` backends) — `cudagraphs` crashes outright against
ComfyUI's `cudaMallocAsync` dynamic-VRAM allocator, and `inductor` recompiles (60s+)
on every new input image resolution, which real user photos always have, making it
strictly worse in practice. `--highvram` OOMs (unet 20.5GB + text encoder 9.4GB
exceeds 24GB without offload).

### NVIDIA L4 (23GB VRAM), warm cache

| Config | Time/image |
|---|---|
| Full 40-step, GGUF Q5_K_S | 212s |
| Lightning 4-step, GGUF Q5_K_S | 34s |
| Lightning 4-step, GGUF Q4_K_M | 32s |
| Lightning 8-step, fp8 native | 32s |
| Lightning 4-step, fp8 native | 17s |

### Notes from tuning this on an L4

- The fp8mixed checkpoint is faster than any GGUF quant level (Q5_K_S/Q4_K_M) because ComfyUI-GGUF
  dequantizes weights on the fly per-layer; the fp8 checkpoint uses native tensor-core fp8 matmul
  instead. Lowering the GGUF quant level (Q5→Q4) barely helps — the bottleneck is the dequant
  kernel itself, not file size or VRAM.
- `--use-sage-attention` produces **NaN / all-black output** with this checkpoint + LoRA chain
  (`RuntimeWarning: invalid value encountered in cast` in the ComfyUI log). Do not use it here.
  Reconfirmed on an RTX 4090 with torch cu130 + comfy-kitchen enabled — same failure, so it's a
  numerical incompatibility with this checkpoint+LoRA combination, not a driver/torch-version issue.
- `--fast fp8_matrix_mult cublas_ops [autotune]` is safe (no NaN) but gave no measurable speedup —
  the fp8mixed checkpoint's built-in `MixedPrecisionOps` already uses the fast path.
- Lowering input resolution (halving pixel count) gave no speedup either. On this GPU the fp8
  config's bottleneck is VRAM-constrained weight paging (unet 20.5GB + text encoder 7.9GB + VAE
  exceeds the L4's 22.5GB, so ComfyUI's dynamic-VRAM system streams weights every request), not
  raw compute. The GGUF config's bottleneck is different: it fully loads into VRAM (`full load:
  True`, no paging) but is compute-bound by the dequant kernels.
- Measured L4 memory bandwidth: ~231 GB/s (device-to-device copy benchmark) vs. the official
  300 GB/s spec. For reference, A100 40GB PCIe is ~1,555 GB/s and A100 80GB SXM is ~2,039 GB/s —
  5-7x more, which is the main reason a bigger/higher-bandwidth GPU would help both bottlenecks
  further (removes the VRAM paging entirely, and speeds up the GGUF dequant compute too).
