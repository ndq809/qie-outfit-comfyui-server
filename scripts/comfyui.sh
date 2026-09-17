#!/bin/bash

utils=/opt/supervisor-scripts/utils
. "${utils}/logging.sh"
. "${utils}/environment.sh"
# No exit_portal guard: in the server deployment ComfyUI is D1's engine, reachable
# only by ai-server on 127.0.0.1. Only data-server is published externally
# (wardrobe-system-spec.md §2.3.4), so there is no /etc/portal.yaml entry to gate on.

source /venv/main/bin/activate
export HF_HOME="${HF_HOME:-${WORKSPACE:-/workspace}/.hf_home}"
cd "${WORKSPACE:-/workspace}/ComfyUI"
# COMFYUI_VRAM_ARGS (set in ${WORKSPACE}/.env) sizes this to the card. Default empty =
# ComfyUI's own autodetection, correct on a 24GB card.
#
# On a 48GB card use --reserve-vram N, NOT --highvram. Two other processes hold VRAM on
# this box - item_detector (~4GB: DETR + SAM + insightface) and the ai-server worker
# itself (~2GB: BiRefNet + SigLIP) - and --highvram forces a full load of the ~31GB of
# weights while ignoring the memory estimate, so the sampler then OOMs with nowhere to
# put its activations (measured: every item of a 10-photo job failed this way).
# ComfyUI's normal mode already keeps the weights resident while there is room for
# them; --reserve-vram is what tells it how much room the other two need.
pty python main.py --listen 127.0.0.1 --port 18188 --preview-method auto \
    ${COMFYUI_VRAM_ARGS} 2>&1
