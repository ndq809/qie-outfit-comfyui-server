"""data-server — the only publicly-callable component (wardrobe-system-spec.md
§3.1). Issues presigned upload URLs, creates jobs (with the user's face embedding the
phone sends, or a face photo) and pushes job-queue tickets, and (via result_consumer,
a background thread) collects ai-server's results for the user to review.

No database: job state and results live in Redis with a TTL (jobs.py), the wardrobe
lives on the phone - confirming a garment hands it the image URL, tags and embeddings
to keep - and API tokens come from API_TOKENS in the environment.
"""
import logging
import threading
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from garment_description import build_description
from server.common import face_ref, queue, report, storage
from server.common.config import get_settings
from server.data_server import jobs
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
    """wardrobe-system-spec.md §2.3.7: the face used for a job whose batch came without
    one. Uploaded rather than read from disk at job time because the consumer is
    ai-server, which in production sits on another machine and cannot see this
    filesystem. A missing file is a warning, not a crash — D0b then falls back to the
    largest person in frame."""
    configured = get_settings().test_fixed_face_ref_image
    if not configured:
        return
    path = Path(configured)
    if not path.is_file():
        log.warning("TEST_FIXED_FACE_REF_IMAGE=%s not found; no fallback face reference", configured)
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


def _job_face(account_id: str, batch_id: str, sent: Optional["FaceEmbedding"]) -> dict:
    """What D0b matches the user against (wardrobe-system-spec.md §2.3.7), in this order:
    the face embedding the phone sent with the job; the face photo it uploaded with the
    batch; the shared test fixture; nothing - ai-server then takes the largest person in
    frame. An embedding that fails validation (wrong model, size, NaN) is not an error: the
    job falls back and the response carries the reason as a warning, so a bad client
    build still gets its photos processed. A face slot that was presigned but never
    uploaded counts as no photo, so a failed upload cannot point every photo at nothing.

    Returns {"key", "embedding", "source", "warning"}; source is "embedding" / "image" /
    "test-fixture" / None."""
    warning = None
    if sent is not None:
        embedding, warning = face_ref.validate(sent.embedding, sent.model)
        if embedding:
            return {"key": None, "embedding": embedding, "source": "embedding", "warning": None}
        log.warning("job face embedding rejected: %s", warning)
    key = jobs.batch_face_ref_key(account_id, batch_id)
    if key and storage.object_exists(get_settings().minio_raw_bucket, key):
        return {"key": key, "embedding": None, "source": "image", "warning": warning}
    fixture = _test_fixture_face_key()
    return {"key": fixture, "embedding": None, "source": "test-fixture" if fixture else None,
            "warning": warning}


# --- auth -------------------------------------------------------------------

def current_account(authorization: Optional[str] = Header(None)):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    account_id = get_settings().api_token_accounts.get(token)
    if not account_id:
        raise HTTPException(status_code=401, detail="invalid token")
    return {"account_id": account_id}


# --- schemas ------------------------------------------------------------------

class PresignItem(BaseModel):
    localId: str
    contentType: str
    checksum: Optional[str] = None


class FaceReferenceSlot(BaseModel):
    contentType: str = "image/jpeg"


class PresignRequest(BaseModel):
    items: list[PresignItem]
    # A face *photo* for D0b to pick the user out of group photos - for a client that
    # cannot send the embedding with POST /v1/jobs, which is preferred (the photo then
    # never leaves the phone). Without either the job uses TEST_FIXED_FACE_REF_IMAGE.
    faceReference: Optional[FaceReferenceSlot] = None


class FaceEmbedding(BaseModel):
    # The user's face embedding as the phone computes it (§1.1 A4): 512 floats from
    # InsightFace w600k_mbf. See server/common/face_ref.py.
    embedding: list[float]
    model: str


class CreateJobRequest(BaseModel):
    batchId: str
    uploadedItems: list[str]
    faceReference: Optional[FaceEmbedding] = None


