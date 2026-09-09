#!/bin/bash
utils=/opt/supervisor-scripts/utils
. "${utils}/logging.sh"
. "${utils}/environment.sh"

source /venv/main/bin/activate
export HF_HOME="${HF_HOME:-${WORKSPACE:-/workspace}/.hf_home}"
cd "${WORKSPACE:-/workspace}/qie-outfit-comfyui-server"
pty python -m uvicorn server.ai_server.app:app \
    --host 127.0.0.1 --port "${AI_SERVER_HEALTH_PORT:-18090}" 2>&1
