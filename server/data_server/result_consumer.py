"""Consumes ai-server's result_queue (wardrobe-system-spec.md §2.1 D4).

A photo's extracted garments are parked in Redis (server/data_server/jobs.py) as a
proposal for the user to review; there is no database. This loop records per-photo
progress and results, deletes the raw photo once it has been processed, and the face
photo the phone sent with the batch once the whole job is over (neither when
KEEP_RAW_IMAGES, test only).
"""
import logging

from server.common import queue, storage
from server.common.config import get_settings
from server.data_server import jobs

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
    if msg["result"] == "success":
        raw_key = jobs.finish_item(job_id, local_id, "success", None, msg.get("garments", []))
    else:
        raw_key = jobs.finish_item(job_id, local_id, "failed", msg.get("errorReason"))
    settings = get_settings()
    if raw_key and not settings.keep_raw_images:
        storage.delete_object(settings.minio_raw_bucket, raw_key)
    face_key = jobs.take_finished_face(job_id) if raw_key else None
    if face_key and not settings.keep_raw_images:
        storage.delete_object(settings.minio_raw_bucket, face_key)
