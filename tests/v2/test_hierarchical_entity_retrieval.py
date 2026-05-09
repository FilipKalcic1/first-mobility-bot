"""Tests for hierarchical_entity_retrieval module."""
from __future__ import annotations

import math
import pytest

from services.v2.hierarchical_entity_retrieval import (
    ENTITY_DESCRIPTIONS,
    EntityIndex,
    PerEntityToolIndex,
    extract_entity_from_tool_id,
    hierarchical_retrieve,
)


class _FakeEmbedder:
    """Deterministic fake embedder — maps text length to vector for testing."""

    async def embed_batch(self, texts):
        # Each entity gets a unique 4-d vector based on hash of its description
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


def test_extract_entity_basic():
    assert extract_entity_from_tool_id("delete_VehicleCalendar_id") == "VehicleCalendar"
    assert extract_entity_from_tool_id("get_MileageReports") == "MileageReports"
    assert extract_entity_from_tool_id("post_AddCase_parse") == "AddCase"
    assert extract_entity_from_tool_id("get_Lookup_CompanyId") == "Lookup"


def test_extract_entity_returns_none_for_invalid():
    assert extract_entity_from_tool_id("") is None
    assert extract_entity_from_tool_id(None) is None
    assert extract_entity_from_tool_id("not_a_real_tool") is None


def test_entity_descriptions_cover_known_entities():
    """ENTITY_DESCRIPTIONS must include the major confused-cluster entities."""
    required = {
        'CostCenters', 'Expenses', 'OrgUnits', 'Companies',
        'MileageReports', 'LatestMileageReports',
        'PeriodicActivitiesSchedules', 'PersonPeriodicActivities',
        'PersonOrgUnits', 'Lookup', 'VehicleCalendar', 'Vehicles',
    }
    missing = required - set(ENTITY_DESCRIPTIONS.keys())
    assert not missing, f"Missing descriptions for: {missing}"


def test_entity_descriptions_have_disambiguation_hints():
    """Confusion-cluster entities must have explicit 'NIJE X' hints."""
    cost_centers = ENTITY_DESCRIPTIONS['CostCenters']
    assert "NIJE Expenses" in cost_centers or "NIJE OrgUnits" in cost_centers

    expenses = ENTITY_DESCRIPTIONS['Expenses']
    assert "NIJE CostCenter" in expenses

    org_units = ENTITY_DESCRIPTIONS['OrgUnits']
    assert "NIJE Companies" in org_units or "NIJE CostCenters" in org_units


@pytest.mark.asyncio
async def test_entity_index_initialize():
    idx = EntityIndex()
    embedder = _FakeEmbedder()
    await idx.initialize(embedder)
    assert len(idx._entity_vectors) == len(ENTITY_DESCRIPTIONS)


@pytest.mark.asyncio
async def test_entity_index_classify_returns_top_k():
    idx = EntityIndex()
    embedder = _FakeEmbedder()
    await idx.initialize(embedder)
    query_vec = [0.5, 0.5, 0.5, 0.5]
    hits = idx.classify(query_vec, top_k=3)
    assert len(hits) == 3
    # Scores must be sorted descending
    assert hits[0].score >= hits[1].score >= hits[2].score


def test_per_entity_tool_index_groups_by_entity():
    anchors = [
        ("get_VehicleCalendar", "rezerv", [1.0, 0.0]),
        ("delete_VehicleCalendar_id", "otkazi", [0.9, 0.1]),
        ("get_MileageReports", "km", [0.0, 1.0]),
        ("post_AddMileage", "unesi km", [0.1, 0.9]),
    ]
    idx = PerEntityToolIndex()
    idx.build(anchors)
    assert "VehicleCalendar" in idx._buckets
    assert len(idx._buckets["VehicleCalendar"]) == 2
    assert "MileageReports" in idx._buckets
    assert "AddMileage" in idx._buckets


def test_per_entity_tool_index_retrieve_within_entities():
    anchors = [
        ("get_VehicleCalendar", "x", [1.0, 0.0]),
        ("delete_VehicleCalendar_id", "y", [0.95, 0.05]),
        ("get_MileageReports", "z", [0.0, 1.0]),
    ]
    idx = PerEntityToolIndex()
    idx.build(anchors)
    # Query close to VehicleCalendar bucket
    hits = idx.retrieve_within_entities([1.0, 0.0], ["VehicleCalendar"], per_entity_top_k=10)
    assert len(hits) == 2
    assert hits[0][0] == "get_VehicleCalendar"  # higher cosine
    # No tools in entity → empty
    hits = idx.retrieve_within_entities([1.0, 0.0], ["NonExistentEntity"], per_entity_top_k=10)
    assert hits == []


def test_per_entity_tool_index_respects_per_entity_top_k():
    """If per_entity_top_k limits, only top within each entity bucket are returned."""
    anchors = [
        ("get_Apples", "x1", [1.0, 0.0]),
        ("get_Apples_id", "x2", [0.99, 0.01]),
        ("delete_Apples_id", "x3", [0.98, 0.02]),
        ("get_Bananas", "y1", [0.0, 1.0]),
        ("get_Bananas_id", "y2", [0.01, 0.99]),
    ]
    idx = PerEntityToolIndex()
    idx.build(anchors)
    hits = idx.retrieve_within_entities(
        [0.7, 0.7], ["Apples", "Bananas"], per_entity_top_k=1,
    )
    # Should get exactly 2 (one per entity, limited to top-1 each)
    assert len(hits) == 2


@pytest.mark.asyncio
async def test_hierarchical_retrieve_end_to_end():
    """Full pipeline: query → entity classify → per-entity retrieve.

    Builds entity index with the entities present in the tool index so
    Stage 1 can pick something that exists in Stage 2 buckets.
    """
    embedder = _FakeEmbedder()
    eidx = EntityIndex()
    await eidx.initialize(embedder)
    # Tool anchors are 4-d to match the fake embedder output dimension.
    anchors = [
        ("get_VehicleCalendar", "rezervacije", [0.5, 0.5, 0.5, 0.5]),
        ("delete_VehicleCalendar_id", "otkazi", [0.5, 0.5, 0.5, 0.5]),
        ("get_MileageReports", "km zapis", [0.4, 0.6, 0.3, 0.7]),
        ("post_AddMileage", "novi km", [0.4, 0.6, 0.3, 0.7]),
        # Add buckets matching real ENTITY_DESCRIPTIONS so Stage 1 picks
        # an entity that has tools in Stage 2.
        ("get_Companies", "tvrtke", [0.3, 0.7, 0.4, 0.6]),
        ("get_OrgUnits", "odjeli", [0.6, 0.4, 0.5, 0.5]),
    ]
    tidx = PerEntityToolIndex()
    tidx.build(anchors)

    query_vec = [0.5, 0.5, 0.5, 0.5]
    # entity_top_k=47 (all entities) guarantees Stage 1 will pick entities
    # that have tools in Stage 2 buckets — the fake embedder's ranking
    # is otherwise arbitrary.
    candidates = hierarchical_retrieve(
        query_vector=query_vec,
        entity_index=eidx,
        tool_index=tidx,
        entity_top_k=47,
        per_entity_tool_top_k=5,
        final_top_k=10,
    )
    assert len(candidates) >= 1
    for c in candidates:
        assert isinstance(c.combined_score, float)
        assert c.entity == extract_entity_from_tool_id(c.tool_id)
