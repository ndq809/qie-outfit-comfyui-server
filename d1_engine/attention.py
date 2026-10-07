"""Joint-attention backends for the D1 transformer. All take and return NHD [B, L, H, D].

SageAttention2's int8-QK / fp8-PV kernel (`sageattn` on sm89) is the default: ~2.4x
PyTorch SDPA on an RTX 4090 at this sequence length, and over the eval set its outputs sit
inside the run-to-run spread of the bf16 model. Its fp16-PV variants are NOT usable here:
Qwen-Image's value activations overflow fp16 and every output comes back NaN (the same
failure the README records for ComfyUI's --use-sage-attention). SDPA is the fallback when
the sageattention package is missing."""
import logging

import torch.nn.functional as F

log = logging.getLogger("d1_engine")


def sdpa(q, k, v):
    return F.scaled_dot_product_attention(q.transpose(1, 2), k.transpose(1, 2), v.transpose(1, 2)).transpose(1, 2)


def get(name="sage"):
    if name == "sdpa":
        return sdpa, "sdpa"
    try:
        import sageattention as sa
    except ImportError:
        log.warning("sageattention not installed - D1 falls back to PyTorch SDPA (~0.7s slower per image)")
        return sdpa, "sdpa"
    return (lambda q, k, v: sa.sageattn(q, k, v, tensor_layout="NHD", is_causal=False)), "sage (int8 QK, fp8 PV)"
