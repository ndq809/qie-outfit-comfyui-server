"""In-process D1 (outfit extraction) engine: Qwen-Image-Edit-2511 + Extract-Outfit LoRA +
Lightning LoRA, int8 W8A8 Triton kernels, SageAttention, resident text encoder and VAE.
See engine.D1Engine and README "D1 engine"."""
from .engine import D1Engine, kontext_scale  # noqa: F401
