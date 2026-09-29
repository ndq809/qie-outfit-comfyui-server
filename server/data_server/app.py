"""data-server — the only publicly-callable component (wardrobe-system-spec.md
§3.1). Issues presigned upload URLs, creates jobs and pushes job-queue tickets, and
(via result_consumer, a background thread) collects ai-server's results for the user
to review. Job state and unreviewed results live in Redis (jobs.py); postgres only
receives what the user confirms (POST /v1/wardrobe/items).
"""
import logging
import threading
import uuid
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from garment_description import build_description
from server.common import queue, report, storage
from server.common.config import get_settings
from server.common.embeddings import most_similar
from server.data_server import db, jobs
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


@app.get("/report", include_in_schema=False)
@app.get("/report/", include_in_schema=False)
@app.get("/report/index.html", include_in_schema=False)
def report_index():
    """The report page, rendered when it is opened rather than after every processed
    photo (server.common.report.render_index). Declared as a route so it wins over the
    static mount below, which then only serves the pictures."""
    configured = get_settings().wardrobe_report_dir
    if not configured or not Path(configured).is_dir():
        raise HTTPException(status_code=404, detail="report not configured")
    return HTMLResponse(report.render_index(configured), headers={"Cache-Control": "no-cache"})


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


class ConfirmGarment(BaseModel):
    garmentId: str
    # Optional corrections from the review screen. Keys given here replace D3's value;
    # keys left out keep it.
    tags: Optional[dict] = None


class ConfirmRequest(BaseModel):
    jobId: str
    garments: list[ConfirmGarment]


class RejectRequest(BaseModel):
    garmentIds: list[str]


class FaceRefPresignRequest(BaseModel):
    contentType: str = "image/jpeg"


class FaceRefRegisterRequest(BaseModel):
    objectKey: str


# --- routes -------------------------------------------------------------------

@app.post("/v1/uploads/presign")
def presign(req: PresignRequest, identity=Depends(current_account)):
    settings = get_settings()
    batch_id = jobs.create_upload_batch(identity["account_id"], identity["user_id"])
    out_items, stored = [], []
    for item in req.items:
        key = storage.raw_object_key(identity["account_id"], batch_id, item.localId, item.contentType)
        stored.append({"localId": item.localId, "objectKey": key,
                       "contentType": item.contentType, "checksum": item.checksum})
        url = storage.presign_put(settings.minio_raw_bucket, key, item.contentType, settings.presign_expires_seconds)
        out_items.append({
            "localId": item.localId,
            "objectKey": key,
            "uploadUrl": url,
            "expiresAt": _expires_at(settings.presign_expires_seconds),
        })
    if stored:
        jobs.add_batch_items(batch_id, stored)
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
    rows = jobs.get_batch_items(identity["account_id"], req.batchId, req.uploadedItems)
    if not rows:
        raise HTTPException(status_code=400, detail="no matching uploaded items for this batch")
    job = jobs.create_job(identity["account_id"], identity["user_id"], req.batchId, rows)
    face_ref_key = _resolve_face_ref_key(identity["account_id"])
    for it in rows:
        queue.push_job(job["jobId"], it["localId"], it["objectKey"], face_ref_key=face_ref_key)
    return job


