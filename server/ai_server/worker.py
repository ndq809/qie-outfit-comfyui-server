"""ai-server worker loop (wardrobe-system-spec.md §2.1: D0b -> D1 -> D3 -> D2,
in that order — D2 needs D3's embeddings, see §2.1's note on why D2 moved after
D3). Pulls tickets from job_queue, reuses the existing D0b/D1/D3 reference
pipeline in test_extract_outfit.py verbatim, does same-image D2 dedup, uploads
crops, and reports back on result_queue. Never touches postgres (§2.3.3) —
cancellation is checked via the cancelled_jobs Redis set instead.
"""
import json
import logging
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root, for test_extract_outfit

import test_extract_outfit as pipeline  # noqa: E402

from server.common import queue, report, storage  # noqa: E402
from server.common.config import get_settings  # noqa: E402
from server.common.embeddings import cosine_similarity  # noqa: E402

log = logging.getLogger("ai-server.worker")

SAME_IMAGE_DEDUP_THRESHOLD = 0.90


def run_worker_loop(stop_event=None):
    log.info("worker loop starting")
    while stop_event is None or not stop_event.is_set():
        try:
            ticket = queue.pop_job(timeout=5)
            if not ticket:
                continue
            _handle_ticket(ticket)
        except Exception:
            log.exception("unhandled error in worker loop")


def _handle_ticket(ticket: dict):
    job_id, item_id, object_key = ticket["jobId"], ticket["itemId"], ticket["objectKey"]
    face_ref_key = ticket.get("faceRefKey")
    retry_count = ticket.get("retryCount", 0)

    if queue.is_cancelled(job_id):
        log.info("job %s cancelled, skipping ticket %s", job_id, item_id)
        queue.push_result(job_id, item_id, "failed", error_reason="cancelled")
        return

    settings = get_settings()
    with tempfile.TemporaryDirectory(prefix="wardrobe_") as tmp:
        tmp_dir = Path(tmp)
        try:
            garments = _process_image(job_id, item_id, object_key, face_ref_key, tmp_dir, settings)
            queue.push_result(job_id, item_id, "success", garments=garments)
            log.info("job %s item %s done: %d garment(s)", job_id, item_id, len(garments))
        except Exception as exc:
            log.exception("processing failed for job %s item %s", job_id, item_id)
            if retry_count < settings.max_job_retries:
                log.info("requeueing job %s item %s (retry %d)", job_id, item_id, retry_count + 1)
                queue.push_job(job_id, item_id, object_key, face_ref_key=face_ref_key,
                               retry_count=retry_count + 1)
            else:
                queue.push_dead(ticket)
                queue.push_result(job_id, item_id, "failed", error_reason=str(exc)[:500])


def _process_image(job_id: str, item_id: str, object_key: str, face_ref_key: str | None,
                   tmp_dir: Path, settings) -> list[dict]:
    ext = Path(object_key).suffix or ".jpg"
    raw_path = tmp_dir / f"input{ext}"
    storage.download_to(settings.minio_raw_bucket, object_key, raw_path)

    isolated_path = tmp_dir / "isolated.png"
    detected = _detect_with_face_ref(raw_path, face_ref_key, isolated_path, tmp_dir, settings)
    items = pipeline.prompt_items(detected)
    if not items:
        # Nothing was found on the subject - a head-and-shoulders portrait, or a crop
        # where no garment is visible. Reporting no garments is the honest answer, and
        # it saves ~20s of generation that would only invent an outfit.
        log.info("job %s item %s: no garment detected, nothing to extract", job_id, item_id)
        _write_report(job_id, item_id, raw_path, isolated_path, tmp_dir / "result.png",
                      [], [], [], detected, items, "", settings)
        return []
    prompt = pipeline.build_prompt(detected)

    generation_image_path = str(raw_path)
    if detected.get("isolated_by_sam") and isolated_path.exists():
        generation_image_path = str(isolated_path)

    result_path = tmp_dir / "result.png"
    _run_comfyui(generation_image_path, prompt, result_path)

    crop_dir = tmp_dir / "items"
    crops, records = pipeline.extract_and_classify(
        result_path, crop_dir, items=items, classify=True, device=None,
    )
    kept = _drop_same_image_duplicates(records) if records else []

    _write_report(job_id, item_id, raw_path, isolated_path, result_path,
                  crops, records, kept, detected, items, prompt, settings)
    if not records:
        return []

    garments = []
    for idx, record in enumerate(kept, start=1):
        crop = next((c for c in crops if c["path"].name == record["image_name"]), None)
        if crop is None:
            continue
        dest_key = storage.item_object_key(job_id, item_id, idx)
        storage.upload_file(settings.minio_items_bucket, dest_key, crop["path"])
        garments.append({
            "objectKey": dest_key,
            "tags": {
                "type": record["type"], "category": record["category"],
                "sub_category": record["sub_category"], "gender": record["gender"],
                "color": record["color"], "neck": record["neck"],
                "sleeve": record["sleeve"], "pattern": record["pattern"],
            },
            "description": record["original_text"],
            "visualEmbedding": record["visual_embedding"],
            "textEmbedding": record["text_embedding"],
        })
    return garments


