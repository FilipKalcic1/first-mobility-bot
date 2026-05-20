"""Unit tests for AnchorIndex."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from services.router.anchor_index import AnchorIndex


def _hashed_embed(texts: list[str]) -> list[list[float]]:
    """Deterministic 8-dim 'embedding' for predictable test ordering."""
    out = []
    for t in texts:
        h = hashlib.md5(t.encode("utf-8")).digest()
        out.append([b / 255.0 for b in h[:8]])
    return out


async def _embed_fn(texts):
    return _hashed_embed(texts)


@pytest.mark.asyncio
async def test_build_loads_anchors_and_embeds(tmp_path):
    anchors = {
        "get_X": ["phrase one", "phrase two"],
        "get_Y": ["different thing", "another different"],
    }
    p = tmp_path / "anchors.json"
    p.write_text(json.dumps(anchors), encoding="utf-8")

    idx = AnchorIndex(
        anchors_path=p,
        cache_path=tmp_path / "cache.json",
        embedding_deployment="test-deployment",
    )
    await idx.build(_embed_fn)
    assert len(idx._vectors_by_tool) == 2
    assert "get_X" in idx._vectors_by_tool
    assert "get_Y" in idx._vectors_by_tool


@pytest.mark.asyncio
async def test_top_k_returns_sorted_by_cosine(tmp_path):
    # We pin the embeddings so we know the cosine ordering.
    anchors = {
        "get_close": ["aaaa"],
        "get_far": ["bbbb"],
    }
    p = tmp_path / "anchors.json"
    p.write_text(json.dumps(anchors), encoding="utf-8")

    idx = AnchorIndex(anchors_path=p, cache_path=None)
    await idx.build(_embed_fn)

    # Query exactly matches "aaaa" → cosine=1.0 with get_close, less with get_far
    results = await idx.top_k("aaaa", _embed_fn, k=2)
    assert len(results) == 2
    assert results[0][0] == "get_close"
    assert results[0][1] == pytest.approx(1.0, abs=1e-6)
    # get_close score should beat get_far
    assert results[0][1] > results[1][1]


@pytest.mark.asyncio
async def test_top_k_respects_k_limit(tmp_path):
    anchors = {f"get_{i}": [f"phrase {i}"] for i in range(20)}
    p = tmp_path / "anchors.json"
    p.write_text(json.dumps(anchors), encoding="utf-8")

    idx = AnchorIndex(anchors_path=p, cache_path=None)
    await idx.build(_embed_fn)

    assert len(await idx.top_k("query", _embed_fn, k=5)) == 5
    assert len(await idx.top_k("query", _embed_fn, k=100)) == 20  # capped at total


@pytest.mark.asyncio
async def test_top_k_max_over_phrases(tmp_path):
    """Tool's score = MAX cosine across its phrases (not average)."""
    anchors = {
        "get_with_match": ["unrelated", "exact match here"],
        "get_uniform_low": ["a", "b"],
    }
    p = tmp_path / "anchors.json"
    p.write_text(json.dumps(anchors), encoding="utf-8")

    idx = AnchorIndex(anchors_path=p, cache_path=None)
    await idx.build(_embed_fn)

    results = await idx.top_k("exact match here", _embed_fn, k=2)
    assert results[0][0] == "get_with_match"  # best matched on second phrase
    assert results[0][1] == pytest.approx(1.0, abs=1e-6)


@pytest.mark.asyncio
async def test_empty_query_returns_empty(tmp_path):
    anchors = {"get_X": ["phrase"]}
    p = tmp_path / "anchors.json"
    p.write_text(json.dumps(anchors), encoding="utf-8")

    idx = AnchorIndex(anchors_path=p, cache_path=None)
    await idx.build(_embed_fn)

    assert await idx.top_k("", _embed_fn) == []
    assert await idx.top_k("   ", _embed_fn) == []


@pytest.mark.asyncio
async def test_cache_round_trip(tmp_path):
    anchors = {"get_X": ["phrase one", "phrase two"]}
    p = tmp_path / "anchors.json"
    p.write_text(json.dumps(anchors), encoding="utf-8")

    cache_path = tmp_path / "cache.json"
    idx1 = AnchorIndex(
        anchors_path=p, cache_path=cache_path, embedding_deployment="X",
    )
    await idx1.build(_embed_fn)
    assert cache_path.exists()

    # New instance with same data — should load from cache without re-embedding
    call_log = []
    async def counting_embed(texts):
        call_log.append(len(texts))
        return _hashed_embed(texts)

    idx2 = AnchorIndex(
        anchors_path=p, cache_path=cache_path, embedding_deployment="X",
    )
    await idx2.build(counting_embed)
    # Build called embed for query embeddings only (none yet) — should be 0
    assert call_log == []
    # And cosine still works
    results = await idx2.top_k("phrase one", counting_embed, k=1)
    assert results[0][0] == "get_X"


@pytest.mark.asyncio
async def test_cache_invalidated_when_anchors_change(tmp_path):
    p = tmp_path / "anchors.json"
    p.write_text(json.dumps({"get_X": ["original phrase"]}), encoding="utf-8")
    cache_path = tmp_path / "cache.json"

    idx1 = AnchorIndex(anchors_path=p, cache_path=cache_path, embedding_deployment="X")
    await idx1.build(_embed_fn)

    # Change content — cache must rebuild
    p.write_text(json.dumps({"get_X": ["different phrase"]}), encoding="utf-8")

    rebuilt_embed_calls = []
    async def counting_embed(texts):
        rebuilt_embed_calls.append(len(texts))
        return _hashed_embed(texts)

    idx2 = AnchorIndex(anchors_path=p, cache_path=cache_path, embedding_deployment="X")
    await idx2.build(counting_embed)
    # Cache stale → rebuild → embed was called
    assert sum(rebuilt_embed_calls) >= 1


@pytest.mark.asyncio
async def test_top_k_before_build_raises(tmp_path):
    p = tmp_path / "anchors.json"
    p.write_text(json.dumps({"get_X": ["phrase"]}), encoding="utf-8")

    idx = AnchorIndex(anchors_path=p, cache_path=None)
    with pytest.raises(RuntimeError, match="not built"):
        await idx.top_k("query", _embed_fn)
