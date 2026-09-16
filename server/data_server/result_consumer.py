"""Consumes ai-server's result_queue (wardrobe-system-spec.md §2.1 D4).

Also runs the cross-item level of D2 here rather than in ai-server: this needs
every existing embedding for the account, which only postgres has, and
ai-server is deliberately never given postgres's address (§2.3.3). See the
plan's "D2 cross-item dedup vs ai-server/postgres boundary" note. Same-image
dedup (cheap, no DB needed) already happened in ai-server before this message
was sent.
"""
import logging

from server.common import queue, report, storage
from server.common.config import get_settings
from server.common.embeddings import most_similar
from server.data_server import db

log = logging.getLogger("result-consumer")


def run_result_consumer():
    log.info("result_consumer loop starting")
    while True:
        try:
            msg = queue.pop_result(timeout=5)
            if msg:
                _handle_result(msg)
        except Exception:
            log.exception("error handling result message")


def _handle_result(msg: dict):
    job_id, local_id = msg["jobId"], msg["itemId"]
    settings = get_settings()

    if msg["result"] == "success":
        info = db.finish_job_item(job_id, local_id, "success", None)
        if not info:
            return  # already terminal (duplicate delivery) — ignore
        outcomes = {}
        for garment in msg.get("garments", []):
            outcomes[garment["objectKey"]] = _insert_garment(
                info["account_id"], info["user_id"], job_id, info["job_item_id"], garment, settings)
        _mark_report(job_id, local_id, outcomes, settings)
    else:
        info = db.finish_job_item(job_id, local_id, "failed", msg.get("errorReason"))
        if not info:
            return

    item = db.job_item_by_local_id(job_id, local_id)
    if item and item["object_key"]:
        storage.delete_object(settings.minio_raw_bucket, item["object_key"])


def _mark_report(job_id: str, local_id: str, outcomes: dict, settings):
    """Test-only (WARDROBE_REPORT_DIR): tell the report which garments were kept."""
    if not settings.wardrobe_report_dir:
        return
    try:
        report.mark_wardrobe(settings.wardrobe_report_dir, job_id, local_id, outcomes)
        report.build_index(settings.wardrobe_report_dir)
    except Exception:
        log.exception("could not update the extraction report for %s/%s", job_id, local_id)


def _insert_garment(account_id: str, user_id: str, job_id: str, job_item_id: str, garment: dict,
                    settings) -> dict:
    visual_embedding = garment["visualEmbedding"]
    garment_type = (garment.get("tags") or {}).get("type")
    candidates = db.wardrobe_candidates_for_dedup(account_id, garment_type)
    match_id, score = most_similar(visual_embedding, candidates)

    duplicate_of = match_id if score >= settings.wardrobe_dedup_threshold else None
    if duplicate_of:
        log.info("garment deduped against %s (score=%.4f)", duplicate_of, score)
        # spec: data-server skips creating a new record for a confirmed duplicate
        return {"added": False, "duplicateOf": duplicate_of, "score": round(score, 4)}

    db.insert_wardrobe_item(
        account_id=account_id, user_id=user_id, job_id=job_id, job_item_id=job_item_id,
        object_key=garment["objectKey"], tags=garment["tags"], description=garment["description"],
        visual_embedding=visual_embedding, text_embedding=garment["textEmbedding"],
        duplicate_of=None,
    )
    return {"added": True, "duplicateOf": None, "score": round(score, 4) if match_id else None}
