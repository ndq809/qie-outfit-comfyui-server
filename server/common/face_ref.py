"""The face embedding the phone sends with a job (POST /v1/jobs "faceReference").

The phone already embeds the user's face for its own scan (wardrobe-system-spec.md §1.1
A4) with the same model D0b matches with - InsightFace w600k_mbf (MobileFaceNet), the
recognition model of the buffalo_s pack in detect_clothing_by_face.py - so the two
embeddings live in one space and D0b can compare against the phone's directly, without
the face photo ever leaving the device. A vector from any other model would compare
as noise, hence the model name check.
"""
import math

FACE_EMBEDDING_MODEL = "w600k_mbf"
FACE_EMBEDDING_DIM = 512


def validate(embedding, model: str | None) -> tuple[list[float] | None, str | None]:
    """(L2-normalised embedding, None) when usable, else (None, why not). Normalised here
    because D0b compares it by dot product against normed face embeddings with a cosine
    threshold - an unnormalised vector would shift every similarity off that threshold."""
    if (model or "").strip().lower() != FACE_EMBEDDING_MODEL:
        return None, f"faceReference.model must be {FACE_EMBEDDING_MODEL!r}, got {model!r}"
    if not isinstance(embedding, (list, tuple)) or len(embedding) != FACE_EMBEDDING_DIM:
        n = len(embedding) if isinstance(embedding, (list, tuple)) else type(embedding).__name__
        return None, f"faceReference.embedding must have {FACE_EMBEDDING_DIM} numbers, got {n}"
    try:
        values = [float(v) for v in embedding]
    except (TypeError, ValueError):
        return None, "faceReference.embedding must contain numbers only"
    if not all(math.isfinite(v) for v in values):
        return None, "faceReference.embedding contains NaN or infinity"
    norm = math.sqrt(sum(v * v for v in values))
    if norm < 1e-6:
        return None, "faceReference.embedding is all zeros"
    return [round(v / norm, 6) for v in values], None
