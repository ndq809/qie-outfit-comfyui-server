#!/bin/bash

utils=/opt/supervisor-scripts/utils
. "${utils}/logging.sh"
. "${utils}/environment.sh"

source /venv/main/bin/activate
export HF_HOME="${HF_HOME:-${WORKSPACE:-/workspace}/.hf_home}"
cd "${WORKSPACE:-/workspace}/qie-outfit-comfyui-server"
pty python clip_classifier_service.py 2>&1
