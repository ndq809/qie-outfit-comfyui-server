#!/bin/bash
utils=/opt/supervisor-scripts/utils
. "${utils}/logging.sh"
. "${utils}/environment.sh"

mkdir -p /workspace/data/miniodata

export MINIO_ROOT_USER="${MINIO_ROOT_USER}"
export MINIO_ROOT_PASSWORD="${MINIO_ROOT_PASSWORD}"

pty /opt/minio/minio server /workspace/data/miniodata \
    --address 127.0.0.1:9000 \
    --console-address 127.0.0.1:9001 2>&1
