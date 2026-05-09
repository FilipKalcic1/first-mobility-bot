"""Tests for L2b DriverBasicsAnchor."""
from __future__ import annotations

import hashlib
from typing import Optional

import pytest

from services.v2.driver_basics import (
    DRIVER_BASICS_ANCHORS, NEGATIVE_ANCHORS,
    MIN_GAP_TO_NOISE, STRONG_THRESHOLD,
    BasicsMatch, DriverBasicsAnchor,
)


class FakeEmbedder:
    def __init__(self):
        self.overrides: dict[str, list[float]] = {}
        self.fail = False

    def set(self, text: str, vec: list[float]):
        self.overrides[text] = vec

    async def embed(self, text: str) -> Optional[list[float]]:
        if self.fail:
            return None
        if text in self.overrides:
            return self.overrides[text]
        h = hashlib.md5(text.encode("utf-8")).digest()
        return [b / 255.0 for b in h[:8]]


@pytest.mark.asyncio
async def test_initialize_embeds_all_anchors():
    emb = FakeEmbedder()
    a = DriverBasicsAnchor(emb)
    await a.initialize()
    assert len(a._positive_vecs) == len(DRIVER_BASICS_ANCHORS)
    assert len(a._negative_vecs) == len(NEGATIVE_ANCHORS)


@pytest.mark.asyncio
async def test_match_returns_unmatched_before_init():
    a = DriverBasicsAnchor(FakeEmbedder())
    r = await a.match("anything")
    assert r.matched is False


@pytest.mark.asyncio
async def test_strong_positive_match():
    emb = FakeEmbedder()
    # Pin one positive anchor; orthogonal negatives
    pos = DRIVER_BASICS_ANCHORS[0]
    target_vec = [1.0, 0, 0, 0, 0, 0, 0, 0]
    emb.set(pos, target_vec)
    for s in DRIVER_BASICS_ANCHORS[1:]:
        emb.set(s, [0.0, 0.5, 0, 0, 0, 0, 0, 0])
    for s in NEGATIVE_ANCHORS:
        emb.set(s, [0.0, 0, 1.0, 0, 0, 0, 0, 0])

    a = DriverBasicsAnchor(emb)
    await a.initialize()
    emb.set("Q", target_vec)
    r = await a.match("Q")
    assert r.matched is True
    assert r.score >= STRONG_THRESHOLD
    assert r.gap >= MIN_GAP_TO_NOISE


@pytest.mark.asyncio
async def test_negative_anchor_suppresses_match():
    """If query is closer to negative (complaint) than positive
    (question), no match — kanibalizacija prevented."""
    emb = FakeEmbedder()
    # Both positive and negative have similar vectors to query, but
    # negative wins → small gap → no match
    for s in DRIVER_BASICS_ANCHORS:
        emb.set(s, [0.5, 0, 0, 0, 0, 0, 0, 0])
    for s in NEGATIVE_ANCHORS:
        emb.set(s, [0.95, 0, 0, 0, 0, 0, 0, 0])
    a = DriverBasicsAnchor(emb)
    await a.initialize()

    emb.set("auto mi je smece", [1.0, 0, 0, 0, 0, 0, 0, 0])
    r = await a.match("auto mi je smece")
    # pos_top ≈ 0.5 / sqrt(0.25) ≈ 1.0 ... actually 0.5 normalized
    # Let's just check that gap is too small to qualify
    assert r.matched is False


@pytest.mark.asyncio
async def test_below_threshold_returns_unmatched():
    emb = FakeEmbedder()
    for s in DRIVER_BASICS_ANCHORS:
        emb.set(s, [1.0, 0, 0, 0, 0, 0, 0, 0])
    for s in NEGATIVE_ANCHORS:
        emb.set(s, [1.0, 0, 0, 0, 0, 0, 0, 0])
    a = DriverBasicsAnchor(emb)
    await a.initialize()

    emb.set("orthogonal", [0.0, 1.0, 0, 0, 0, 0, 0, 0])
    r = await a.match("orthogonal")
    assert r.matched is False


@pytest.mark.asyncio
async def test_empty_query_unmatched():
    a = DriverBasicsAnchor(FakeEmbedder())
    await a.initialize()
    r = await a.match("")
    assert r.matched is False


@pytest.mark.asyncio
async def test_embed_failure_unmatched():
    emb = FakeEmbedder()
    a = DriverBasicsAnchor(emb)
    await a.initialize()
    emb.fail = True
    r = await a.match("anything")
    assert r.matched is False


def test_thresholds_documented():
    assert 0.7 <= STRONG_THRESHOLD <= 0.9
    # Tightened after real Azure measurement: ada-002 baseline cosine on
    # Croatian sits ~0.8, leaving narrow gap room. 0.02 catches 90%+ of
    # real driver queries while keeping >85% specificity.
    assert 0.01 <= MIN_GAP_TO_NOISE <= 0.2
