"""Postgres access for data-server. data-server is the sole DB owner/writer
(wardrobe-system-spec.md §2.1) — ai-server never imports this module."""
import json
from contextlib import contextmanager

import psycopg2
import psycopg2.extras

from server.common.config import get_settings

psycopg2.extras.register_uuid()


@contextmanager
def get_conn():
    conn = psycopg2.connect(get_settings().postgres_dsn)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ping() -> bool:
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
        return True
    except Exception:
        return False


def lookup_token(token: str):
    """Returns (account_id, user_id) or None."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT account_id, user_id FROM api_tokens WHERE token = %s", (token,))
        row = cur.fetchone()
        return (str(row[0]), str(row[1])) if row else None


def set_account_face_ref(account_id: str, object_key: str):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("UPDATE accounts SET face_ref_key = %s WHERE id = %s", (object_key, account_id))


def get_account_face_ref(account_id: str) -> str | None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT face_ref_key FROM accounts WHERE id = %s", (account_id,))
        row = cur.fetchone()
        return row[0] if row else None


def create_upload_batch(account_id: str, user_id: str) -> str:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO upload_batches (account_id, user_id) VALUES (%s, %s) RETURNING id",
            (account_id, user_id),
        )
        return str(cur.fetchone()[0])


def add_batch_item(batch_id: str, local_id: str, object_key: str, content_type: str, checksum: str | None):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO upload_batch_items (batch_id, local_id, object_key, content_type, checksum)
               VALUES (%s, %s, %s, %s, %s)""",
            (batch_id, local_id, object_key, content_type, checksum),
        )


def get_batch_items(batch_id: str, local_ids: list[str]) -> list[dict]:
    with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT local_id, object_key FROM upload_batch_items WHERE batch_id = %s AND local_id = ANY(%s)",
            (batch_id, local_ids),
        )
        return cur.fetchall()


def create_job(account_id: str, user_id: str, batch_id: str, items: list[dict]) -> dict:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO jobs (account_id, user_id, batch_id, status, total_items)
               VALUES (%s, %s, %s, 'pending', %s) RETURNING id, status, total_items, created_at""",
            (account_id, user_id, batch_id, len(items)),
        )
        job_id, status, total_items, created_at = cur.fetchone()
        for it in items:
            cur.execute(
                """INSERT INTO job_items (job_id, local_id, object_key, status)
                   VALUES (%s, %s, %s, 'pending')""",
                (job_id, it["local_id"], it["object_key"]),
            )
        return {"jobId": str(job_id), "status": status, "totalItems": total_items, "createdAt": created_at.isoformat()}


def mark_job_processing_if_pending(job_id: str):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET status = 'processing', updated_at = now() WHERE id = %s AND status = 'pending'",
            (job_id,),
        )


def get_job(account_id: str, job_id: str):
    with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """SELECT id, status, total_items, processed_items, failed_items, updated_at
               FROM jobs WHERE id = %s AND account_id = %s""",
            (job_id, account_id),
        )
        job = cur.fetchone()
        if not job:
            return None
        cur.execute("SELECT local_id, status FROM job_items WHERE job_id = %s", (job_id,))
        items = cur.fetchall()
        return {
            "status": job["status"],
            "totalItems": job["total_items"],
            "processedItems": job["processed_items"],
            "failedItems": job["failed_items"],
            "updatedAt": job["updated_at"].isoformat(),
            "items": [{"localId": i["local_id"], "status": i["status"]} for i in items if i["status"] != "pending"],
        }


def cancel_job(account_id: str, job_id: str):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE jobs SET status = CASE WHEN status IN ('completed','cancelled','failed') THEN status
                                             ELSE 'cancelling' END,
                               updated_at = now()
               WHERE id = %s AND account_id = %s RETURNING status""",
            (job_id, account_id),
        )
        row = cur.fetchone()
        return row[0] if row else None


def job_item_by_local_id(job_id: str, local_id: str):
    with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT id, object_key, status FROM job_items WHERE job_id = %s AND local_id = %s",
            (job_id, local_id),
        )
        return cur.fetchone()


def finish_job_item(job_id: str, local_id: str, status: str, error_reason: str | None) -> dict:
    """Marks one job_item terminal, rolls up job counters, flips job status when
    every item is terminal. Returns the finished item's object_key (raw image,
    for the caller to delete) plus the job's account_id/user_id (for inserting
    wardrobe_items) and whether it just completed."""
    with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """UPDATE job_items SET status = %s, error_reason = %s, updated_at = now()
               WHERE job_id = %s AND local_id = %s AND status = 'pending'
               RETURNING id, object_key""",
            (status, error_reason, job_id, local_id),
        )
        item = cur.fetchone()
        if not item:
            return {}

        counter = "processed_items" if status == "success" else "failed_items"
        cur.execute(
            f"""UPDATE jobs SET {counter} = {counter} + 1, updated_at = now()
                WHERE id = %s
                RETURNING account_id, user_id, status, total_items, processed_items, failed_items""",
            (job_id,),
        )
        job = cur.fetchone()
        finished = (job["processed_items"] + job["failed_items"]) >= job["total_items"]
        if finished:
            new_status = "cancelled" if job["status"] == "cancelling" else "completed"
            cur.execute("UPDATE jobs SET status = %s, updated_at = now() WHERE id = %s", (new_status, job_id))

        return {
            "job_item_id": str(item["id"]),
            "object_key": item["object_key"],
            "account_id": str(job["account_id"]),
            "user_id": str(job["user_id"]),
            "job_finished": finished,
        }


def wardrobe_candidates_for_dedup(account_id: str, garment_type: str | None) -> list[tuple[str, list[float]]]:
    with get_conn() as conn, conn.cursor() as cur:
        if garment_type:
            cur.execute(
                """SELECT id, visual_embedding FROM wardrobe_items
                   WHERE account_id = %s AND duplicate_of IS NULL AND tags->>'type' = %s""",
                (account_id, garment_type),
            )
        else:
            cur.execute(
                "SELECT id, visual_embedding FROM wardrobe_items WHERE account_id = %s AND duplicate_of IS NULL",
                (account_id,),
            )
        return [(str(r[0]), r[1]) for r in cur.fetchall()]


def insert_wardrobe_item(account_id: str, user_id: str, job_id: str, job_item_id: str,
                          object_key: str, tags: dict, description: str,
                          visual_embedding: list[float], text_embedding: list[float],
                          duplicate_of: str | None) -> str:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO wardrobe_items
               (account_id, user_id, job_id, job_item_id, object_key, tags, description,
                visual_embedding, text_embedding, duplicate_of)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (account_id, user_id, job_id, job_item_id, object_key, json.dumps(tags), description,
             visual_embedding, text_embedding, duplicate_of),
        )
        return str(cur.fetchone()[0])


def list_wardrobe_items(account_id: str, job_id: str | None, cursor: str | None, limit: int):
    with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        clauses = ["account_id = %s", "duplicate_of IS NULL"]
        params: list = [account_id]
        if job_id:
            clauses.append("job_id = %s")
            params.append(job_id)
        if cursor:
            clauses.append("id > %s")
            params.append(cursor)
        where = " AND ".join(clauses)
        params.append(limit + 1)
        cur.execute(
            f"""SELECT id, object_key, tags, job_id FROM wardrobe_items
                WHERE {where} ORDER BY id ASC LIMIT %s""",
            params,
        )
        rows = cur.fetchall()
        next_cursor = None
        if len(rows) > limit:
            next_cursor = str(rows[limit - 1]["id"])
            rows = rows[:limit]
        return rows, next_cursor