class ConfirmGarment(BaseModel):
    garmentId: str
    # Optional corrections from the review screen. Keys given here replace D3's value;
    # keys left out keep it.
    tags: Optional[dict] = None


class ConfirmRequest(BaseModel):
    garments: list[ConfirmGarment]


class RejectRequest(BaseModel):
    garmentIds: list[str]


# --- routes -------------------------------------------------------------------

@app.post("/v1/uploads/presign")
def presign(req: PresignRequest, identity=Depends(current_account)):
    settings = get_settings()
    account_id = identity["account_id"]
    batch_id = jobs.new_batch_id()
    face = None
    if req.faceReference:
        key = storage.face_object_key(account_id, batch_id, req.faceReference.contentType)
        face = {
            "objectKey": key,
            "uploadUrl": storage.presign_put(settings.minio_raw_bucket, key,
                                             req.faceReference.contentType,
                                             settings.presign_expires_seconds),
            "expiresAt": _expires_at(settings.presign_expires_seconds),
        }
    jobs.create_upload_batch(account_id, batch_id, face["objectKey"] if face else None)
    out_items, stored = [], []
    for item in req.items:
        key = storage.raw_object_key(account_id, batch_id, item.localId, item.contentType)
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
    out = {"batchId": batch_id, "items": out_items}
    if face:
        out["faceReference"] = face
    return out


@app.post("/v1/jobs")
def create_job(req: CreateJobRequest, identity=Depends(current_account)):
    rows = jobs.get_batch_items(identity["account_id"], req.batchId, req.uploadedItems)
    if not rows:
        raise HTTPException(status_code=400, detail="no matching uploaded items for this batch")
    face = _job_face(identity["account_id"], req.batchId, req.faceReference)
    job = jobs.create_job(identity["account_id"], req.batchId, rows, face["key"], face["source"])
    for it in rows:
        queue.push_job(job["jobId"], it["localId"], it["objectKey"], face_ref_key=face["key"],
                       face_ref_embedding=face["embedding"])
    if face["warning"]:
        job["faceReference"]["warning"] = face["warning"]
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
def job_garments(job_id: str, includeEmbeddings: bool = Query(False),
                 identity=Depends(current_account)):
    """What the user reviews: every garment extracted so far, with the AI's tags. Can be
    called while the job is still running. includeEmbeddings: also return the D3
    embeddings, for the phone to match garments against the wardrobe it keeps."""
    settings = get_settings()
    garments = jobs.list_garments(identity["account_id"], job_id)
    if garments is None:
        raise HTTPException(status_code=404, detail="job not found")
    # D2 across the job's photos (jobs._mark_job_duplicates): the copies are not listed,
    # the garment they copy says which other photos it was also extracted from.
    copies = {}
    for g in garments:
        if g["reviewStatus"] == "duplicate":
            copies.setdefault(g.get("duplicateOf"), []).append(
                {"garmentId": g["garmentId"], "localId": g["localId"], "score": g.get("duplicateScore")})
    out = []
    for g in garments:
        if g["reviewStatus"] == "duplicate":
            continue
        entry = _garment_out(g, settings, includeEmbeddings)
        if g["garmentId"] in copies:
            entry["duplicates"] = copies[g["garmentId"]]
        out.append(entry)
    return {"jobId": job_id, "garments": out}


def _garment_out(g: dict, settings, embeddings: bool) -> dict:
    entry = {
        "garmentId": g["garmentId"],
        "localId": g["localId"],
        "reviewStatus": g["reviewStatus"],
        "imageUrl": (storage.presign_get(settings.minio_items_bucket, g["objectKey"],
                                         settings.read_url_expires_seconds)
                     if g["reviewStatus"] != "rejected" else None),
        "tags": g["tags"],
        "description": g["description"],
    }
    if g["reviewStatus"] == "confirmed":
        entry["aiTags"] = g.get("aiTags", g["tags"])
        entry["tagsEdited"] = g.get("tagsEdited", False)
    if embeddings:
        entry["visualEmbedding"] = g["visualEmbedding"]
        entry["textEmbedding"] = g["textEmbedding"]
    return entry