@app.get("/v1/jobs/{job_id}")
def get_job(job_id: str, identity=Depends(current_account)):
    job = jobs.get_job(identity["account_id"], job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@app.post("/v1/jobs/{job_id}/cancel")
def cancel_job(job_id: str, identity=Depends(current_account)):
    status = jobs.cancel_job(identity["account_id"], job_id)
    if not status:
        raise HTTPException(status_code=404, detail="job not found")
    if status == "cancelling":
        queue.mark_cancelled(job_id)
    return {"status": status}


@app.get("/v1/jobs/{job_id}/garments")
def job_garments(job_id: str, identity=Depends(current_account)):
    """What the user reviews: every garment extracted so far, with the AI's tags. Can be
    called while the job is still running."""
    settings = get_settings()
    garments = jobs.list_garments(identity["account_id"], job_id)
    if garments is None:
        raise HTTPException(status_code=404, detail="job not found")
    out = []
    for g in garments:
        key = g["objectKey"]
        if g["reviewStatus"] == "confirmed":
            key = storage.wardrobe_object_key(identity["account_id"], g["wardrobeItemId"])
        entry = {
            "garmentId": g["garmentId"],
            "localId": g["localId"],
            "reviewStatus": g["reviewStatus"],
            "imageUrl": (storage.presign_get(settings.minio_items_bucket, key,
                                             settings.read_url_expires_seconds)
                         if g["reviewStatus"] != "rejected" else None),
            "tags": g["tags"],
            "description": g["description"],
        }
        if g.get("wardrobeItemId"):
            entry["wardrobeItemId"] = g["wardrobeItemId"]
        if settings.wardrobe_dedup_enabled and g["reviewStatus"] == "pending":
            entry["possibleDuplicate"] = _possible_duplicate(identity["account_id"], g, settings)
        out.append(entry)
    return {"jobId": job_id, "garments": out}


def _possible_duplicate(account_id: str, garment: dict, settings) -> Optional[dict]:
    """D2 against the wardrobe, as a hint for the review screen rather than a silent drop:
    the user decides whether it is really the same piece."""
    candidates = db.wardrobe_candidates_for_dedup(account_id, (garment.get("tags") or {}).get("type"))
    match_id, score = most_similar(garment["visualEmbedding"], candidates)
    if match_id and score >= settings.wardrobe_dedup_threshold:
        return {"wardrobeItemId": match_id, "score": round(score, 4)}
    return None


@app.post("/v1/jobs/{job_id}/garments/reject")
def reject_garments(job_id: str, req: RejectRequest, identity=Depends(current_account)):
    settings = get_settings()
    found = jobs.get_garments(identity["account_id"], job_id, req.garmentIds)
    if found is None:
        raise HTTPException(status_code=404, detail="job not found")
    rejected = []
    for gid, g in found.items():
        if g["reviewStatus"] == "confirmed":
            continue  # already in the wardrobe - removing it is a different operation
        if g["reviewStatus"] == "pending":
            storage.delete_object(settings.minio_items_bucket, g["objectKey"])
            jobs.set_review(job_id, gid, "rejected")
            _mark_report(job_id, g, {"added": False, "rejected": True}, settings)
        rejected.append(gid)
    return {"rejected": rejected,
            "notFound": [gid for gid in req.garmentIds if gid not in found],
            "alreadyConfirmed": [gid for gid, g in found.items() if g["reviewStatus"] == "confirmed"]}


@app.post("/v1/wardrobe/items")
def confirm_wardrobe_items(req: ConfirmRequest, identity=Depends(current_account)):
    """The only way anything enters the wardrobe: the user confirmed these garments
    (optionally with corrected tags) after reviewing the job's results. Idempotent per
    garmentId, so a retried request does not create a second copy."""
    settings = get_settings()
    account_id = identity["account_id"]
    found = jobs.get_garments(account_id, req.jobId, [g.garmentId for g in req.garments])
    if found is None:
        raise HTTPException(status_code=404, detail="job not found")
    created, errors = [], []
    for ask in req.garments:
        g = found.get(ask.garmentId)
        if g is None:
            errors.append({"garmentId": ask.garmentId, "error": "not found"})
            continue
        if g["reviewStatus"] == "rejected":
            errors.append({"garmentId": ask.garmentId, "error": "already rejected"})
            continue
        if g["reviewStatus"] == "confirmed":
            existing = db.find_by_source_garment(account_id, ask.garmentId)
            if existing:
                created.append(_wardrobe_out(existing, settings, ask.garmentId))
            continue

        tags = {**g["tags"], **(ask.tags or {})}
        edited = tags != g["tags"]
        description = _describe(tags) if edited else g["description"]
        item_id = str(uuid.uuid4())
        dest_key = storage.wardrobe_object_key(account_id, item_id)
        try:
            storage.move_object(settings.minio_items_bucket, g["objectKey"], dest_key)
        except Exception:
            log.exception("could not move %s into the wardrobe", g["objectKey"])
            errors.append({"garmentId": ask.garmentId, "error": "image no longer available"})
            continue
        try:
            db.insert_wardrobe_item(
                item_id=item_id, account_id=account_id, user_id=g["_user_id"], job_id=req.jobId,
                source_local_id=g["localId"], source_garment_id=ask.garmentId, object_key=dest_key,
                tags=tags, ai_tags=g["tags"], tags_edited=edited, description=description,
                visual_embedding=g["visualEmbedding"], text_embedding=g["textEmbedding"])
        except Exception:
            # Put the image back so a retry of this request finds it where it expects.
            storage.move_object(settings.minio_items_bucket, dest_key, g["objectKey"])
            raise
        jobs.set_review(req.jobId, ask.garmentId, "confirmed", item_id)
        _mark_report(req.jobId, g, {"added": True}, settings)
        created.append(_wardrobe_out({"id": item_id, "object_key": dest_key, "tags": tags,
                                      "job_id": req.jobId}, settings, ask.garmentId))
    return {"items": created, "errors": errors}


def _describe(tags: dict) -> str:
    try:
        return build_description(tags.get("type"), tags.get("gender"), tags.get("category"),
                                 tags.get("sub_category"), tags.get("color") or [],
                                 tags.get("neck") or [], tags.get("sleeve") or [],
                                 tags.get("pattern") or [])
    except Exception:
        return ""


def _wardrobe_out(row: dict, settings, garment_id: Optional[str] = None) -> dict:
    out = {
        "id": str(row["id"]),
        "imageUrl": storage.presign_get(settings.minio_items_bucket, row["object_key"],
                                        settings.read_url_expires_seconds),
        "tags": row["tags"],
        "jobId": str(row["job_id"]),
    }
    if garment_id:
        out["garmentId"] = garment_id
    return out


def _mark_report(job_id: str, garment: dict, outcome: dict, settings):
    """Test-only (WARDROBE_REPORT_DIR): show the user's decision on the report page."""
    if not settings.wardrobe_report_dir:
        return
    try:
        report.mark_review(settings.wardrobe_report_dir, job_id, garment["localId"],
                           garment["objectKey"], outcome)
    except Exception:
        log.exception("could not update the extraction report for %s", job_id)


@app.get("/v1/wardrobe/items")
def wardrobe_items(jobId: Optional[str] = Query(None), cursor: Optional[str] = Query(None),
                    limit: int = Query(50, ge=1, le=200), identity=Depends(current_account)):
    settings = get_settings()
    rows, next_cursor = db.list_wardrobe_items(identity["account_id"], jobId, cursor, limit)
    out = {"items": [_wardrobe_out(r, settings) for r in rows]}
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
