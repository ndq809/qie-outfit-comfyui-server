#!/usr/bin/env python3
"""Drives the public data-server API exactly as the mobile app does (MOBILE_CLIENT_API.md):
presign -> PUT to object-storage (4 in parallel) -> create job -> poll -> wardrobe items.

Goes through the public vast.ai edge, so it also proves the two-token scheme works from
outside. Reads BASE/tokens from the instance environment unless given explicitly.

Usage: python scripts/mobile_flow_test.py [--base URL] [--storage-host HOST:PORT] <image dir or files>
"""
import argparse
import json
import mimetypes
import os
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def env_file(key):
    path = Path(os.environ.get("WORKSPACE", "/workspace")) / ".env"
    for line in path.read_text().splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    return ""


def local_id(path: Path) -> str:
    m = re.search(r"IMG_(\d+)", path.name)
    return m.group(1) if m else path.stem


class Client:
    def __init__(self, base, edge, app, cookie_name):
        self.base, self.edge, self.app = base.rstrip("/"), edge, app
        self.cookie = f"{cookie_name}={edge}"
        self.storage_host = None

    def call(self, method, path, body=None):
        sep = "&" if "?" in path else "?"
        req = urllib.request.Request(
            f"{self.base}{path}{sep}token={self.edge}", method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {self.app}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())

    def put(self, url, path: Path, content_type):
        if self.storage_host:
            url = re.sub(r"^http://[^/]+", f"http://{self.storage_host}", url)
        req = urllib.request.Request(url, method="PUT", data=path.read_bytes(),
                                     headers={"Content-Type": content_type, "Cookie": self.cookie})
        with urllib.request.urlopen(req, timeout=300) as r:
            return r.status


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("--base", default=f"http://{os.environ.get('PUBLIC_IPADDR')}:"
                                      f"{os.environ.get('VAST_TCP_PORT_10100')}")
    # Uploading to this box's own public IP from inside it hairpins through NAT at ~5 KB/s.
    # Point the PUTs at the same Caddy edge locally instead; signature and cookie are unchanged.
    ap.add_argument("--storage-host", help="e.g. localhost:10200 when running on the server itself")
    ap.add_argument("--poll", type=int, default=10)
    ap.add_argument("--out", help="write the final job + wardrobe JSON here")
    args = ap.parse_args()

    files = []
    for inp in args.inputs:
        p = Path(inp)
        files += sorted(x for x in p.iterdir() if x.suffix.lower() in (".jpg", ".jpeg", ".png")) \
            if p.is_dir() else [p]

    c = Client(args.base, os.environ["OPEN_BUTTON_TOKEN"], env_file("TEST_BEARER_TOKEN"),
               f"{os.environ.get('VAST_CONTAINERLABEL', 'C.' + os.environ.get('CONTAINER_ID', ''))}_auth_token")

    c.storage_host = args.storage_host
    print("health        ", c.call("GET", "/v1/health"))
    print("face-reference", c.call("GET", "/v1/face-reference"))

    by_id = {local_id(f): f for f in files}
    ctype = {i: (mimetypes.guess_type(f.name)[0] or "image/jpeg") for i, f in by_id.items()}
    pre = c.call("POST", "/v1/uploads/presign",
                 {"items": [{"localId": i, "contentType": ctype[i]} for i in by_id]})
    print(f"presign        batch {pre['batchId']}, {len(pre['items'])} url(s)")

    def upload(it):
        try:
            return it["localId"], c.put(it["uploadUrl"], by_id[it["localId"]], ctype[it["localId"]])
        except urllib.error.HTTPError as e:
            return it["localId"], f"{e.code} {e.read()[:200]!r}"

    t0 = time.time()
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(upload, pre["items"]))
    ok = [i for i, s in results if s == 200]
    for i, s in results:
        print(f"  PUT {i:<10} {s}")
    print(f"upload         {len(ok)}/{len(results)} ok in {time.time() - t0:.1f}s")
    if not ok:
        sys.exit("nothing uploaded")

    job = c.call("POST", "/v1/jobs", {"batchId": pre["batchId"], "uploadedItems": ok})
    jid = job["jobId"]
    print("job           ", job)

    t0 = time.time()
    while True:
        st = c.call("GET", f"/v1/jobs/{jid}")
        print(f"  [{time.time() - t0:5.0f}s] {st['status']:<10} "
              f"processed {st['processedItems']}/{st['totalItems']} failed {st['failedItems']}", flush=True)
        if st["status"] in ("completed", "failed", "cancelled"):
            break
        time.sleep(args.poll)

    wardrobe = c.call("GET", f"/v1/wardrobe/items?jobId={jid}&limit=200")
    print(f"wardrobe       {len(wardrobe['items'])} item(s)")
    for it in wardrobe["items"]:
        t = it["tags"]
        colors = ", ".join(f"{x['color']} {x['confidence']:.0f}%" for x in t.get("color", []))
        print(f"  {t.get('type'):<28} {t.get('gender'):<6} {colors}")
    if wardrobe["items"]:
        code = urllib.request.urlopen(urllib.request.Request(
            re.sub(r"^http://[^/]+", f"http://{c.storage_host}", wardrobe["items"][0]["imageUrl"])
            if c.storage_host else wardrobe["items"][0]["imageUrl"], headers={"Cookie": c.cookie})).status
        print(f"imageUrl GET   {code}")
    if args.out:
        Path(args.out).write_text(json.dumps({"job": st, "wardrobe": wardrobe}, indent=1))


if __name__ == "__main__":
    main()
