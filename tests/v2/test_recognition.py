"""Tests for L3 RecognitionEngine.

Two test layers:
  - in-memory: tiny fake registry + pinned vectors, exercises every
    branch of recognize() deterministically
  - real registry: uses ToolRegistry.from_file() for the 950-tool corpus,
    pins one anchor's vector to verify init + top-K work at scale

LLM Judge is always stubbed — recognition logic is independent of any
specific Azure deployment.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pytest

from services.v2.recognition import (
    MIN_RATIONALE_LEN, RecognitionEngine, RecognitionResult,
)
from services.v2.registry import ToolRegistry


REGISTRY_PATH = (
    Path(__file__).resolve().parents[2]
    / "config" / "processed_tool_registry.json"
)


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class FakeEmbedder:
    """Hashed deterministic embedder. Tests pin specific text → vector
    pairs so cosine ordering is predictable."""

    def __init__(self):
        self.overrides: dict[str, list[float]] = {}
        self.fail_on: set[str] = set()
        self.return_none = False

    def set(self, text: str, vec: list[float]):
        self.overrides[text] = vec

    async def embed(self, text):
        if self.return_none:
            return None
        if text in self.fail_on:
            return None
        if text in self.overrides:
            return self.overrides[text]
        h = hashlib.md5(text.encode("utf-8")).digest()
        return [b / 255.0 for b in h[:8]]


@dataclass
class _Choice:
    message: object


@dataclass
class _Msg:
    content: str


@dataclass
class _Response:
    choices: list


class _FakeCompletions:
    def __init__(self):
        self.queue: list = []
        self.last_kwargs: Optional[dict] = None

    def push(self, item):
        self.queue.append(item)

    async def create(self, **kwargs):
        self.last_kwargs = kwargs
        if not self.queue:
            return _Response(choices=[_Choice(message=_Msg(content="{}"))])
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return _Response(choices=[_Choice(message=_Msg(content=item))])


class _FakeChat:
    def __init__(self, completions):
        self.completions = completions


class FakeLLM:
    def __init__(self):
        self.completions = _FakeCompletions()
        self.chat = _FakeChat(self.completions)


def _make_registry(tools_meta) -> ToolRegistry:
    """Build a small in-memory registry from compact tuples
    (operation_id, method, embedding_text). Action markers disabled
    so tests can pin exact embedding strings without dealing with
    Croatian prefix injection."""
    tools = [
        {
            "operation_id":   tid,
            "method":         method,
            "service_name":   "automation",
            "path":           f"/{tid}",
            "embedding_text": text,
        }
        for tid, method, text in tools_meta
    ]
    return ToolRegistry(tools, enable_action_markers=False)


async def _build_engine(
    tools_meta,
    *,
    pin_anchors=None,
    enable_hierarchy=False,
):
    """All optional features off by default in unit tests so the Judge
    gets exactly the queued LLM response and rankings come from pinned
    cosines alone.

    NOTE: enable_hyde, enable_sparse, enable_second_pass,
    enable_category_prefilter were REMOVED 2026-05-08 (empirically
    falsified). Tests that exercised those features are deleted; the
    builder no longer accepts those kwargs.
    """
    registry = _make_registry(tools_meta)
    embedder = FakeEmbedder()
    if pin_anchors:
        for text, vec in pin_anchors.items():
            embedder.set(text, vec)
    llm = FakeLLM()
    eng = RecognitionEngine(
        embedder, llm, "gpt-4o-mini", registry,
        enable_hierarchy=enable_hierarchy,
    )
    await eng.initialize()
    return eng, embedder, llm, registry


# --------------------------------------------------------------------------
# Initialize
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_initialize_indexes_every_tool_with_anchor_text():
    eng, _, _, _ = await _build_engine([
        ("get_A", "GET", "Anchor for A"),
        ("get_B", "GET", "Anchor for B"),
    ])
    assert len(eng._anchors) == 2


@pytest.mark.asyncio
async def test_initialize_is_idempotent():
    eng, _, _, _ = await _build_engine([("get_A", "GET", "Anchor")])
    n_before = len(eng._anchors)
    await eng.initialize()  # second call should noop
    assert len(eng._anchors) == n_before


@pytest.mark.asyncio
async def test_initialize_skips_tools_when_embedder_returns_none():
    registry = _make_registry([
        ("get_A", "GET", "Anchor for A"),
        ("get_B", "GET", "Anchor for B"),
    ])
    embedder = FakeEmbedder()
    embedder.fail_on.add("Anchor for B")
    eng = RecognitionEngine(embedder, FakeLLM(), "x", registry)
    await eng.initialize()
    assert len(eng._anchors) == 1
    assert eng._anchors[0][0] == "get_A"


# --------------------------------------------------------------------------
# recognize() — error / fallback paths
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recognize_returns_error_when_not_initialized():
    registry = _make_registry([("get_A", "GET", "x")])
    eng = RecognitionEngine(FakeEmbedder(), FakeLLM(), "x", registry)
    r = await eng.recognize("query")
    assert r.error == "not_initialized"


@pytest.mark.asyncio
async def test_recognize_empty_query_returns_error():
    eng, _, _, _ = await _build_engine([("get_A", "GET", "x")])
    r = await eng.recognize("")
    assert r.error == "empty_query"


@pytest.mark.asyncio
async def test_recognize_returns_error_when_query_embed_fails():
    eng, embedder, _, _ = await _build_engine([("get_A", "GET", "x")])
    embedder.return_none = True
    r = await eng.recognize("anything")
    assert r.error == "no_candidates"


@pytest.mark.asyncio
async def test_recognize_judge_exception_yields_error_with_candidates():
    eng, _, llm, _ = await _build_engine([("get_A", "GET", "x")])
    llm.completions.queue.append(ConnectionError("azure unreachable"))
    r = await eng.recognize("query")
    assert r.error and "judge_error" in r.error
    assert r.candidates  # candidates still provided for context fallback


@pytest.mark.asyncio
async def test_recognize_invalid_tool_pick_falls_back():
    eng, _, llm, _ = await _build_engine([("get_A", "GET", "x")])
    llm.completions.push(json.dumps({
        "tool_id": "tool_we_invented_off_corpus",
        "rationale": "x" * 50,
        "confidence": 0.9,
    }))
    r = await eng.recognize("query")
    assert r.error and r.error.startswith("invalid_tool:")


# --------------------------------------------------------------------------
# recognize() — happy paths
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recognize_high_confidence_returns_normal_result():
    eng, embedder, llm, _ = await _build_engine([
        ("get_A", "GET", "Anchor for A"),
        ("get_B", "GET", "Anchor for B"),
    ], pin_anchors={
        "Anchor for A": [1.0, 0, 0, 0, 0, 0, 0, 0],
        "Anchor for B": [0, 1.0, 0, 0, 0, 0, 0, 0],
    })
    embedder.set("kilometraza pitanje", [1.0, 0, 0, 0, 0, 0, 0, 0])

    llm.completions.push(json.dumps({
        "tool_id":     "get_A",
        "rationale":   "User clearly asked about A; anchor strongly aligned.",
        "confidence":  0.92,
        "params":      {"id": "v1"},
        "template_id": "vehicle_data_summary",
    }))

    r = await eng.recognize("kilometraza pitanje")
    assert r.tool_id == "get_A"
    assert r.params == {"id": "v1"}
    assert r.template_id == "vehicle_data_summary"
    assert r.llm_confidence == pytest.approx(0.92)
    assert r.anchor_score == pytest.approx(1.0, abs=0.001)
    assert r.disagreement is False
    assert r.error is None


@pytest.mark.asyncio
async def test_recognize_disagreement_downgrades_combined_confidence():
    """Anchor #1 is get_A but Judge picks get_B → disagreement true,
    combined confidence multiplied by 0.7."""
    eng, embedder, llm, _ = await _build_engine([
        ("get_A", "GET", "Anchor for A"),
        ("get_B", "GET", "Anchor for B"),
    ], pin_anchors={
        "Anchor for A": [1.0, 0, 0, 0, 0, 0, 0, 0],
        "Anchor for B": [0, 1.0, 0, 0, 0, 0, 0, 0],
    })
    embedder.set("ambiguous query", [1.0, 0, 0, 0, 0, 0, 0, 0])  # anchor → A

    llm.completions.push(json.dumps({
        "tool_id":    "get_B",   # judge disagrees with anchor
        "rationale":  "Despite anchor leaning A, semantics fit B better.",
        "confidence": 0.9,
    }))
    r = await eng.recognize("ambiguous query")
    assert r.tool_id == "get_B"
    assert r.disagreement is True
    plain = 0.6 * r.anchor_score + 0.4 * r.llm_confidence
    assert r.combined_confidence == pytest.approx(plain * 0.7, abs=0.01)


@pytest.mark.asyncio
async def test_recognize_short_rationale_caps_llm_confidence():
    eng, _, llm, _ = await _build_engine([("get_A", "GET", "x")])
    llm.completions.push(json.dumps({
        "tool_id":   "get_A",
        "rationale": "short",  # < MIN_RATIONALE_LEN
        "confidence": 0.99,
    }))
    r = await eng.recognize("query")
    assert len(r.rationale) < MIN_RATIONALE_LEN
    assert r.llm_confidence <= 0.5  # capped


@pytest.mark.asyncio
async def test_recognize_flow_name_passed_through():
    eng, _, llm, _ = await _build_engine([("get_A", "GET", "x")])
    llm.completions.push(json.dumps({
        "tool_id":     None,
        "flow_name":   "booking",
        "rationale":   "User wants a multi-step booking interaction.",
        "confidence":  0.9,
    }))
    r = await eng.recognize("rezerviraj auto sutra")
    assert r.flow_name == "booking"
    assert r.tool_id is None


@pytest.mark.asyncio
async def test_recognize_clamps_confidence_to_zero_one():
    eng, _, llm, _ = await _build_engine([("get_A", "GET", "x")])
    llm.completions.push(json.dumps({
        "tool_id":   "get_A",
        "rationale": "x" * 50,
        "confidence": 1.7,  # out-of-range
    }))
    r = await eng.recognize("q")
    assert r.llm_confidence == 1.0


@pytest.mark.asyncio
async def test_recognize_handles_judge_returning_fenced_json():
    eng, _, llm, _ = await _build_engine([("get_A", "GET", "x")])
    llm.completions.push(
        "```json\n" +
        json.dumps({
            "tool_id":   "get_A",
            "rationale": "x" * 50,
            "confidence": 0.9,
        }) +
        "\n```"
    )
    r = await eng.recognize("q")
    assert r.tool_id == "get_A"


# --------------------------------------------------------------------------
# audience filter
# --------------------------------------------------------------------------



@pytest.mark.asyncio
async def test_llm_retrieve_merges_picks_into_candidates():
    """When enable_llm_retrieve=True, the LLM's catalog picks are
    front-loaded into the candidates list before the Judge sees them.
    Tools the LLM picks but dense missed get fresh ToolCandidate
    entries; tools both signals like get the dense anchor_score."""
    registry = _make_registry([
        ("get_A", "GET", "Anchor for A"),
        ("get_B", "GET", "Anchor for B"),
        ("get_C", "GET", "Anchor for C"),
    ])
    embedder = FakeEmbedder()
    llm = FakeLLM()
    eng = RecognitionEngine(
        embedder, llm, "x", registry,
        enable_llm_retrieve=True,
        llm_retrieve_top_n=2,
    )
    await eng.initialize()

    # First: LLM-retrieve response (catalog pass)
    llm.completions.push(json.dumps({"tools": ["get_C", "get_A"]}))
    # Then: Judge picks one
    llm.completions.push(json.dumps({
        "tool_id":   "get_C",
        "rationale": "x" * 50,
        "confidence": 0.9,
    }))
    r = await eng.recognize("any query")
    cand_ids = [c.tool_id for c in r.candidates]
    # LLM picks first, then dense candidates not already in
    assert cand_ids[0] == "get_C"
    assert cand_ids[1] == "get_A"
    # Final pick respects Judge
    assert r.tool_id == "get_C"


@pytest.mark.asyncio
async def test_llm_retrieve_off_means_no_extra_call_consumed():
    """Default enable_llm_retrieve=False must NOT consume an LLM call
    for the catalog pass. Only the Judge call should fire."""
    eng, _, llm, _ = await _build_engine(
        [("get_A", "GET", "Anchor for A")],
    )
    llm.completions.push(json.dumps({
        "tool_id":   "get_A",
        "rationale": "x" * 50,
        "confidence": 0.9,
    }))
    r = await eng.recognize("any query")
    assert r.tool_id == "get_A"
    assert llm.completions.queue == []  # no extra calls consumed


@pytest.mark.asyncio
async def test_llm_retrieve_handles_empty_pick_gracefully():
    """If the LLM returns an empty list (no good match in catalog),
    the engine falls through to the dense candidates."""
    registry = _make_registry([("get_A", "GET", "Anchor")])
    embedder = FakeEmbedder()
    llm = FakeLLM()
    eng = RecognitionEngine(
        embedder, llm, "x", registry,
        enable_llm_retrieve=True,
    )
    await eng.initialize()
    # LLM-retrieve returns empty
    llm.completions.push(json.dumps({"tools": []}))
    # Judge still fires on dense candidates
    llm.completions.push(json.dumps({
        "tool_id":   "get_A",
        "rationale": "x" * 50,
        "confidence": 0.9,
    }))
    r = await eng.recognize("query")
    assert r.tool_id == "get_A"


@pytest.mark.asyncio
async def test_anchor_enrichment_concatenates_phrases_into_one_entry():
    """Enrichment phrases are concatenated into ONE anchor entry per
    tool — empirically a single long semantically-rich vector beats
    N short vectors (measured: 20.3% concat vs 9.4% multi-entry on
    the adversarial benchmark, see docs/round_log.md if/when written).

    Multi-entry surfaced phrases that match queries via accidental
    short-string cosine noise rather than meaningful tool relevance.
    Concat averages those out and stays closer to abstract paraphrases.
    """
    from services.v2.registry import ToolRegistry
    enriched = ToolRegistry(
        [{
            "operation_id":   "get_X",
            "method":         "GET",
            "service_name":   "automation",
            "path":           "/X",
            "embedding_text": "terse anchor",
        }],
        anchor_enrichments={
            "get_X": ["Croatian phrase one.", "Croatian phrase two."],
        },
    )
    embedder = FakeEmbedder()
    eng = RecognitionEngine(embedder, FakeLLM(), "x", enriched)
    await eng.initialize()
    # ONE entry per tool, not per phrase
    assert len(eng._anchors) == 1
    tool_id, anchor_text, _vec = eng._anchors[0]
    assert tool_id == "get_X"
    # And that one anchor must contain BOTH the enrichment and the base
    assert "Croatian phrase one." in anchor_text
    assert "Croatian phrase two." in anchor_text
    assert "terse anchor" in anchor_text


# Category-prefilter tests removed 2026-05-08 alongside the feature.


@pytest.mark.asyncio
async def test_anchor_cache_round_trip(tmp_path):
    """Initialize once, persist to disk; second engine loads from disk
    without calling embedder."""
    cache_path = str(tmp_path / "anchors.json")
    registry = _make_registry([("get_A", "GET", "Anchor for A")])
    embedder1 = FakeEmbedder()
    eng1 = RecognitionEngine(
        embedder1, FakeLLM(), "gpt-4o-mini", registry,
        anchor_cache_path=cache_path,
    )
    await eng1.initialize()
    assert eng1._anchors

    # Second engine — same registry/deployment, fresh embedder that
    # would explode if called
    embedder2 = FakeEmbedder()
    embedder2.fail_on.add("Anchor for A")
    eng2 = RecognitionEngine(
        embedder2, FakeLLM(), "gpt-4o-mini", registry,
        anchor_cache_path=cache_path,
    )
    await eng2.initialize()
    assert len(eng2._anchors) == 1
    assert eng2._anchors[0][0] == "get_A"


@pytest.mark.asyncio
async def test_anchor_cache_invalidates_on_registry_change(tmp_path):
    """If the registry's tool set changes, the cache fingerprint
    differs and the engine re-embeds rather than serving stale data."""
    cache_path = str(tmp_path / "anchors.json")
    registry_v1 = _make_registry([("get_A", "GET", "Anchor for A")])
    eng1 = RecognitionEngine(
        FakeEmbedder(), FakeLLM(), "gpt-4o-mini", registry_v1,
        anchor_cache_path=cache_path,
    )
    await eng1.initialize()

    # Now bigger registry → fingerprint differs → fresh embed
    registry_v2 = _make_registry([
        ("get_A", "GET", "Anchor for A"),
        ("get_B", "GET", "Anchor for B"),
    ])
    eng2 = RecognitionEngine(
        FakeEmbedder(), FakeLLM(), "gpt-4o-mini", registry_v2,
        anchor_cache_path=cache_path,
    )
    await eng2.initialize()
    assert len(eng2._anchors) == 2


@pytest.mark.asyncio
async def test_real_registry_initialize_indexes_all_tools_with_anchors():
    """Heavy: build the real 950-tool index and assert non-trivial size.

    Embedder is a hash-stub (deterministic, no network), so this is a
    pure structural test — every tool with embedding_text gets indexed.
    """
    registry = ToolRegistry.from_file(REGISTRY_PATH)
    eng = RecognitionEngine(FakeEmbedder(), FakeLLM(), "x", registry)
    await eng.initialize()
    assert len(eng._anchors) > 800  # ~950 tools, all have embedding_text


@pytest.mark.asyncio
async def test_real_registry_recognize_returns_top_k_from_corpus():
    """End-to-end on the real registry with a stub Judge —
    confirms top-K wires through and one of them is whitelisted."""
    registry = ToolRegistry.from_file(REGISTRY_PATH)
    eng = RecognitionEngine(FakeEmbedder(), FakeLLM(), "x", registry)
    await eng.initialize()

    eng._llm.completions.push(json.dumps({
        # Judge picks whatever the anchor #1 is — guaranteed valid.
        "tool_id":   None,
        "rationale": "x" * 50,
        "confidence": 0.5,
    }))

    r = await eng.recognize("kolika mi je km na vozilu")
    assert isinstance(r, RecognitionResult)
    assert r.error is None or r.error == "no_candidates"
    if r.candidates:
        # All candidates must be real, whitelisted tool_ids
        for c in r.candidates:
            assert registry.has_tool(c.tool_id)
        assert len(r.candidates) <= 20
