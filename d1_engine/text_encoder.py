"""D1's prompt encoder: Qwen2.5-VL-7B through ComfyUI's own model code, kept resident.

ComfyUI runs it with fp32 activations over fp8-scaled weights that it dequantizes to fp32
on every call (sd1_clip passes dtype=torch.float32), plus dynamic-VRAM staging: ~0.41s per
prompt for ~5 TFLOP of work. Here each Linear keeps a dequantized fp16 copy of its weight on
the GPU and runs its GEMM in fp16 (fp32 accumulation); everything between GEMMs - residual
stream, RMSNorm, RoPE, softmax - stays fp32. ~0.1s. bf16 GEMMs were tried and rejected: the
residual stream reaches ~170 and bf16's 8-bit mantissa moved the conditioning by ~10%.
"""
import math
import types

import torch
import torch.nn.functional as F

from . import comfy_lib  # noqa: F401

# TextEncodeQwenImageEditPlus' template, verbatim.
LLAMA_TEMPLATE = ("<|im_start|>system\nDescribe the key features of the input image (color, shape, size, texture, "
                  "objects, background), then explain how the user's text instruction should alter or modify the "
                  "image. Generate a new image that meets the user's requirements while maintaining consistency "
                  "with the original input where appropriate.<|im_end|>\n<|im_start|>user\n{}<|im_end|>\n"
                  "<|im_start|>assistant\n")


def _fp32(t, device):
    """ComfyUI weight -> fp32 on `device`. fp8 QuantizedTensors go straight to fp32
    (qdata * scale), as ComfyUI does for its fp32 compute; via their bf16 orig_dtype every
    weight would round. Moved first, converted on the GPU: 7.6B params on the CPU took ~15s."""
    if hasattr(t, "_qdata"):
        return t._qdata.to(device).to(torch.float32) * t._params.scale.to(device, torch.float32)
    return t.detach().to(device).to(torch.float32)


def _mixed_linear(self, x, *args, **kwargs):
    return F.linear(x.to(self._gd), self._fw, self._fb).to(x.dtype)


class TextEncoder:
    def __init__(self, path, device="cuda", gemm_dtype=torch.float16):
        import comfy.sd
        self.device = torch.device(device)
        self.clip = comfy.sd.load_clip(ckpt_paths=[path], clip_type=comfy.sd.CLIPType.QWEN_IMAGE)
        tem = self.clip.cond_stage_model
        for _, mod in tem.named_modules():
            if type(mod).__name__ == "Linear" and getattr(mod, "weight", None) is not None:
                mod._fw = _fp32(mod.weight, self.device).to(gemm_dtype).contiguous()
                mod._fb = None if mod.bias is None else _fp32(mod.bias, self.device).to(gemm_dtype)
                mod._gd = gemm_dtype
                mod.forward = types.MethodType(_mixed_linear, mod)
                mod.weight = None
        for _, mod in tem.named_modules():
            for pn, p in list(mod.named_parameters(recurse=False)):
                if p is not None:
                    t = _fp32(p, self.device).to(p.dtype) if hasattr(p, "_qdata") else p.detach()
                    setattr(mod, pn, torch.nn.Parameter(t.to(self.device), requires_grad=False))
            for bn, buf in list(mod.named_buffers(recurse=False)):
                if buf is not None:
                    mod._buffers[bn] = buf.to(self.device)
        torch.cuda.empty_cache()

    @torch.no_grad()
    def encode(self, image_bhwc, prompt):
        """TextEncodeQwenImageEditPlus for one image, without its reference-latent half.
        image_bhwc: float [1,H,W,3] in 0..1. Returns the context [1, L, 3584] (fp32)."""
        import comfy.utils
        samples = image_bhwc.movedim(-1, 1)
        scale_by = math.sqrt(384 * 384 / (samples.shape[3] * samples.shape[2]))
        s = comfy.utils.common_upscale(samples, round(samples.shape[3] * scale_by),
                                       round(samples.shape[2] * scale_by), "area", "disabled").movedim(1, -1)
        tokens = self.clip.tokenize("Picture 1: <|vision_start|><|image_pad|><|vision_end|>" + prompt,
                                    images=[s], llama_template=LLAMA_TEMPLATE)
        return self.clip.cond_stage_model.encode_token_weights(tokens)[0]
