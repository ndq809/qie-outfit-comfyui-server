"""Redis-backed job queue / result queue (wardrobe-system-spec.md §2.1, §3.3).

data-server and ai-server never call each other directly — this module is the
only thing either side imports to talk to the other, via two lists plus a
cancellation set (see the plan's "cancellation needs a channel ai-server is
allowed to use" note for why cancellation rides on Redis rather than postgres).
"""
import json

import redis

from .config import get_settings

JOB_QUEUE = "job_queue"
RESULT_QUEUE = "result_queue"
JOB_QUEUE_DEAD = "job_queue_dead"
CANCELLED_JOBS_SET = "cancelled_jobs"

_client = None


def redis_client() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.Redis(**get_settings().redis_kwargs)
    return _client


def push_job(job_id: str, item_id: str, object_key: str, retry_count: int = 0):
    redis_client().lpush(JOB_QUEUE, json.dumps({
        "jobId": job_id, "itemId": item_id, "objectKey": object_key,
        "retryCount": retry_count,
    }))


def pop_job(timeout: int = 5):
    return _brpop(JOB_QUEUE, timeout)


def push_dead(ticket: dict):
    redis_client().lpush(JOB_QUEUE_DEAD, json.dumps(ticket))


def push_result(job_id: str, item_id: str, result: str, garments=None, error_reason: str = None):
    msg = {"jobId": job_id, "itemId": item_id, "result": result}
    if result == "success":
        msg["garments"] = garments or []
    else:
        msg["errorReason"] = error_reason or "unknown error"
    redis_client().lpush(RESULT_QUEUE, json.dumps(msg))


def pop_result(timeout: int = 5):
    return _brpop(RESULT_QUEUE, timeout)


def _brpop(key: str, timeout: int):
    """redis-py's blocking read can raise redis.exceptions.TimeoutError instead
    of returning None when BRPOP's own idle timeout elapses (observed with
    redis-py 8.1.0's RESP3 parser) — treat that exactly like the empty-list nil
    reply it's standing in for, not a real error."""
    try:
        popped = redis_client().brpop(key, timeout=timeout)
    except redis.exceptions.TimeoutError:
        return None
    if not popped:
        return None
    return json.loads(popped[1])


def mark_cancelled(job_id: str):
    redis_client().sadd(CANCELLED_JOBS_SET, job_id)


def is_cancelled(job_id: str) -> bool:
    return bool(redis_client().sismember(CANCELLED_JOBS_SET, job_id))


def clear_cancelled(job_id: str):
    redis_client().srem(CANCELLED_JOBS_SET, job_id)


def ping() -> bool:
    try:
        return redis_client().ping()
    except Exception:
        return False
