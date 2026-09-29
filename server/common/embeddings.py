"""Cosine similarity helper shared by both D2 dedup levels — same-image dedup
in ai_server/worker.py, cross-item dedup in data_server/result_consumer.py.
Embeddings are already L2-normalized by the D3 classifier (magic_eye), so this
is a plain dot product, per wardrobe-system-spec.md §2.1 (D2).

numpy rather than a Python loop: the wardrobe-level pass compares one garment
against every stored item of the account, and element-wise Python over
~1000-d vectors made that O(items x dim) interpreter work inside data-server.
"""
from typing import Sequence

import numpy as np


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return float(np.dot(np.asarray(a, dtype=np.float32), np.asarray(b, dtype=np.float32)))


def most_similar(query: Sequence[float], candidates: list[tuple[str, Sequence[float]]]):
    """candidates: list of (id, embedding). Returns (id, score) for the best
    match, or (None, 0.0) if candidates is empty."""
    if not query:
        return None, 0.0
    candidates = [(cid, emb) for cid, emb in candidates if emb and len(emb) == len(query)]
    if not candidates:
        return None, 0.0
    matrix = np.asarray([emb for _, emb in candidates], dtype=np.float32)
    scores = matrix @ np.asarray(query, dtype=np.float32)
    best = int(scores.argmax())
    if scores[best] <= 0.0:
        return None, 0.0
    return candidates[best][0], float(scores[best])
