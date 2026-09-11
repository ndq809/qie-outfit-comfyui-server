#!/bin/bash
utils=/opt/supervisor-scripts/utils
. "${utils}/logging.sh"
. "${utils}/environment.sh"

mkdir -p /workspace/data/redis

# exec, not `pty`: see the comment in postgres.sh — the pty wrapper detaches the daemon
# from supervisor's process group, so it survives a stop and then blocks its own restart
# with "Address already in use". Redis writes its log unbuffered, so no pty is needed.
exec redis-server \
    --bind 127.0.0.1 \
    --port 6379 \
    --daemonize no \
    --dir /workspace/data/redis \
    --logfile "" \
    --save 60 1 2>&1
