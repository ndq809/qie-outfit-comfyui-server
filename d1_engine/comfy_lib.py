"""ComfyUI as a plain library: its model code (Qwen-Image transformer helpers, Qwen2.5-VL
text encoder, Wan VAE, tokenizers, sigma schedules) without its server, prompt queue or
dynamic-VRAM manager. COMFYUI_DIR points at the checkout (default /workspace/ComfyUI)."""
import os
import sys

COMFYUI_DIR = os.environ.get("COMFYUI_DIR", "/workspace/ComfyUI")
MODELS_DIR = os.path.join(COMFYUI_DIR, "models")
if COMFYUI_DIR not in sys.path:
    sys.path.insert(0, COMFYUI_DIR)


def model_path(kind, name):
    return os.path.join(MODELS_DIR, kind, name)
