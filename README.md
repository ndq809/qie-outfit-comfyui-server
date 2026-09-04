# QIE-2511-Extract-Outfit — ComfyUI server

ComfyUI server setup to run [prithivMLmods/QIE-2511-Extract-Outfit](https://huggingface.co/prithivMLmods/QIE-2511-Extract-Outfit),
a LoRA on top of `Qwen-Image-Edit-2511` that extracts garments from a photo into a
clean flat-lay mockup.

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
`torchvision` and `scipy` (mostly pulled in by ComfyUI's own requirements) for the
worn-item detector, plus `insightface` + `onnxruntime` for face matching:

```bash
pip install transformers torchvision scipy
pip install insightface onnxruntime      # only needed for --selfie
```

The detector models (`yainage90/fashion-object-detection`, `facebook/sam-vit-base`,
`insightface/buffalo_l`) download themselves on first use into `HF_HOME` /
`~/.insightface` — nothing to place by hand.

## Files

| File | What it is |
|---|---|
| `test_extract_outfit.py` | Terminal client: detects the worn items, builds the prompt, runs the ComfyUI workflow. |
| `outfit_items.py` | The worn-item detector used by the auto-prompt path. Wraps the two scripts below. |
| `item_detector_service.py` | Keeps those models warm behind `127.0.0.1:18189` so repeat runs skip the load cost. |
| `detect_clothing_yolo.py` | Standalone garment detector (multi-scale + person-crop TTA, hue dress arbitration). |
| `detect_clothing_by_face.py` | Standalone: picks one person out of a group photo by face, isolates them with SAM, then detects their clothes. |

`outfit_items.py` imports the last two, so all five files must sit in the same folder.

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
    [--detect-threshold F] [--face-threshold F] <input_image> ["<custom prompt>"]
```

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
  The subject is the largest person in frame, or the ArcFace (`insightface/buffalo_l`)
  match for `--selfie` when given.

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
