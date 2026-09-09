#!/bin/bash
utils=/opt/supervisor-scripts/utils
. "${utils}/logging.sh"
. "${utils}/environment.sh"

mkdir -p /workspace/data/redis

pty redis-server \
    --bind 127.0.0.1 \
    --port 6379 \
    --daemonize no \
    --dir /workspace/data/redis \
    --logfile "" \
    --save 60 1 2>&1
