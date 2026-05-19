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


# ---- Faza 4 Bug #5 (Filip 2026-05-17): keyword fallback + circuit ----


class _RaisingEmbedder:
    """Always raises — simulates persistent embedder outage."""

    def __init__(self, exc=None):
        self._exc = exc or RuntimeError("embedder down")
        self.calls = 0

    async def embed(self, text: str):
        self.calls += 1
        raise self._exc


class _IntermittentEmbedder:
    """Raises N times, then succeeds."""

    def __init__(self, fail_first=3):
        self.fail_first = fail_first
        self.calls = 0

    async def embed(self, text: str):
        self.calls += 1
        if self.calls <= self.fail_first:
            raise RuntimeError("transient")
        # Return a fixed vector that won't trigger semantic match —
        # caller will fall through to keyword fallback if pattern matches.
        return [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]


@pytest.mark.asyncio
async def test_keyword_fallback_matches_kolika_km():
    """When embedder fails, regex pattern 'kolika ... km' must match."""
    a = DriverBasicsAnchor(_RaisingEmbedder())
    await a.initialize()  # init also raises but is wrapped — anchors stay empty
    # Anchor vectors empty → keyword_fallback runs
    r = await a.match("kolika mi je km")
    assert r.matched is True
    assert "keyword_fallback" in r.reasoning


@pytest.mark.asyncio
async def test_keyword_fallback_matches_registration():
    a = DriverBasicsAnchor(_RaisingEmbedder())
    await a.initialize()
    r = await a.match("koja mi je registracija auta")
    assert r.matched is True


@pytest.mark.asyncio
async def test_keyword_fallback_does_not_false_positive():
    """Random text without driver-basics keywords stays unmatched even
    in keyword-fallback path."""
    a = DriverBasicsAnchor(_RaisingEmbedder())
    await a.initialize()
    r = await a.match("kasnim na sastanak")
    assert r.matched is False


# ----------------------------------------------------------------------
# Faza 2026-05-19 (Filip bench finding): 1-word queries
# ----------------------------------------------------------------------

@pytest.mark.parametrize("query", [
    "tablica", "registracija", "rega",
    "marka", "model", "godina",
    "km", "kilometraža", "kilometara",
    "lizing", "leasing",
    "vin", "pin",
    "casco", "casco istječe",
    "lizing kuća", "leasing kuca",
])
@pytest.mark.asyncio
async def test_standalone_one_word_driver_queries_match(query):
    """1-word driver queries (Filip 2026-05-19 bench finding: 'tablica'
    was lost to get_Metadata in anchor cosine — these now short-circuit
    via L2b keyword fallback)."""
    a = DriverBasicsAnchor(_RaisingEmbedder())
    await a.initialize()
    r = await a.match(query)
    assert r.matched is True, f"expected match for {query!r}, got {r.reasoning}"


@pytest.mark.parametrize("query", [
    "tablica vozila marka",   # 3+ words → not a 1-word match
    "modelirati raspored",     # contains "model" but full word elsewhere
    "registriraj se",          # "registri" stem but verb form
])
@pytest.mark.asyncio
async def test_1word_patterns_do_not_overmatch(query):
    """Standalone 1-word patterns must NOT match multi-word context
    where the keyword is part of unrelated phrase. Bench false-positive
    guard."""
    a = DriverBasicsAnchor(_RaisingEmbedder())
    await a.initialize()
    r = await a.match(query)
    # These should NOT match standalone — but multi-word patterns may catch them.
    # The assertion is just that the standalone-1-word patterns don't trigger.
    if r.matched:
        # Acceptable only if matched by multi-word pattern, not by the new 1-word patterns
        assert "tablica|registracija|rega" not in r.reasoning
        assert "marka|model|godina" not in r.reasoning


@pytest.mark.asyncio
async def test_circuit_opens_after_three_embedder_exceptions():
    """3 consecutive embedder exceptions → circuit opens, subsequent
    calls skip embedding entirely."""
    emb = _RaisingEmbedder()
    a = DriverBasicsAnchor(emb)
    # Bypass initialize (avoid using up call count) by setting up vecs manually
    a._initialized = True
    a._positive_vecs = [[1.0, 0, 0, 0, 0, 0, 0, 0]]
    a._negative_vecs = [[0.0, 1.0, 0, 0, 0, 0, 0, 0]]

    # Three failures with non-matching queries → keyword_no_match + circuit OPEN
    for _ in range(3):
        await a.match("nešto što nije anchor")
    # Circuit OPEN: 4th call must not invoke embedder
    calls_before = emb.calls
    r = await a.match("nešto novo")
    assert emb.calls == calls_before  # NO new embedder calls
    assert r.matched is False


@pytest.mark.asyncio
async def test_circuit_recovery_after_timeout():
    """After OPEN window expires, embedder is tried again."""
    import time as _time_mod
    emb = _RaisingEmbedder()
    a = DriverBasicsAnchor(emb)
    a._initialized = True
    a._positive_vecs = [[1.0, 0, 0, 0, 0, 0, 0, 0]]
    a._negative_vecs = [[0.0, 1.0, 0, 0, 0, 0, 0, 0]]

    # Open the circuit
    for _ in range(3):
        await a.match("query")
    assert a._circuit_open_until > _time_mod.time()  # circuit is open

    # Simulate time passing — fast-forward by zeroing the timer
    a._circuit_open_until = _time_mod.time() - 1
    calls_before = emb.calls
    await a.match("retry after recovery")
    assert emb.calls == calls_before + 1  # embedder was attempted again


@pytest.mark.asyncio
async def test_embedder_success_resets_fail_count():
    """If embedder recovers BEFORE circuit opens, fail count resets."""
    emb = _IntermittentEmbedder(fail_first=2)  # only 2 fails, then success
    a = DriverBasicsAnchor(emb)
    a._initialized = True
    a._positive_vecs = [[1.0, 0, 0, 0, 0, 0, 0, 0]]
    a._negative_vecs = [[0.0, 1.0, 0, 0, 0, 0, 0, 0]]

    # 2 fails → fail_count=2
    await a.match("q1")
    await a.match("q2")
    assert a._fail_count == 2

    # 3rd embed succeeds → reset
    await a.match("q3")
    assert a._fail_count == 0
    # Circuit NOT open (we never hit threshold)
    import time as _time_mod
    assert a._circuit_open_until <= _time_mod.time()


@pytest.mark.asyncio
async def test_initialize_swallows_embedder_exceptions():
    """If embedder raises during initialize, anchors stay empty but
    no propagation crashes the engine boot."""
    a = DriverBasicsAnchor(_RaisingEmbedder())
    await a.initialize()  # must NOT raise
    assert a._initialized is True
    assert a._positive_vecs == []
    assert a._negative_vecs == []
