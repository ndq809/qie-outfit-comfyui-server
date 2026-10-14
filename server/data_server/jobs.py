"""Upload batches, jobs and their extraction results — all the state data-server keeps,
in Redis. There is no database: the wardrobe lives on the phone, which takes what the
user confirms out of the confirm response (image URL, tags, embeddings) and stores it.
Everything here has a TTL; a job nobody comes back to simply expires, and its crops sit
under the pending/ prefix, which a MinIO lifecycle rule expires on the same schedule
(storage.ensure_buckets).

Keys (all refreshed to JOB_TTL_SECONDS on every write):
  wd:batch:{batchId}            hash  account_id, face_ref_key (the slot presigned for the
                                      phone's face photo, if it asked for one)
  wd:batch:{batchId}:items      hash  localId -> {objectKey, contentType, checksum}
  wd:job:{jobId}                hash  account_id, batch_id, status, counters, times,
                                      face_ref_key, face_source (embedding | image |
                                      test-fixture | ""); the embedding itself is only in the
                                      job's tickets, never stored here
  wd:job:{jobId}:items          hash  localId -> {objectKey, status, errorReason}
  wd:job:{jobId}:garments       hash  garmentId -> {localId, index, objectKey, tags,
                                      description, visualEmbedding, textEmbedding,
                                      reviewStatus, aiTags, duplicateOf, duplicateScore}
  reviewStatus: pending | confirmed | rejected | duplicate (D2 across the job's photos -
  see _mark_job_duplicates; never shown for review)

The two multi-field transitions (an item finishing, a cancel) run as Lua scripts, so
the result consumer and a cancel request arriving together cannot leave a job
"completed" that should have been "cancelled" or the other way round.
"""
import json
import uuid
from datetime import datetime, timezone

from server.common.config import get_settings
from server.common.embeddings import best_same_garment
from server.common.queue import redis_client

TERMINAL = ("completed", "cancelled", "failed")


def _batch_key(batch_id): return f"wd:batch:{batch_id}"
def _job_key(job_id): return f"wd:job:{job_id}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ttl() -> int:
    return get_settings().job_ttl_seconds


def _touch(pipe, *keys):
    for k in keys:
        pipe.expire(k, _ttl())


# --- upload batches ---------------------------------------------------------------

def new_batch_id() -> str:
    return str(uuid.uuid4())


def create_upload_batch(account_id: str, batch_id: str, face_ref_key: str | None = None):
    r = redis_client().pipeline()
    r.hset(_batch_key(batch_id), mapping={"account_id": account_id, "face_ref_key": face_ref_key or ""})
    _touch(r, _batch_key(batch_id))
    r.execute()


def batch_face_ref_key(account_id: str, batch_id: str) -> str | None:
    """The face slot presigned with this batch (uploaded or not), None if it had none."""
    batch = redis_client().hgetall(_batch_key(batch_id))
    if batch.get("account_id") != account_id:
        return None
    return batch.get("face_ref_key") or None


def add_batch_items(batch_id: str, items: list[dict]):
    """items: [{localId, objectKey, contentType, checksum}]"""
    key = f"{_batch_key(batch_id)}:items"
    r = redis_client().pipeline()
    r.hset(key, mapping={it["localId"]: json.dumps(it) for it in items})
    _touch(r, key)
    r.execute()


def get_batch_items(account_id: str, batch_id: str, local_ids: list[str]) -> list[dict]:
    r = redis_client()
    if r.hget(_batch_key(batch_id), "account_id") != account_id:
        return []
    raw = r.hmget(f"{_batch_key(batch_id)}:items", local_ids) if local_ids else []
    return [json.loads(x) for x in raw if x]


# --- jobs -------------------------------------------------------------------------

def create_job(account_id: str, batch_id: str, items: list[dict],
               face_ref_key: str | None, face_source: str | None) -> dict:
    """items: [{localId, objectKey}]. Created straight in 'processing': the tickets are
    pushed right after this returns, so 'pending' would only ever be seen by a race.
    face_source: "embedding" (the phone sent its face embedding), "image" (a face photo
    uploaded with the batch), "test-fixture", or None."""
    job_id = str(uuid.uuid4())
    now = _now()
    jk = _job_key(job_id)
    r = redis_client().pipeline()
    r.hset(jk, mapping={
        "account_id": account_id, "batch_id": batch_id,
        "face_ref_key": face_ref_key or "", "face_source": face_source or "",
        "status": "processing", "total_items": len(items),
        "processed_items": 0, "failed_items": 0, "created_at": now, "updated_at": now,
    })
    r.hset(f"{jk}:items", mapping={
        it["localId"]: json.dumps({"objectKey": it["objectKey"], "status": "pending"})
        for it in items})
    _touch(r, jk, f"{jk}:items")
    r.execute()
    return {"jobId": job_id, "status": "pending", "totalItems": len(items), "createdAt": now,
            "faceReference": {"source": face_source}}


