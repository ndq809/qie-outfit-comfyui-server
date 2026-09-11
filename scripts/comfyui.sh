#!/bin/bash

utils=/opt/supervisor-scripts/utils
. "${utils}/logging.sh"
. "${utils}/environment.sh"

source /venv/main/bin/activate
export HF_HOME="${HF_HOME:-${WORKSPACE:-/workspace}/.hf_home}"
cd "${WORKSPACE:-/workspace}/ComfyUI"
# --reserve-vram keeps headroom on the card for the OTHER two GPU consumers on this
# single-GPU box: the D0b detector service (DETR + SAM + ArcFace) and ai-server's D3
# classifier. Without it ComfyUI expands to fill all 24GB and the detector OOMs on load.
# 4GB measured as enough for both with room to spare; raise it if either starts OOMing.
pty python main.py --listen 127.0.0.1 --port 18188 --preview-method auto \
    --reserve-vram "${COMFYUI_RESERVE_VRAM:-4}" 2>&1
