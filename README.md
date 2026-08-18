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

`test_extract_outfit.py`'s auto-prompt path also needs `transformers` + a CPU build of
`torch` (already pulled in by ComfyUI's own requirements) to run the CLIP classifier —
see [Auto-detected prompt](#auto-detected-prompt-no-hardcoded-items) below.

## Service setup (Vast.ai / supervisor)

Two supervisor services:

- **ComfyUI** — `scripts/comfyui.sh` (`/opt/supervisor-scripts/comfyui.sh`), runs
  `python main.py --listen 127.0.0.1 --port 18188`. Config: `scripts/comfyui.conf`.
  Expose via the instance's Caddy auth edge by adding a `ComfyUI` entry to
  `/etc/portal.yaml` (`external_port: 10100`, `internal_port: 18188`), then
  `supervisorctl reread && supervisorctl update`.
- **CLIP item classifier** — `scripts/clip_classifier.sh`
  (`/opt/supervisor-scripts/clip_classifier.sh`), runs `clip_classifier_service.py` on
  `127.0.0.1:18189`. CPU-only, no portal entry needed (internal use only by
  `test_extract_outfit.py`). Config: `scripts/clip_classifier.conf`.

Install both the same way:
```bash
cp scripts/comfyui.sh scripts/clip_classifier.sh /opt/supervisor-scripts/
chmod +x /opt/supervisor-scripts/comfyui.sh /opt/supervisor-scripts/clip_classifier.sh
cp scripts/comfyui.conf scripts/clip_classifier.conf /etc/supervisor/conf.d/
supervisorctl reread && supervisorctl update
```

## Usage

```bash
source /venv/main/bin/activate
python3 test_extract_outfit.py --lightning4 --fp8 [--seed N] <input_image> ["<custom prompt>"]
```

Flags:
- `--fp8` — use the native fp8mixed checkpoint (recommended: fast, numerically stable).
- `--lightning4` / `--lightning8` — chain the Lightning distilled-speed LoRA (4 or 8 steps, cfg 1.0).
- No flag — full 40-step generation (cfg 4.0), GGUF unet, slowest but no distillation LoRA involved.
- `--seed N` — sampler seed (default 42). The LoRA doesn't follow layout/presence
  instructions with 100% reliability on every seed (see below) — re-run with a
  different seed if one result comes out with an overlap or a missed item.

### Auto-detected prompt (no hardcoded items)

If you don't pass a custom prompt, the script runs a lightweight CLIP (ViT-B/32,
~350MB, CPU-only) zero-shot classifier against the input photo *before* calling
ComfyUI, and builds the prompt from what it actually finds — nothing is assumed
present:

| Detected | Effect |
|---|---|
| `headwear` | `hat` included in the item list only if a hat is actually visible |
| `footwear` | `shoes` included only if feet/shoes are actually visible (crops to the bottom 25% of the frame — whole-image classification is unreliable here, confused by background) |
| `one_piece` | `dress` (single item) if the outfit is a genuine one-piece; otherwise `top/shirt` + `bottom (skirt/pants)` as two items. Without this check the LoRA defaults to always splitting into top+skirt even for a real one-piece dress. |
| `bag` | `bag` included only if a bag/purse is actually visible |

The resulting item list is placed into an explicit numbered grid (`row 1 left, row 1
right, row 2 left, ...`) so every item lands in its own cell with a wide white gap —
naming positions `top/middle/bottom` instead of `row N` caused occasional duplicated
items (the LoRA doesn't reliably tell "middle" from "bottom" apart in a 2-column
layout).

Classification talks to the `clip_classifier` supervisor service (model loaded once,
kept warm — see `clip_classifier_service.py`), so repeat runs cost ~0.5-1s instead of
the ~8s process-startup + model-load penalty of loading CLIP fresh each time. Falls
back to loading CLIP in-process automatically if that service isn't running.

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
