"""Cosine similarity helper shared by both D2 dedup levels — same-image dedup
in ai_server/worker.py, cross-photo dedup within a job in data_server/jobs.py.
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


# "Same physical garment?" for D2, both levels. Visual similarity alone does not separate
# them: measured on 4 real jobs (169 garments, every cross-photo pair judged by eye),
# grey vs black trousers scored 0.951 and a navy vs a white tank top 0.948, while the
# same black trousers in two photos scored 0.944. textEmbedding (built from D3's tags,
# colour and type included) is what tells those apart, and averaging the two keeps the
# same bag seen at two angles (visual 0.879, text 0.924). The colours have to agree as
# well: two different floral shirts, tagged [beige, brown, white] and [black, white],
# reached a 0.882 mean. "Agree" is one colour shared between the first two of each: D3
# swaps the order of a print's colours between photos ([brown, beige] vs [beige, brown]
# on the same shirt, job 7664af2a), and any shared colour would match those two shirts.
# With these three conditions (jobs c78bd68a, a10ca1c9, cfcf11e3, cca32ca8): 26 pairs
# flagged, 24 the same piece by eye, 2 unclear (plain dark-grey vs black trousers; two
# blue shirts, one with two pockets). Seen and not flagged: the same uniform vest
# (visual 0.850, text 0.729) and a jacket D3 called "jackets" in one photo and "bombers"
# in the other - erring this way leaves a duplicate for the user to reject, the other
# way would hide a garment they own.
SAME_GARMENT_VISUAL_MIN = 0.85
SAME_GARMENT_MEAN_MIN = 0.87


def _garment_parts(g: dict):
    """(visual, text, first two colours) from a garment as ai-server's records name the fields
    (visual_embedding, color) or as data-server stores them (visualEmbedding, tags.color)."""
    if "visualEmbedding" in g:
        visual, text, colors = g["visualEmbedding"], g["textEmbedding"], (g.get("tags") or {}).get("color")
    else:
        visual, text, colors = g["visual_embedding"], g["text_embedding"], g.get("color")
    return visual, text, {c.get("color") for c in (colors or [])[:2]}


def same_garment_score(a: dict, b: dict) -> float | None:
    """The mean of visual and text similarity when a and b look like the same garment,
    None when they do not."""
    va, ta, ca = _garment_parts(a)
    vb, tb, cb = _garment_parts(b)
    if not ca & cb:
        return None
    visual = cosine_similarity(va, vb)
    if visual < SAME_GARMENT_VISUAL_MIN:
        return None
    mean = (visual + cosine_similarity(ta, tb)) / 2
    return mean if mean >= SAME_GARMENT_MEAN_MIN else None


def best_same_garment(g: dict, candidates: list[tuple[str, dict]]):
    """(id, score) of the candidate g most looks like the same garment as, or (None, 0.0)."""
    best_id, best = None, 0.0
    for cid, c in candidates:
        score = same_garment_score(g, c)
        if score is not None and score > best:
            best_id, best = cid, score
    return best_id, best
