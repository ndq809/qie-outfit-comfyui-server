"""D1Engine: photo + prompt -> flat-lay grid, entirely in this process.

Replaces the ComfyUI round trip (upload PNG, queue, poll /history, download PNG) with the
same workflow run directly: FluxKontextImageScale -> TextEncodeQwenImageEditPlus (prompt
+ reference latent) -> sampler -> VAEDecode. Models stay resident between calls.
"""
import logging
import math
import time

import numpy as np
import torch
from PIL import Image

from . import attention as attn_mod
from . import comfy_lib
from .dit import QwenEditDiT
from .text_encoder import TextEncoder

log = logging.getLogger("d1_engine")

UNET = "qwen_image_edit_2511_fp8mixed.safetensors"
TEXT_ENCODER = "qwen_2.5_vl_7b_fp8_scaled.safetensors"
VAE = "qwen_image_vae.safetensors"
LORA_OUTFIT = "QIE-2511-Extract-Outfit-4200.safetensors"
LORA_LIGHTNING = {4: "Qwen-Image-Edit-2511-Lightning-4steps-V1.0-bf16.safetensors",
                  8: "Qwen-Image-Edit-2511-Lightning-8steps-V1.0-bf16.safetensors"}
SHIFT = 3.1


def kontext_size(width, height):
    """FluxKontextImageScale: the preferred Kontext resolution closest in aspect ratio."""
    from comfy_extras.nodes_flux import PREFERRED_KONTEXT_RESOLUTIONS
    ar = width / height
    _, w, h = min((abs(ar - w / h), w, h) for w, h in PREFERRED_KONTEXT_RESOLUTIONS)
    return w, h


def kontext_scale(image: Image.Image) -> Image.Image:
    """FluxKontextImageScale on a PIL image: centre-crop to the target aspect, Lanczos resize.
    ComfyUI does the same through comfy.utils.common_upscale("lanczos", "center"), which
    round-trips the pixels through float - this skips that, a <=1 LSB difference."""
    w0, h0 = image.size
    w, h = kontext_size(w0, h0)
    old_ar, new_ar = w0 / h0, w / h
    x = y = 0
    if old_ar > new_ar:
        x = round((w0 - w0 * (new_ar / old_ar)) / 2)
    elif old_ar < new_ar:
        y = round((h0 - h0 * (old_ar / new_ar)) / 2)
    if x or y:
        image = image.crop((x, y, w0 - x, h0 - y))
    return image.resize((w, h), Image.Resampling.LANCZOS)


class D1Engine:
    def __init__(self, steps=4, attention="sage", device="cuda", cache_dir=None):
        import comfy.latent_formats
        import comfy.sd
        import comfy.utils
        self.device = torch.device(device)
        self.steps = steps
        t = time.time()
        self.attention_fn, self.attention_name = attn_mod.get(attention)
        self.text_encoder = TextEncoder(comfy_lib.model_path("text_encoders", TEXT_ENCODER), device)
        self.vae = comfy.sd.VAE(sd=comfy.utils.load_torch_file(comfy_lib.model_path("vae", VAE)))
        self.latent_format = comfy.latent_formats.Wan21()
        self.loras = [(comfy_lib.model_path("loras", LORA_OUTFIT), 1.0),
                      (comfy_lib.model_path("loras", LORA_LIGHTNING[steps]), 1.0)]
        self.dit = QwenEditDiT(comfy_lib.model_path("diffusion_models", UNET), self.loras, steps=steps,
                               shift=SHIFT, device=device, attention=self.attention_fn,
                               cache_dir=cache_dir)
        log.info("D1 engine ready in %.1fs (%.1f GiB allocated)", time.time() - t,
                 torch.cuda.memory_allocated() / 2 ** 30)

    def settings(self, seed):
        """What this engine runs with, for the report page."""
        return {
            "models": {"diffusion model": f"{UNET} (LoRA gộp sẵn, W8A8 int8)",
                       "text encoder": f"{TEXT_ENCODER} (GEMM fp16, thường trú)",
                       "VAE": VAE,
                       "LoRA tách trang phục": f"{LORA_OUTFIT} · strength 1.0",
                       "LoRA tăng tốc": f"{LORA_LIGHTNING[self.steps]} · strength 1.0"},
            "params": {"steps": self.steps, "cfg": 1.0, "sampler": "euler", "scheduler": "simple",
                       "denoise": 1.0, "seed": seed, "shift (ModelSamplingAuraFlow)": SHIFT,
                       "CFGNorm strength": 1.0, "reference latents": "index_timestep_zero",
                       "attention": self.attention_name, "engine": "d1_engine (in-process)"},
        }

    def warm_up(self):
        """One throwaway generation: Triton compiles/tunes its kernels and SageAttention
        initialises on the first call, which would otherwise land on a real photo."""
        t = time.time()
        self.generate(Image.new("RGB", (880, 1184), (200, 200, 200)),
                      "Arrange in a single centered item (no grid): upper-body garment in symmetric flat "
                      "lay in row 1 center. Plain white background.", seed=0)
        log.info("D1 engine warm-up %.1fs", time.time() - t)

    @torch.no_grad()
    def generate(self, image: Image.Image, prompt: str, seed: int = 42, timing: dict | None = None,
                 prescaled: bool = False) -> Image.Image:
        """image: the photo D1 should draw from (RGB). prescaled=True when the caller already
        ran kontext_scale() on it - the worker does that on its CPU-side thread."""
        import comfy.sample
        import comfy.utils
        # stream-local: the worker runs D0b/D3 on other streams, a device-wide sync would wait for them
        sync = torch.cuda.current_stream().synchronize
        t0 = time.perf_counter()
        scaled = image if prescaled else kontext_scale(image.convert("RGB"))
        img = torch.from_numpy(np.array(scaled)).to(self.device).float().div_(255.0)[None]   # [1,H,W,3]
        t1 = time.perf_counter()
        context = self.text_encoder.encode(img, prompt)
        sync(); t2 = time.perf_counter()
        # reference latent: TextEncodeQwenImageEditPlus scales to ~1024x1024 px, multiples of 8
        samples = img.movedim(-1, 1)
        sb = math.sqrt(1024 * 1024 / (samples.shape[3] * samples.shape[2]))
        rw, rh = round(samples.shape[3] * sb / 8.0) * 8, round(samples.shape[2] * sb / 8.0) * 8
        ref_img = comfy.utils.common_upscale(samples, rw, rh, "area", "disabled").movedim(1, -1)
        ref_latent = self.vae.encode(ref_img[:, :, :, :3])
        sync(); t3 = time.perf_counter()
        H, W = img.shape[1], img.shape[2]
        noise = comfy.sample.prepare_noise(torch.zeros((1, 16, 1, H // 8, W // 8)), seed)
        latent = self.dit.sample(noise, context, ref_latent, self.latent_format)
        sync(); t4 = time.perf_counter()
        decoded = self.vae.decode(latent.float())
        decoded = decoded.reshape(-1, decoded.shape[-3], decoded.shape[-2], decoded.shape[-1])[0]
        grid = Image.fromarray((decoded.float().clamp(0, 1) * 255).round().to(torch.uint8).cpu().numpy())
        t5 = time.perf_counter()
        if timing is not None:
            timing.update({"tiền xử lý ảnh": round(t1 - t0, 3), "text encoder": round(t2 - t1, 3),
                           "VAE encode ảnh tham chiếu": round(t3 - t2, 3),
                           f"sampling {self.steps} bước": round(t4 - t3, 3), "VAE decode": round(t5 - t4, 3)})
        return grid
