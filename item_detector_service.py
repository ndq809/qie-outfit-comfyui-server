#!/usr/bin/env python3
"""Persistent worn-item detector service used by test_extract_outfit.py to decide
which garment/accessory categories are actually present before running the
outfit-extraction pipeline.

Replaces the previous CLIP classifier service: the detection stack itself now
lives in outfit_items.py (fashion-object-detection DETR + person-crop/multi-scale
TTA + SAM person isolation + optional ArcFace selfie matching, i.e. what
detect_clothing_by_face.py uses). This file only keeps those models loaded once
and serves them over a local-only HTTP API, so repeated invocations of
test_extract_outfit.py don't each pay the model-load cost.

Follows detect_clothing_by_face.py in using the GPU when there is one, which
means this service holds VRAM for as long as it runs. On a box where ComfyUI is
already paging a ~30GB fp8 unet + text encoder through a smaller card, start it
with OUTFIT_ITEMS_DEVICE=cpu so the two don't fight over VRAM.
"""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import outfit_items

HOST, PORT = "127.0.0.1", 18189

# Models are loaded on the first /detect, not at startup, and dropped again after
# MODEL_IDLE_UNLOAD_SECONDS without a request, so an idle box holds no VRAM for them.
# (A warm detector on every instance was part of what made SSH crawl on other boxes.)
# ITEM_DETECTOR_WARMUP=true restores the old load-at-startup behaviour.
IDLE_UNLOAD_SECONDS = float(os.environ.get("MODEL_IDLE_UNLOAD_SECONDS", "60"))
_model_lock = threading.Lock()
_last_used = time.monotonic()

if os.environ.get("ITEM_DETECTOR_WARMUP", "").lower() in ("1", "true", "yes"):
    print(f"Loading worn-item detector (device={outfit_items.DEVICE})...", flush=True)
    outfit_items.warm_up()
    print("Worn-item detector ready.", flush=True)
else:
    print(f"Worn-item detector: lazy load on first request (device={outfit_items.DEVICE}, "
          f"unload after {IDLE_UNLOAD_SECONDS:.0f}s idle)", flush=True)


def _idle_unloader():
    while True:
        time.sleep(5)
        if IDLE_UNLOAD_SECONDS <= 0 or not outfit_items.models_loaded():
            continue
        if time.monotonic() - _last_used < IDLE_UNLOAD_SECONDS:
            continue
        with _model_lock:
            if outfit_items.models_loaded() and time.monotonic() - _last_used >= IDLE_UNLOAD_SECONDS:
                outfit_items.unload_models()
                print("Worn-item detector idle: models unloaded from VRAM.", flush=True)


class Handler(BaseHTTPRequestHandler):
    def _json(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            return self._json(200, {"status": "ok", "device": outfit_items.DEVICE,
                                    "loaded": outfit_items.models_loaded()})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/detect":
            return self._json(404, {"error": "not found"})
        global _last_used
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length))
            with _model_lock:
                try:
                    result = outfit_items.detect_worn_items(
                        body["image_path"],
                        selfie_path=body.get("selfie_path"),
                        threshold=body.get("threshold"),
                        face_match_threshold=body.get("face_match_threshold"),
                        save_isolated_to=body.get("save_isolated_to"),
                    )
                finally:
                    _last_used = time.monotonic()
            self._json(200, result)
        except Exception as e:
            self._json(500, {"error": str(e)})

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    # Requests are serialised anyway (single set of torch models, no thread
    # safety guarantees), but ThreadingHTTPServer keeps a slow request from
    # blocking /health.
    threading.Thread(target=_idle_unloader, daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Worn-item detector service listening on http://{HOST}:{PORT}", flush=True)
    server.serve_forever()
