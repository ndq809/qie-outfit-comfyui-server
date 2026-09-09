"""Cosine similarity helper shared by both D2 dedup levels — same-image dedup
in ai_server/worker.py, cross-item dedup in data_server/result_consumer.py.
Embeddings are already L2-normalized by the D3 classifier (magic_eye), so this
is a plain dot product, per wardrobe-system-spec.md §2.1 (D2).
"""
from typing import Sequence


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b))


def most_similar(query: Sequence[float], candidates: list[tuple[str, Sequence[float]]]):
    """candidates: list of (id, embedding). Returns (id, score) for the best
    match, or (None, 0.0) if candidates is empty."""
    best_id, best_score = None, 0.0
    for cid, emb in candidates:
        score = cosine_similarity(query, emb)
        if score > best_score:
            best_id, best_score = cid, score
    return best_id, best_score
