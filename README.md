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

## Service setup (Vast.ai / supervisor)

- `scripts/comfyui.sh` — supervisor wrapper script (`/opt/supervisor-scripts/comfyui.sh`), runs
  `python main.py --listen 127.0.0.1 --port 18188`.
- `scripts/comfyui.conf` — supervisor program config (`/etc/supervisor/conf.d/comfyui.conf`).
- Expose via the instance's Caddy auth edge by adding a `ComfyUI` entry to `/etc/portal.yaml`
  (`external_port: 10100`, `internal_port: 18188`), then `supervisorctl reread && supervisorctl update`.

## Usage

```bash
source /venv/main/bin/activate
python3 test_extract_outfit.py --lightning4 --fp8 <input_image> ["<custom prompt>"]
```

Flags:
- `--fp8` — use the native fp8mixed checkpoint (recommended: fast, numerically stable).
- `--lightning4` / `--lightning8` — chain the Lightning distilled-speed LoRA (4 or 8 steps, cfg 1.0).
- No flag — full 40-step generation (cfg 4.0), GGUF unet, slowest but no distillation LoRA involved.

Default prompt: `"Extract the clothing and create a flat mockup."` (the LoRA's trigger phrase).

## Benchmarks (NVIDIA L4, 23GB VRAM, warm cache)

| Config | Time/image |
|---|---|
| Full 40-step, GGUF Q5_K_S | 212s |
| Lightning 4-step, GGUF Q5_K_S | 34s |
| Lightning 4-step, GGUF Q4_K_M | 32s |
| Lightning 8-step, fp8 native | 32s |
| **Lightning 4-step, fp8 native** | **17s** ⭐ recommended |

### Notes from tuning this on an L4

- The fp8mixed checkpoint is faster than any GGUF quant level (Q5_K_S/Q4_K_M) because ComfyUI-GGUF
  dequantizes weights on the fly per-layer; the fp8 checkpoint uses native tensor-core fp8 matmul
  instead. Lowering the GGUF quant level (Q5→Q4) barely helps — the bottleneck is the dequant
  kernel itself, not file size or VRAM.
- `--use-sage-attention` produces **NaN / all-black output** with this checkpoint + LoRA chain
  (`RuntimeWarning: invalid value encountered in cast` in the ComfyUI log). Do not use it here.
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
