#!/bin/bash
utils=/opt/supervisor-scripts/utils
. "${utils}/logging.sh"
. "${utils}/environment.sh"

source /venv/main/bin/activate
cd "${WORKSPACE:-/workspace}/qie-outfit-comfyui-server"
pty python -m uvicorn server.data_server.app:app \
    --host 127.0.0.1 --port "${DATA_SERVER_PORT:-18080}" 2>&1
