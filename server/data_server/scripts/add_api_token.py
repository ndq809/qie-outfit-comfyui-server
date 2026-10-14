#!/usr/bin/env python3
"""Creates a bearer token for a new test account and adds it to API_TOKENS in
${WORKSPACE}/.env (data-server reads tokens from there - there is no database). Prints
the token and account id; restart data-server to pick it up. Stands in for the
login/session system the spec assumes exists upstream - test-only.

Usage: python3 -m server.data_server.scripts.add_api_token
"""
import os
import re
import secrets
import uuid
from pathlib import Path

if __name__ == "__main__":
    env = Path(os.environ.get("WORKSPACE", "/workspace")) / ".env"
    token, account_id = secrets.token_hex(24), str(uuid.uuid4())
    text = env.read_text() if env.exists() else ""
    m = re.search(r"^API_TOKENS=(.*)$", text, re.M)
    if m:
        pairs = ",".join(p for p in (m.group(1).strip(), f"{token}:{account_id}") if p)
        text = text[:m.start()] + f"API_TOKENS={pairs}" + text[m.end():]
    else:
        text = text.rstrip("\n") + f"\nAPI_TOKENS={token}:{account_id}\n"
    env.write_text(text)
    print(f"account_id: {account_id}")
    print(f"token:      {token}")
    print(f"added to API_TOKENS in {env}; run: supervisorctl restart data-server")
