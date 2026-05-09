"""Tests for confusion_disambiguator module."""
from __future__ import annotations

import math
from dataclasses import dataclass

import pytest

from services.v2.confusion_disambiguator import (
    ConfusionDisambiguator,
    DISCRIMINATORS,
    apply_disambiguator_bias,
    extract_entity_from_tool_id,
)


class _FakeEmbedder:
    async def embed_batch(self, texts):
        # Each text → unique 4-d unit vector based on hash
        out = []
        for t in texts:
            h = hash(t) % 1000
            out.append([
                math.cos(h * 0.01),
                math.sin(h * 0.01),
                math.cos(h * 0.02),
                math.sin(h * 0.02),
            ])
        return out


@dataclass
class _FakeCandidate:
    tool_id: str
    anchor_score: float


def test_extract_entity_basic():
    assert extract_entity_from_tool_id("delete_CostCenters") == "CostCenters"
    assert extract_entity_from_tool_id("get_Expenses_id") == "Expenses"
    assert extract_entity_from_tool_id("") is None


def test_discriminators_cover_known_clusters():
    pairs = {tuple(d["cluster"]) for d in DISCRIMINATORS}
    assert ("CostCenters", "Expenses") in pairs
    assert ("CostCenters", "OrgUnits") in pairs
    assert ("OrgUnits", "Companies") in pairs
    assert ("MileageReports", "LatestMileageReports") in pairs


def test_each_discriminator_has_bidirectional_signals():
    """Every cluster must have signals pointing to BOTH entities — that's
    the difference from when_not_to_use which is one-directional."""
    for entry in DISCRIMINATORS:
        cluster = entry["cluster"]
        favored = {sig[1] for sig in entry["signals"]}
        # Both cluster members should appear as 'favored' across the signals
        assert all(c in favored for c in cluster), \
            f"Cluster {cluster} lacks bidirectional signals; favored={favored}"


@pytest.mark.asyncio
async def test_initialize_loads_all_discriminators():
    d = ConfusionDisambiguator()
    await d.initialize(_FakeEmbedder())
    expected_count = sum(len(e["signals"]) for e in DISCRIMINATORS)
    assert d.size() == expected_count


@pytest.mark.asyncio
async def test_initialize_idempotent():
    d = ConfusionDisambiguator()
    await d.initialize(_FakeEmbedder())
    n1 = d.size()
    await d.initialize(_FakeEmbedder())
    assert d.size() == n1


@pytest.mark.asyncio
async def test_score_query_returns_dict():
    d = ConfusionDisambiguator()
    await d.initialize(_FakeEmbedder())
    bias = d.score_query([0.5, 0.5, 0.5, 0.5], threshold=0.0)
    assert isinstance(bias, dict)
    # With threshold 0.0, every discriminator fires → all entities should
    # have some bias (positive for favored, negative for opposing).
    assert len(bias) > 0


@pytest.mark.asyncio
async def test_score_query_with_high_threshold_returns_empty():
    d = ConfusionDisambiguator()
    await d.initialize(_FakeEmbedder())
    bias = d.score_query([0.5, 0.5, 0.5, 0.5], threshold=0.999)
    # No real cosine match should exceed 0.999 with random fake vectors
    assert bias == {} or all(abs(v) < 1.0 for v in bias.values())


def test_apply_bias_preserves_recall():
    """Applying bias never drops candidates — only re-orders."""
    candidates = [
        _FakeCandidate("get_Expenses", 0.8),
        _FakeCandidate("get_CostCenters", 0.7),
        _FakeCandidate("get_OrgUnits", 0.6),
    ]
    bias = {"CostCenters": 0.5, "Expenses": -0.5}
    out = apply_disambiguator_bias(candidates, bias, bias_weight=0.1)
    assert len(out) == len(candidates)
    assert {c.tool_id for c in out} == {c.tool_id for c in candidates}


def test_apply_bias_promotes_favored_entity():
    candidates = [
        _FakeCandidate("get_Expenses", 0.8),       # would be top without bias
        _FakeCandidate("get_CostCenters", 0.7),
    ]
    # Strong CostCenters bias, anti-Expenses
    bias = {"CostCenters": 5.0, "Expenses": -5.0}
    out = apply_disambiguator_bias(candidates, bias, bias_weight=0.1)
    assert out[0].tool_id == "get_CostCenters"


def test_apply_bias_no_op_when_empty_bias():
    candidates = [
        _FakeCandidate("get_Expenses", 0.8),
        _FakeCandidate("get_CostCenters", 0.7),
    ]
    out = apply_disambiguator_bias(candidates, {}, bias_weight=0.1)
    # Empty bias → return unchanged (still a list of same length)
    assert len(out) == len(candidates)
    assert out[0].tool_id == "get_Expenses"


def test_apply_bias_handles_unmapped_entities():
    """Candidates whose entity is not in bias dict should keep original score."""
    candidates = [
        _FakeCandidate("get_RandomEntity", 0.9),  # not in bias
        _FakeCandidate("get_CostCenters", 0.7),    # boosted
    ]
    bias = {"CostCenters": 5.0}
    out = apply_disambiguator_bias(candidates, bias, bias_weight=0.1)
    # CostCenters: 0.7 + 0.5 = 1.2 > RandomEntity: 0.9
    assert out[0].tool_id == "get_CostCenters"
