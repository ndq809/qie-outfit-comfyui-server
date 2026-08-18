#!/usr/bin/env python3
"""Persistent CLIP (ViT-B/32) classifier service used by test_extract_outfit.py to
detect headwear/footwear presence and one-piece-dress-vs-separates before running
the outfit-extraction pipeline.

Loads the model once at process startup and serves requests over a local-only HTTP
API, so repeated invocations of test_extract_outfit.py don't each pay the ~8s
process-startup + model-load cost. Runs on CPU only — never touches the GPU/VRAM
the main ComfyUI pipeline needs.
"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import torch
from PIL import Image
from transformers import CLIPModel, CLIPProcessor

MODEL_NAME = "openai/clip-vit-base-patch32"
HOST, PORT = "127.0.0.1", 18189

print("Loading CLIP classifier (CPU)...", flush=True)
torch.set_num_threads(max(1, torch.get_num_threads()))
_model = CLIPModel.from_pretrained(MODEL_NAME)
_processor = CLIPProcessor.from_pretrained(MODEL_NAME)
_model.eval()
print("CLIP classifier ready.", flush=True)


def _classify(img, pos, neg):
    inputs = _processor(text=[pos, neg], images=img, return_tensors="pt", padding=True)
    with torch.no_grad():
        out = _model(**inputs)
    probs = out.logits_per_image.softmax(dim=1)[0]
    return probs[0].item() > probs[1].item()


def detect_worn_items(image_path):
    img = Image.open(image_path).convert("RGB")
    w, h = img.size
    bottom_crop = img.crop((0, int(h * 0.75), w, h))
    headwear = _classify(img, "a person with a hat on their head", "a person with no hat, bare head")
    footwear = _classify(
        bottom_crop,
        "a close-up of feet wearing shoes or sandals on the ground",
        "a close-up of the ground, ocean, or fabric with no feet or shoes",
    )
    one_piece = _classify(
        img,
        "a one-piece dress with no separation at the waist",
        "a separate top and bottom, two different garments worn together",
    )
    bag = _classify(
        img,
        "a person with a shoulder bag or handbag",
        "a person with nothing extra, just clothing",
    )
    return {"headwear": headwear, "footwear": footwear, "one_piece": one_piece, "bag": bag}


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
            return self._json(200, {"status": "ok"})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/detect":
            return self._json(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length))
            result = detect_worn_items(body["image_path"])
            self._json(200, result)
        except Exception as e:
            self._json(500, {"error": str(e)})

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"CLIP classifier service listening on http://{HOST}:{PORT}", flush=True)
    server.serve_forever()
