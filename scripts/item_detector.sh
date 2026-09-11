#!/bin/bash

utils=/opt/supervisor-scripts/utils
. "${utils}/logging.sh"
. "${utils}/environment.sh"

source /venv/main/bin/activate
export HF_HOME="${HF_HOME:-${WORKSPACE:-/workspace}/.hf_home}"

# ArcFace (insightface) runs through onnxruntime, not torch, and asks for GPU via
# ctx_id=0. onnxruntime-gpu finds CUDA only if cuBLAS/cuDNN are on the loader path --
# here they come from the pip nvidia-* packages torch pulls in, which are NOT on the
# default path. Without this the CUDA provider fails to load and insightface silently
# falls back to CPU with no error, so the "GPU" detector still runs face matching on CPU.
# Pinned to onnxruntime-gpu 1.22 (CUDA 12.x): 1.30 wants CUDA 13 and this box is cu128.
NV_LIB=/venv/main/lib/python3.12/site-packages/nvidia
export LD_LIBRARY_PATH="${NV_LIB}/cublas/lib:${NV_LIB}/cudnn/lib:${NV_LIB}/cuda_runtime/lib:${NV_LIB}/cufft/lib:${NV_LIB}/curand/lib:${LD_LIBRARY_PATH}"

cd "${WORKSPACE:-/workspace}/qie-outfit-comfyui-server"
pty python item_detector_service.py 2>&1
