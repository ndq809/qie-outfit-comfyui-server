#!/usr/bin/env python3
"""Applies schema.sql and creates the MinIO buckets. Run once after postgres and
MinIO are up; safe to re-run (everything is CREATE ... IF NOT EXISTS)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import psycopg2

from server.common.config import get_settings
from server.common.storage import ensure_buckets

if __name__ == "__main__":
    settings = get_settings()
    schema_path = Path(__file__).resolve().parent.parent / "schema.sql"
    conn = psycopg2.connect(settings.postgres_dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(schema_path.read_text())
        conn.commit()
        print(f"schema applied from {schema_path}")
    finally:
        conn.close()

    ensure_buckets()
    print(f"buckets ready: {settings.minio_raw_bucket}, {settings.minio_items_bucket}")
