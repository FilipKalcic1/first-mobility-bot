"""Lightweight vector utilities — no numpy dep.

Used by v2 layers that need cosine similarity over small/medium embedding
sets. For large-corpus FAISS-style queries, use services/faiss_vector_store.
"""
from __future__ import annotations

import math
from typing import Sequence


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity in [-1, 1]; returns 0.0 on bad input.

    Tolerates None / empty / mismatched-length inputs by returning 0
    instead of raising — callers (e.g. anchor matchers) treat 0 as
    "no signal" and proceed.
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0