def _write_report(job_id, item_id, raw_path, isolated_path, result_path,
                  crops, records, kept, detected, items, prompt, settings):
    """Test-only (WARDROBE_REPORT_DIR). Wrapped so a reporting problem can never fail a
    job whose extraction actually worked."""
    if not settings.wardrobe_report_dir:
        return
    try:
        report.save_stages(
            settings.wardrobe_report_dir, job_id, item_id,
            original=raw_path,
            isolated=isolated_path if detected.get("isolated_by_sam") else None,
            grid=result_path, crops=crops, records=records,
            object_keys={r["image_name"]: storage.item_object_key(job_id, item_id, idx)
                         for idx, r in enumerate(kept, start=1)},
            detected={**detected, "_asked_items": items}, prompt=prompt,
        )
        report.build_index(settings.wardrobe_report_dir)
    except Exception:
        log.exception("could not write the extraction report for %s/%s", job_id, item_id)


def _detect_with_face_ref(raw_path: Path, face_ref_key: str | None, isolated_path: Path,
                          tmp_dir: Path, settings) -> dict:
    """D0b. The ticket carries only a key (wardrobe-system-spec.md §2.3.7) — where it
    came from, a real registration or the test fixture, is data-server's business.

    A reference that matches nobody in the photo is not a failure: the spec's fallback
    is "largest person in frame", which is exactly what the detector does with no
    reference at all, so retry that way rather than failing the ticket."""
    selfie_path = None
    if face_ref_key:
        try:
            selfie_path = str(storage.download_to(
                settings.minio_raw_bucket, face_ref_key, tmp_dir / Path(face_ref_key).name))
        except Exception:
            log.warning("face reference %s could not be fetched; using largest person in frame",
                        face_ref_key)

    if selfie_path:
        try:
            return pipeline.detect_worn_items(
                str(raw_path), selfie=selfie_path, save_isolated_to=str(isolated_path))
        except Exception as exc:
            log.info("no face matched the reference (%s); using largest person in frame", exc)

    return pipeline.detect_worn_items(str(raw_path), save_isolated_to=str(isolated_path))


def _drop_same_image_duplicates(records: list[dict]) -> list[dict]:
    """D2, same-source-image level (wardrobe-system-spec.md §2.1): rare, but a
    generation can draw the same garment into two grid cells. Cheaper to catch
    here than let it into the database."""
    kept: list[dict] = []
    for record in records:
        emb = record["visual_embedding"]
        if any(cosine_similarity(emb, k["visual_embedding"]) >= SAME_IMAGE_DEDUP_THRESHOLD for k in kept):
            log.info("dropping same-image duplicate crop %s", record["image_name"])
            continue
        kept.append(record)
    return kept


def _run_comfyui(image_path: str, prompt: str, result_path: Path, seed: int = 42):
    settings = get_settings()
    comfy_url = settings.comfyui_url
    image_name = pipeline.upload_image(image_path)
    workflow = pipeline.build_workflow(image_name, prompt, seed=seed, lightning_steps=4, fp8=True)

    client_id = uuid.uuid4().hex
    payload = json.dumps({"prompt": workflow, "client_id": client_id}).encode()
    req = urllib.request.Request(f"{comfy_url}/prompt", data=payload, headers={"Content-Type": "application/json"})
    try:
        resp = json.loads(urllib.request.urlopen(req).read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"ComfyUI rejected workflow: {e.read().decode()}") from e
    prompt_id = resp["prompt_id"]

    deadline = time.time() + 300
    while time.time() < deadline:
        with urllib.request.urlopen(f"{comfy_url}/history/{prompt_id}") as r:
            hist = json.loads(r.read())
        if prompt_id in hist:
            entry = hist[prompt_id]
            status = entry.get("status", {})
            if status.get("completed"):
                outputs = entry["outputs"]
                saved = [img["filename"] for out in outputs.values() for img in out.get("images", [])]
                if not saved:
                    raise RuntimeError("ComfyUI produced no image output")
                pipeline.download_output(saved[0], result_path)
                return
            if status.get("status_str") == "error":
                raise RuntimeError(f"ComfyUI generation failed: {json.dumps(status)}")
        time.sleep(2)
    raise RuntimeError(f"ComfyUI generation timed out for prompt_id={prompt_id}")
