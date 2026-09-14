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
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from server.common import queue, storage
from server.common.config import get_settings
from server.data_server import db
from server.data_server.result_consumer import run_result_consumer

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("data-server")

app = FastAPI(title="Wardrobe data-server (test)")


def _mount_test_report():
    """Serve WARDROBE_REPORT_DIR at /report (test only).

    It has to be served from here rather than read off disk through some other service:
    the page is HTML with relative <img> paths, and a browser sends no Authorization
    header for those, so whatever serves it must authorise them another way. Behind the
    vast.ai Caddy edge that works out - one visit to /report/?token=... makes Caddy set
    the instance auth cookie, and every image request then carries it. (Jupyter's
    /files/ endpoint cannot do this: it sends `Content-Security-Policy: sandbox`, which
    puts the page in an opaque origin where no cookie is sent, so every image 302s to
    its login page.)

    No app bearer token is required, for the same reason - an <img> cannot send one. The
    edge token is the only thing gating it, which is why this stays test-only: the
    report contains the user's original photos.
    """
    configured = get_settings().wardrobe_report_dir
    if not configured:
        return
    path = Path(configured)
    if not path.is_dir():
        log.warning("WARDROBE_REPORT_DIR=%s does not exist yet; /report not mounted", configured)
        return
    app.mount("/report", StaticFiles(directory=path, html=True), name="report")
    log.info("test extraction report mounted at /report from %s", configured)


@app.on_event("startup")
def _startup():
    storage.ensure_buckets()
    _upload_test_face_fixture()
    _mount_test_report()
    t = threading.Thread(target=run_result_consumer, daemon=True)
    t.start()
    log.info("result_consumer thread started")


def _upload_test_face_fixture():
    """wardrobe-system-spec.md §2.3.7. Uploaded rather than read from disk at job time
    because the consumer is ai-server, which in production sits on another machine and
    cannot see this filesystem. A missing file is a warning, not a crash — D0b then
    falls back to the largest person in frame."""
    configured = get_settings().test_fixed_face_ref_image
    if not configured:
        return
    path = Path(configured)
    if not path.is_file():
        log.warning("TEST_FIXED_FACE_REF_IMAGE=%s not found; no shared test face reference", configured)
        return
    storage.upload_file(get_settings().minio_raw_bucket, storage.TEST_FIXTURE_FACE_KEY,
                        path, content_type="image/jpeg")
    log.info("test face fixture uploaded to %s from %s", storage.TEST_FIXTURE_FACE_KEY, configured)


def _test_fixture_face_key() -> Optional[str]:
    settings = get_settings()
    if not settings.test_fixed_face_ref_image:
        return None
    if not storage.object_exists(settings.minio_raw_bucket, storage.TEST_FIXTURE_FACE_KEY):
        return None
    return storage.TEST_FIXTURE_FACE_KEY


def _resolve_face_ref_key(account_id: str) -> Optional[str]:
    """Account's own registration wins, then the shared test fixture, then nothing
    (wardrobe-system-spec.md §2.3.7) — so configuring the fixture cannot change
    production behaviour for an account that registered a face."""
    return db.get_face_ref_key(account_id) or _test_fixture_face_key()


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
    contentType: str = "image/jpeg"


class FaceRefRegisterRequest(BaseModel):
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
    settings = get_settings()
    key = storage.face_object_key(identity["account_id"], req.contentType)
    return {
        "objectKey": key,
        "uploadUrl": storage.presign_put(settings.minio_raw_bucket, key, req.contentType,
                                         settings.presign_expires_seconds),
        "expiresAt": _expires_at(settings.presign_expires_seconds),
    }


@app.put("/v1/face-reference")
def face_reference_register(req: FaceRefRegisterRequest, identity=Depends(current_account)):
    settings = get_settings()
    expected = storage.face_object_key(identity["account_id"], "image/jpeg").rsplit(".", 1)[0]
    if not req.objectKey.startswith(expected):
        raise HTTPException(status_code=400, detail="objectKey does not belong to this account")
    # Recorded only once the bytes are actually there, so a failed upload can't leave
    # every later job pointing at an empty key.
    if not storage.object_exists(settings.minio_raw_bucket, req.objectKey):
        raise HTTPException(status_code=400, detail="no object uploaded at that objectKey")
    db.set_face_ref_key(identity["account_id"], req.objectKey)
    return {"registered": True, "objectKey": req.objectKey, "source": "account"}


@app.get("/v1/face-reference")
def face_reference_get(identity=Depends(current_account)):
    own = db.get_face_ref_key(identity["account_id"])
    key = own or _test_fixture_face_key()
    source = "account" if own else ("test-fixture" if key else None)
    return {"registered": key is not None, "objectKey": key, "source": source}


@app.post("/v1/jobs")
def create_job(req: CreateJobRequest, identity=Depends(current_account)):
    rows = db.get_batch_items(req.batchId, req.uploadedItems)
    if not rows:
        raise HTTPException(status_code=400, detail="no matching uploaded items for this batch")
    items = [{"local_id": r["local_id"], "object_key": r["object_key"]} for r in rows]
    job = db.create_job(identity["account_id"], identity["user_id"], req.batchId, items)
    db.mark_job_processing_if_pending(job["jobId"])
    face_ref_key = _resolve_face_ref_key(identity["account_id"])
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