def _owned_job(account_id: str, job_id: str) -> dict | None:
    job = redis_client().hgetall(_job_key(job_id))
    if not job or job.get("account_id") != account_id:
        return None
    return job


def get_job(account_id: str, job_id: str) -> dict | None:
    job = _owned_job(account_id, job_id)
    if not job:
        return None
    items = redis_client().hgetall(f"{_job_key(job_id)}:items")
    garments = _garments(job_id)
    per_item, dup_item = {}, {}
    for g in garments:
        counter = dup_item if g["reviewStatus"] == "duplicate" else per_item
        counter[g["localId"]] = counter.get(g["localId"], 0) + 1
    out_items = []
    for local_id, raw in items.items():
        it = json.loads(raw)
        if it["status"] == "pending":
            continue
        entry = {"localId": local_id, "status": it["status"]}
        if it["status"] == "success":
            entry["garmentCount"] = per_item.get(local_id, 0)
            if dup_item.get(local_id):
                entry["duplicateCount"] = dup_item[local_id]
        out_items.append(entry)
    return {
        "status": job["status"],
        "totalItems": int(job["total_items"]),
        "processedItems": int(job["processed_items"]),
        "failedItems": int(job["failed_items"]),
        "updatedAt": job["updated_at"],
        "items": out_items,
        "review": _review_counts(garments),
        "faceReference": {"source": job.get("face_source") or None},
    }


_CANCEL = """
local st = redis.call('HGET', KEYS[1], 'status')
if not st then return nil end
if st == 'completed' or st == 'cancelled' or st == 'failed' then return st end
redis.call('HSET', KEYS[1], 'status', 'cancelling', 'updated_at', ARGV[1])
return 'cancelling'
"""


def cancel_job(account_id: str, job_id: str) -> str | None:
    if not _owned_job(account_id, job_id):
        return None
    return redis_client().eval(_CANCEL, 1, _job_key(job_id), _now())


_FINISH = """
local raw = redis.call('HGET', KEYS[2], ARGV[1])
if not raw then return nil end
local it = cjson.decode(raw)
if it['status'] ~= 'pending' then return nil end
it['status'] = ARGV[2]
if ARGV[3] ~= '' then it['errorReason'] = ARGV[3] end
redis.call('HSET', KEYS[2], ARGV[1], cjson.encode(it))
local counter = 'failed_items'
if ARGV[2] == 'success' then counter = 'processed_items' end
redis.call('HINCRBY', KEYS[1], counter, 1)
redis.call('HSET', KEYS[1], 'updated_at', ARGV[4])
local done = tonumber(redis.call('HGET', KEYS[1], 'processed_items'))
           + tonumber(redis.call('HGET', KEYS[1], 'failed_items'))
if done >= tonumber(redis.call('HGET', KEYS[1], 'total_items')) then
  if redis.call('HGET', KEYS[1], 'status') == 'cancelling' then
    redis.call('HSET', KEYS[1], 'status', 'cancelled')
  else
    redis.call('HSET', KEYS[1], 'status', 'completed')
  end
end
return it['objectKey']
"""


def finish_item(job_id: str, local_id: str, status: str, error_reason: str | None,
                garments: list[dict] | None = None) -> str | None:
    """Marks one photo terminal and stores its garments for review. Returns the raw
    photo's object key (for the caller to delete), or None when the item was already
    terminal (duplicate delivery) or the job has expired."""
    jk = _job_key(job_id)
    raw_key = redis_client().eval(_FINISH, 2, jk, f"{jk}:items", local_id, status,
                                  error_reason or "", _now())
    if raw_key is None:
        return None
    r = redis_client().pipeline()
    if garments:
        stored = {uuid.uuid4().hex: {
            "localId": local_id, "index": idx, "objectKey": g["objectKey"],
            "tags": g["tags"], "description": g["description"],
            "visualEmbedding": g["visualEmbedding"], "textEmbedding": g["textEmbedding"],
            "reviewStatus": "pending",
        } for idx, g in enumerate(garments, start=1)}
        if get_settings().wardrobe_dedup_enabled:
            _mark_job_duplicates(job_id, local_id, stored)
        r.hset(f"{jk}:garments", mapping={gid: json.dumps(g) for gid, g in stored.items()})
    _touch(r, jk, f"{jk}:items", f"{jk}:garments")
    r.execute()
    return raw_key