@app.post("/v1/jobs/{job_id}/garments/reject")
def reject_garments(job_id: str, req: RejectRequest, identity=Depends(current_account)):
    settings = get_settings()
    found = jobs.get_garments(identity["account_id"], job_id, req.garmentIds)
    if found is None:
        raise HTTPException(status_code=404, detail="job not found")
    rejected = []
    for gid, g in found.items():
        if g["reviewStatus"] == "confirmed":
            continue  # already handed to the phone's wardrobe - removing it is the app's call
        if g["reviewStatus"] in ("pending", "duplicate"):
            storage.delete_object(settings.minio_items_bucket, g["objectKey"])
            jobs.set_review(job_id, gid, "rejected")
            _mark_report(job_id, g, {"added": False, "rejected": True}, settings)
        rejected.append(gid)
    return {"rejected": rejected,
            "notFound": [gid for gid in req.garmentIds if gid not in found],
            "alreadyConfirmed": [gid for gid, g in found.items() if g["reviewStatus"] == "confirmed"]}


@app.post("/v1/jobs/{job_id}/garments/confirm")
def confirm_garments(job_id: str, req: ConfirmRequest, identity=Depends(current_account)):
    """The user keeps these garments (optionally with corrected tags). Nothing is stored
    for them beyond the job: the response carries what the phone needs to put them in
    the wardrobe it keeps - image URL (download it; the image expires with the job),
    final tags, description and the D3 embeddings. Idempotent per garmentId; confirming
    again with other tags re-applies them on top of the AI's."""
    settings = get_settings()
    found = jobs.get_garments(identity["account_id"], job_id, [g.garmentId for g in req.garments])
    if found is None:
        raise HTTPException(status_code=404, detail="job not found")
    items, errors = [], []
    for ask in req.garments:
        g = found.get(ask.garmentId)
        if g is None:
            errors.append({"garmentId": ask.garmentId, "error": "not found"})
            continue
        if g["reviewStatus"] == "rejected":
            errors.append({"garmentId": ask.garmentId, "error": "already rejected"})
            continue
        ai_tags = g.get("aiTags", g["tags"])
        tags = {**ai_tags, **(ask.tags or {})} if ask.tags or g["reviewStatus"] != "confirmed" else g["tags"]
        edited = tags != ai_tags
        description = _describe(tags) if edited else g["description"]
        if g["reviewStatus"] != "confirmed" or tags != g["tags"]:
            updates = {"tags": tags, "aiTags": ai_tags, "tagsEdited": edited, "description": description}
            jobs.set_review(job_id, ask.garmentId, "confirmed", updates)
            if g["reviewStatus"] != "confirmed":
                _mark_report(job_id, g, {"added": True}, settings)
            g = {**g, **updates, "reviewStatus": "confirmed"}
        items.append(_garment_out(g, settings, embeddings=True))
    return {"items": items, "errors": errors}


def _describe(tags: dict) -> str:
    try:
        return build_description(tags.get("type"), tags.get("gender"), tags.get("category"),
                                 tags.get("sub_category"), tags.get("color") or [],
                                 tags.get("neck") or [], tags.get("sleeve") or [],
                                 tags.get("pattern") or [])
    except Exception:
        return ""


def _mark_report(job_id: str, garment: dict, outcome: dict, settings):
    """Test-only (WARDROBE_REPORT_DIR): show the user's decision on the report page."""
    if not settings.wardrobe_report_dir:
        return
    try:
        report.mark_review(settings.wardrobe_report_dir, job_id, garment["localId"],
                           garment["objectKey"], outcome)
    except Exception:
        log.exception("could not update the extraction report for %s", job_id)


@app.get("/v1/health")
def health():
    deps = {
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
