"""ai-server — internal only, no business API (wardrobe-system-spec.md §3.2).
Just GET /health plus the worker thread(s) started at startup."""
import logging
import threading

from fastapi import FastAPI

from server.ai_server.worker import run_worker_loop
from server.common import queue
from server.common.config import get_settings

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ai-server")

app = FastAPI(title="Wardrobe ai-server (test, internal)")

_workers: list[threading.Thread] = []


@app.on_event("startup")
def _startup():
    t = threading.Thread(target=run_worker_loop, daemon=True)
    t.start()
    _workers.append(t)
    log.info("worker thread started")


@app.get("/health")
def health():
    deps = {
        "objectStorage": "ok" if _minio_ok() else "error",
        "queue": "ok" if queue.ping() else "error",
    }
    status = "ok" if all(v == "ok" for v in deps.values()) else "error"
    return {
        "status": status,
        "activeWorkers": sum(1 for t in _workers if t.is_alive()),
        "dependencies": deps,
    }


def _minio_ok() -> bool:
    from server.common.storage import s3_client
    try:
        s3_client().list_buckets()
        return True
    except Exception:
        return False
