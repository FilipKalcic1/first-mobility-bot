"""Tests for the golden-set harvester (scripts/build_golden_set.py).

Tests the pure `build_labels` classifier — no Redis needed. The script is
loaded via importlib because scripts/ is not an importable package.
Covers the chained-correction + L2B-sentinel + confidence rules added in
the Filip review (2026-06-10).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "build_golden_set", _REPO / "scripts" / "build_golden_set.py"
)
bgs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bgs)


def _g(result):
    """Index golden list by (query, expected_tool)."""
    return {(d["query"], d["expected_tool"]): d for d in result["golden"]}


def test_single_correction_positive_and_negative():
    events = [{
        "tenant_id": "t1",
        "query_scrubbed": "moje rezervacije",
        "tool_picked": "get_VehicleCalendar",
        "correction": {"wrong_tool": "delete_VehicleCalendar_id",
                       "correct_tool": "get_VehicleCalendar"},
    }]
    r = bgs.build_labels(events)
    g = _g(r)
    label = g[("moje rezervacije", "get_VehicleCalendar")]
    # positive correct_tool is MEDIUM (picked + no complaint), not HIGH
    assert label["source"] == "corrected"
    assert label["label_confidence"] == "medium"
    assert label["chain_len"] == 1
    # negative is HIGH (explicit rejection) and carries no positive "source"
    assert r["negatives"] == [{"query": "moje rezervacije",
                               "wrong_tool": "delete_VehicleCalendar_id",
                               "confidence": "high"}]
    assert r["counts"]["correction_chains"] == 1


def test_chain_resolves_to_last_survivor_only():
    """A → nije točno → B → nije točno → C: golden must contain ONLY C.
    B must NOT be a positive (it was later rejected), and A, B are negatives."""
    q = "obriši onu stavku"
    events = [
        {"tenant_id": "t1", "query_scrubbed": q, "tool_picked": "B",
         "correction": {"wrong_tool": "A", "correct_tool": "B"}},
        {"tenant_id": "t1", "query_scrubbed": q, "tool_picked": "C",
         "correction": {"wrong_tool": "B", "correct_tool": "C"}},
    ]
    r = bgs.build_labels(events)
    g = _g(r)
    assert (q, "C") in g
    assert g[(q, "C")]["source"] == "corrected"
    assert (q, "B") not in g          # B was rejected — must NOT be a positive
    assert (q, "A") not in g
    neg_tools = {n["wrong_tool"] for n in r["negatives"]}
    assert neg_tools == {"A", "B"}     # both rejected → negatives
    assert r["counts"]["golden_unique"] == 1


def test_l2b_sentinel_becomes_category_not_tool_id():
    """Rejected L2B shortcut must be a categorized negative, never a tool_id;
    the real positive is the resolving pick."""
    q = "kolika km"
    events = [{
        "tenant_id": "t1", "query_scrubbed": q, "tool_picked": "get_MasterData",
        "correction": {"wrong_tool": "__L2B_DRIVER_BASICS__",
                       "correct_tool": "get_FuelExpenses"},
    }]
    r = bgs.build_labels(events)
    g = _g(r)
    assert (q, "get_FuelExpenses") in g          # real positive kept
    # sentinel never appears as a tool_id anywhere
    assert all("__L2B_DRIVER_BASICS__" != d["expected_tool"] for d in r["golden"])
    assert r["negatives"] == [{"query": q, "wrong_tool": None,
                               "category": "l2b_rejected", "confidence": "high"}]


def test_weak_accepted_kept_when_not_contradicted():
    events = [{"tenant_id": "t1", "query_scrubbed": "kolika km",
               "tool_picked": "get_MasterData"}]
    r = bgs.build_labels(events)
    assert r["counts"]["accepted_weak_kept"] == 1
    assert r["golden"][0]["source"] == "accepted_weak"
    assert r["golden"][0]["label_confidence"] == "low"


def test_weak_suppressed_if_tool_rejected_for_query():
    """A weak positive must be dropped if a correction rejected that tool
    for the same query (the weak signal is contradicted by explicit data)."""
    q = "moje rezervacije"
    events = [
        # weak positive proposing delete_X
        {"tenant_id": "t1", "query_scrubbed": q, "tool_picked": "delete_X"},
        # but a correction rejected delete_X for the same query
        {"tenant_id": "t1", "query_scrubbed": q, "tool_picked": "get_X",
         "correction": {"wrong_tool": "delete_X", "correct_tool": "get_X"}},
    ]
    r = bgs.build_labels(events)
    g = _g(r)
    assert (q, "delete_X") not in g       # contradicted weak → dropped
    assert (q, "get_X") in g              # corrected positive kept
    assert r["counts"]["accepted_weak_kept"] == 0


def test_negated_error_and_empty_dropped_from_positives():
    events = [
        {"tenant_id": "t1", "query_scrubbed": "a", "tool_picked": "get_X", "is_negation": True},
        {"tenant_id": "t1", "query_scrubbed": "b", "tool_picked": "get_Y", "error": "execution_failed"},
        {"tenant_id": "t1", "query_scrubbed": "", "tool_picked": "get_W"},
    ]
    r = bgs.build_labels(events)
    picked = {d["expected_tool"] for d in r["golden"]}
    assert picked.isdisjoint({"get_X", "get_Y", "get_W"})
    assert r["counts"]["golden_unique"] == 0


def test_corrected_outranks_weak_for_same_pair():
    q = "kolika km"
    events = [
        {"tenant_id": "t1", "query_scrubbed": q, "tool_picked": "get_MasterData"},
        {"tenant_id": "t1", "query_scrubbed": q, "tool_picked": "get_MasterData",
         "correction": {"wrong_tool": "get_Z", "correct_tool": "get_MasterData"}},
    ]
    r = bgs.build_labels(events)
    g = _g(r)
    assert g[(q, "get_MasterData")]["source"] == "corrected"


def test_empty_events_no_crash():
    r = bgs.build_labels([])
    assert r["counts"]["golden_unique"] == 0
    assert r["golden"] == []
    assert r["negatives"] == []
