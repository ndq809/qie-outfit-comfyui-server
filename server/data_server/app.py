"""data-server — the only publicly-callable component (wardrobe-system-spec.md
§3.1). Owns postgres, issues presigned upload URLs, creates jobs and pushes
job-queue tickets, and (via result_consumer, started as a background thread)
consumes ai-server's results into the database.
"""
import logging
import threading
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel

from server.common import queue, storage
from server.common.config import get_settings
from server.data_server import db
from server.data_server.result_consumer import run_result_consumer

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("data-server")

app = FastAPI(title="Wardrobe data-server (test)")


@app.on_event("startup")
def _startup():
    storage.ensure_buckets()
    t = threading.Thread(target=run_result_consumer, daemon=True)
    t.start()
    log.info("result_consumer thread started")


# --- auth -------------------------------------------------------------------

def current_account(authorization: Optional[str] = Header(None)):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    identity = db.lookup_token(token)
    if not identity:
        raise HTTPException(status_code=401, detail="invalid token")
    account_id, user_id = identity
    return {"account_id": account_id, "user_id": user_id}


# --- schemas ------------------------------------------------------------------

class PresignItem(BaseModel):
    localId: str
    contentType: str
    checksum: Optional[str] = None


class PresignRequest(BaseModel):
    items: list[PresignItem]


class CreateJobRequest(BaseModel):
    batchId: str
    uploadedItems: list[str]


# --- routes -------------------------------------------------------------------

@app.post("/v1/uploads/presign")
def presign(req: PresignRequest, identity=Depends(current_account)):
    settings = get_settings()
    batch_id = db.create_upload_batch(identity["account_id"], identity["user_id"])
    out_items = []
    for item in req.items:
        key = storage.raw_object_key(identity["account_id"], batch_id, item.localId, item.contentType)
        db.add_batch_item(batch_id, item.localId, key, item.contentType, item.checksum)
        url = storage.presign_put(settings.minio_raw_bucket, key, item.contentType, settings.presign_expires_seconds)
        out_items.append({
            "localId": item.localId,
            "objectKey": key,
            "uploadUrl": url,
            "expiresAt": _expires_at(settings.presign_expires_seconds),
        })
    return {"batchId": batch_id, "items": out_items}


@app.post("/v1/jobs")
def create_job(req: CreateJobRequest, identity=Depends(current_account)):
    rows = db.get_batch_items(req.batchId, req.uploadedItems)
    if not rows:
        raise HTTPException(status_code=400, detail="no matching uploaded items for this batch")
    items = [{"local_id": r["local_id"], "object_key": r["object_key"]} for r in rows]
    job = db.create_job(identity["account_id"], identity["user_id"], req.batchId, items)
    db.mark_job_processing_if_pending(job["jobId"])
    for it in items:
        queue.push_job(job["jobId"], it["local_id"], it["object_key"])
    return job


@app.get("/v1/jobs/{job_id}")
def get_job(job_id: str, identity=Depends(current_account)):
    job = db.get_job(identity["account_id"], job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@app.post("/v1/jobs/{job_id}/cancel")
def cancel_job(job_id: str, identity=Depends(current_account)):
    status = db.cancel_job(identity["account_id"], job_id)
    if not status:
        raise HTTPException(status_code=404, detail="job not found")
    if status == "cancelling":
        queue.mark_cancelled(job_id)
    return {"status": status}


@app.get("/v1/wardrobe/items")
def wardrobe_items(jobId: Optional[str] = Query(None), cursor: Optional[str] = Query(None),
                    limit: int = Query(50, ge=1, le=200), identity=Depends(current_account)):
    settings = get_settings()
    rows, next_cursor = db.list_wardrobe_items(identity["account_id"], jobId, cursor, limit)
    items = [{
        "imageUrl": storage.presign_get(settings.minio_items_bucket, r["object_key"], settings.read_url_expires_seconds),
        "tags": r["tags"],
        "jobId": str(r["job_id"]),
    } for r in rows]
    out = {"items": items}
    if next_cursor:
        out["nextCursor"] = next_cursor
    return out


@app.get("/v1/health")
def health():
    deps = {
        "postgres": "ok" if db.ping() else "error",
        "objectStorage": "ok" if _minio_ok() else "error",
        "queue": "ok" if queue.ping() else "error",
    }
    status = "ok" if all(v == "ok" for v in deps.values()) else "error"
    return {"status": status, "dependencies": deps}


def _minio_ok() -> bool:
    try:
        storage.s3_client().list_buckets()
        return True
    except Exception:
        return False


def _expires_at(seconds: int) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
