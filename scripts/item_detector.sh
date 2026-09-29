#!/bin/bash

utils=/opt/supervisor-scripts/utils
. "${utils}/logging.sh"
. "${utils}/environment.sh"

source /venv/main/bin/activate
export HF_HOME="${HF_HOME:-${WORKSPACE:-/workspace}/.hf_home}"
cd "${WORKSPACE:-/workspace}/qie-outfit-comfyui-server"
# Below sshd and the API in CPU and disk priority: on other instances a job burst
# starved the SSH session until it dropped. Thread caps are in ${WORKSPACE}/.env.
pty nice -n 10 ionice -c2 -n7 python item_detector_service.py 2>&1
