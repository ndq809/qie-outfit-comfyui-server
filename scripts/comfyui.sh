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
pty python main.py --listen 127.0.0.1 --port 18188 --preview-method auto 2>&1
