#!/usr/bin/env python3
"""Creates one test account + user + bearer token, prints the token.
Stands in for the login/session system the spec assumes exists elsewhere —
this is a test-only shortcut (see plan's "Open items" section).

Usage: python3 -m server.data_server.scripts.seed_test_account [account name]
"""
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from server.data_server.db import get_conn

if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else "test-account"
    token = secrets.token_hex(24)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO accounts (name) VALUES (%s) RETURNING id", (name,))
        account_id = cur.fetchone()[0]
        cur.execute("INSERT INTO users (account_id) VALUES (%s) RETURNING id", (account_id,))
        user_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO api_tokens (token, account_id, user_id) VALUES (%s, %s, %s)",
            (token, account_id, user_id),
        )
    print(f"account_id: {account_id}")
    print(f"user_id:    {user_id}")
    print(f"token:      {token}")