def take_finished_face(job_id: str) -> str | None:
    """The face photo the phone sent for this job, once the job is over and nothing will
    read it again - handed out once (the field is cleared), for the caller to delete. The
    shared test fixture is never handed out. Only the result-consumer thread calls this,
    so reading and clearing the field need not be atomic."""
    r = redis_client()
    status, source, key = r.hmget(_job_key(job_id), "status", "face_source", "face_ref_key")
    if status not in TERMINAL or source != "image" or not key:
        return None
    r.hset(_job_key(job_id), "face_ref_key", "")
    return key


def _mark_job_duplicates(job_id: str, local_id: str, new: dict):
    """D2 across the photos of one job: the same person in the same outfit is usually in
    several photos of a batch, and each of them extracts the same shirt again. A garment
    that matches one already extracted from another photo of this job
    (embeddings.same_garment_score) is stored as "duplicate" of it and left out of the
    review list; the first copy stands for all of them. Results arrive one photo at a
    time on the single result-consumer thread, so "already extracted" is well defined.
    Rejected garments are not matched against: the user said that crop is wrong."""
    earlier = [(g["garmentId"], g) for g in _garments(job_id)
               if g["localId"] != local_id and g["reviewStatus"] in ("pending", "confirmed")]
    if not earlier:
        return
    for g in new.values():
        match_id, score = best_same_garment(g, earlier)
        if match_id:
            g["reviewStatus"] = "duplicate"
            g["duplicateOf"] = match_id
            g["duplicateScore"] = round(score, 4)


# --- review -----------------------------------------------------------------------

def _garments(job_id: str) -> list[dict]:
    raw = redis_client().hgetall(f"{_job_key(job_id)}:garments")
    out = [{"garmentId": gid, **json.loads(v)} for gid, v in raw.items()]
    out.sort(key=lambda g: (_sort_key(g["localId"]), g["index"]))
    return out


def _sort_key(name: str):
    return (0, int(name), "") if name.isdigit() else (1, 0, name)


def _review_counts(garments: list[dict]) -> dict:
    counts = {"pending": 0, "confirmed": 0, "rejected": 0, "duplicate": 0}
    for g in garments:
        counts[g["reviewStatus"]] = counts.get(g["reviewStatus"], 0) + 1
    return counts


def list_garments(account_id: str, job_id: str) -> list[dict] | None:
    """Every garment extracted so far, embeddings included (callers strip them). Works
    while the job is still running, so the phone can start reviewing early."""
    if not _owned_job(account_id, job_id):
        return None
    return _garments(job_id)


def get_garments(account_id: str, job_id: str, garment_ids: list[str]) -> dict | None:
    """{garmentId: garment} for the ids that exist in this job; None if the job is not
    this account's (or has expired)."""
    job = _owned_job(account_id, job_id)
    if not job:
        return None
    raw = redis_client().hmget(f"{_job_key(job_id)}:garments", garment_ids) if garment_ids else []
    return {gid: {"garmentId": gid, **json.loads(v)} for gid, v in zip(garment_ids, raw) if v}


def set_review(job_id: str, garment_id: str, review_status: str, updates: dict | None = None):
    """updates: other fields to change with it (a confirm's corrected tags/description)."""
    key = f"{_job_key(job_id)}:garments"
    r = redis_client()
    raw = r.hget(key, garment_id)
    if not raw:
        return
    g = json.loads(raw)
    g["reviewStatus"] = review_status
    g.update(updates or {})
    pipe = r.pipeline()
    pipe.hset(key, garment_id, json.dumps(g))
    pipe.hset(_job_key(job_id), "updated_at", _now())
    _touch(pipe, _job_key(job_id), f"{_job_key(job_id)}:items", key)
    pipe.execute()
