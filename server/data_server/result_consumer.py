"""Consumes ai-server's result_queue (wardrobe-system-spec.md §2.1 D4).

No database here any more: a photo's extracted garments are parked in Redis
(server/data_server/jobs.py) as a proposal for the user to review. Only what the
user confirms is written to postgres, by POST /v1/wardrobe/items. This loop just
records per-photo progress and results, and deletes the raw photo once it has been
processed.
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
    if raw_key:
        storage.delete_object(get_settings().minio_raw_bucket, raw_key)
