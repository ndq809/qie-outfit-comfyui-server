"""Postgres access for data-server. data-server is the sole DB owner/writer
(wardrobe-system-spec.md §2.1) — ai-server never imports this module.

Holds accounts/auth, the face reference, and the wardrobe the user confirmed. Nothing
from the processing flow (batches, jobs, unreviewed results) is written here — see
server/data_server/jobs.py."""
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


def get_face_ref_key(account_id: str) -> str | None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT face_ref_key FROM accounts WHERE id = %s", (account_id,))
        row = cur.fetchone()
        return row[0] if row else None


def set_face_ref_key(account_id: str, key: str):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("UPDATE accounts SET face_ref_key = %s WHERE id = %s", (key, account_id))


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


def find_by_source_garment(account_id: str, garment_id: str):
    with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """SELECT id, object_key, tags, job_id FROM wardrobe_items
               WHERE account_id = %s AND source_garment_id = %s""",
            (account_id, garment_id),
        )
        return cur.fetchone()


def insert_wardrobe_item(item_id: str, account_id: str, user_id: str, job_id: str,
                         source_local_id: str, source_garment_id: str, object_key: str,
                         tags: dict, ai_tags: dict, tags_edited: bool, description: str,
                         visual_embedding: list[float], text_embedding: list[float]) -> bool:
    """False if this garment was already confirmed (a retried request)."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO wardrobe_items
               (id, account_id, user_id, job_id, source_local_id, source_garment_id, object_key,
                tags, ai_tags, tags_edited, description, visual_embedding, text_embedding)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (source_garment_id) WHERE source_garment_id IS NOT NULL DO NOTHING
               RETURNING id""",
            (item_id, account_id, user_id, job_id, source_local_id, source_garment_id, object_key,
             json.dumps(tags), json.dumps(ai_tags), tags_edited, description,
             visual_embedding, text_embedding),
        )
        return cur.fetchone() is not None


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
