"""data-server — the only publicly-callable component (wardrobe-system-spec.md
§3.1). Owns postgres, issues presigned upload URLs, creates jobs and pushes
job-queue tickets, and (via result_consumer, started as a background thread)
consumes ai-server's results into the database.
"""
import logging
import threading
from pathlib import Path
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


# Object key of the test-deployment fixed reference face, or None when not configured.
# Populated once at startup so create_job doesn't re-check the filesystem per request.
_fixed_face_ref_key: Optional[str] = None


@app.on_event("startup")
def _startup():
    storage.ensure_buckets()
    _load_fixed_face_ref()
    t = threading.Thread(target=run_result_consumer, daemon=True)
    t.start()
    log.info("result_consumer thread started")


def _load_fixed_face_ref():
    """TEST DEPLOYMENT ONLY (wardrobe-system-spec.md §2.3.7). Uploads the configured
    reference selfie into object-storage once at boot and remembers its key, so every
    job gets a working D0b face reference without the client ever calling
    /v1/face-reference. Uploaded rather than read from disk at job time because
    ai-server fetches it from object-storage and may not share this filesystem.

    A missing or unreadable file is logged and ignored, not fatal: the system still
    works without it, D0b just falls back to "largest person in frame"."""
    global _fixed_face_ref_key
    configured = get_settings().test_fixed_face_ref_image
    if not configured:
        return
    path = Path(configured)
    if not path.is_file():
        log.warning("TEST_FIXED_FACE_REF_IMAGE=%s does not exist — jobs will fall back "
                    "to largest-person-in-frame for group photos", configured)
        return
    try:
        storage.upload_file(get_settings().minio_raw_bucket, storage.FIXED_FACE_REF_KEY,
                            path, content_type="image/jpeg")
    except Exception:
        log.exception("could not upload fixed face reference %s", configured)
        return
    _fixed_face_ref_key = storage.FIXED_FACE_REF_KEY
    log.info("fixed D0b face reference active: %s -> %s", configured, _fixed_face_ref_key)


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


class FaceRefPresignRequest(BaseModel):
    contentType: str


class FaceRefCommitRequest(BaseModel):
    objectKey: str


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


@app.post("/v1/face-reference/presign")
def face_reference_presign(req: FaceRefPresignRequest, identity=Depends(current_account)):
    """D0b needs a reference face to tell the account owner apart from everyone
    else in a group photo (wardrobe-system-spec.md §2.1). Same
    upload-straight-to-object-storage shape as /v1/uploads/presign so Mobile
    reuses the code path it already has."""
    settings = get_settings()
    key = storage.face_ref_object_key(identity["account_id"], req.contentType)
    url = storage.presign_put(settings.minio_raw_bucket, key, req.contentType,
                              settings.presign_expires_seconds)
    return {
        "objectKey": key,
        "uploadUrl": url,
        "expiresAt": _expires_at(settings.presign_expires_seconds),
    }


@app.put("/v1/face-reference")
def face_reference_commit(req: FaceRefCommitRequest, identity=Depends(current_account)):
    """Called after the PUT to uploadUrl succeeds. Verifies the object is really
    there before recording it — otherwise a failed upload would leave every later
    job pointing at a key ai-server can't fetch."""
    settings = get_settings()
    expected_prefix = f"face/{identity['account_id']}/"
    if not req.objectKey.startswith(expected_prefix):
        raise HTTPException(status_code=403, detail="objectKey does not belong to this account")
    try:
        storage.s3_client().head_object(Bucket=settings.minio_raw_bucket, Key=req.objectKey)
    except Exception:
        raise HTTPException(status_code=400, detail="no object uploaded at that objectKey")
    db.set_account_face_ref(identity["account_id"], req.objectKey)
    return {"objectKey": req.objectKey, "status": "registered"}


@app.get("/v1/face-reference")
def face_reference_get(identity=Depends(current_account)):
    """`source` says which reference D0b will actually use: "account" for one this
    account registered, "test-fixture" for the fixed image the test deployment
    supplies (§2.3.7), null when there is none and D0b falls back to the largest
    person in frame."""
    key = db.get_account_face_ref(identity["account_id"])
    if key:
        return {"registered": True, "objectKey": key, "source": "account"}
    if _fixed_face_ref_key:
        return {"registered": True, "objectKey": _fixed_face_ref_key, "source": "test-fixture"}
    return {"registered": False, "objectKey": None, "source": None}


@app.post("/v1/jobs")
def create_job(req: CreateJobRequest, identity=Depends(current_account)):
    rows = db.get_batch_items(req.batchId, req.uploadedItems)
    if not rows:
        raise HTTPException(status_code=400, detail="no matching uploaded items for this batch")
    items = [{"local_id": r["local_id"], "object_key": r["object_key"]} for r in rows]
    job = db.create_job(identity["account_id"], identity["user_id"], req.batchId, items)
    db.mark_job_processing_if_pending(job["jobId"])
    # An account that registered its own face wins; the fixed test fixture is only a
    # fallback, so production behaviour is unchanged when it isn't configured.
    face_ref_key = db.get_account_face_ref(identity["account_id"]) or _fixed_face_ref_key
    for it in items:
        queue.push_job(job["jobId"], it["local_id"], it["object_key"], face_ref_key=face_ref_key)
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
