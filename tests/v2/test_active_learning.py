"""Tests for active_learning analysis functions."""
from __future__ import annotations

from services.v2.active_learning import (
    NegationRateRow,
    extract_confidence_distribution,
    extract_negation_per_tool,
    extract_query_patterns,
    extract_tool_coverage,
    render_markdown_report,
)


def _ev(
    ts: str,
    tenant_id: str = "t1",
    query: str = "",
    tool: str | None = None,
    is_negation: bool = False,
    confidence: float | None = None,
) -> dict:
    """Synthetic telemetry event factory — mirrors TelemetryEvent.to_record()."""
    e: dict = {
        "ts": ts,
        "tenant_id": tenant_id,
        "is_negation": is_negation,
    }
    if query:
        e["query_scrubbed"] = query
    if tool is not None:
        e["tool_picked"] = tool
    if confidence is not None:
        e["confidence"] = confidence
    return e


# --------------------------------------------------------------------------
# extract_negation_per_tool
# --------------------------------------------------------------------------


def test_negation_per_tool_counts_next_turn_negations():
    """Tool A picked → next turn has is_negation → A gets +1 negation."""
    events = [
        _ev("2026-05-17T10:00:00", tool="get_A"),
        _ev("2026-05-17T10:01:00", is_negation=True),
        _ev("2026-05-17T10:02:00", tool="get_A"),  # no negation after
    ]
    rows = extract_negation_per_tool(events)
    assert len(rows) == 1
    assert rows[0].tool_id == "get_A"
    assert rows[0].picks == 2
    assert rows[0].negation_count == 1
    assert rows[0].rate == 0.5


def test_negation_per_tool_sorted_by_rate_desc():
    """Higher rate comes first; ties broken by picks desc."""
    events = [
        # Tool A: 1 pick, 1 negation → 100%
        _ev("2026-05-17T10:00:00", tool="get_A"),
        _ev("2026-05-17T10:01:00", is_negation=True),
        # Tool B: 10 picks, 2 negations → 20%
        *[
            ev for i in range(10)
            for ev in (
                _ev(f"2026-05-17T11:{i:02d}:00", tool="get_B"),
                _ev(f"2026-05-17T11:{i:02d}:30",
                    is_negation=(i < 2)),
            )
        ],
    ]
    rows = extract_negation_per_tool(events)
    # A's 100% beats B's 20%
    assert rows[0].tool_id == "get_A"
    assert rows[1].tool_id == "get_B"


def test_negation_per_tool_separates_tenants():
    """Negation in tenant_2 must NOT count against tool picked in tenant_1."""
    events = [
        _ev("2026-05-17T10:00:00", tenant_id="t1", tool="get_X"),
        _ev("2026-05-17T10:00:30", tenant_id="t2", is_negation=True),
    ]
    rows = extract_negation_per_tool(events)
    assert rows[0].tool_id == "get_X"
    assert rows[0].negation_count == 0  # cross-tenant ignored


def test_negation_per_tool_handles_empty_events():
    assert extract_negation_per_tool([]) == []


def test_negation_per_tool_ignores_events_without_tool_picked():
    """Events with no tool_picked (e.g., crisis intercept, rate limit) skip."""
    events = [
        _ev("2026-05-17T10:00:00", query="ne želim živjeti"),  # no tool
        _ev("2026-05-17T10:01:00", is_negation=True),
    ]
    rows = extract_negation_per_tool(events)
    assert rows == []


# --------------------------------------------------------------------------
# extract_query_patterns
# --------------------------------------------------------------------------


def test_query_patterns_finds_unstable_routing():
    """Same query picked different tools across occurrences → flagged."""
    events = [
        _ev("t1", query="kolika km", tool="get_MasterData"),
        _ev("t2", query="kolika km", tool="get_LatestMileageReports"),
        _ev("t3", query="kolika km", tool="get_MasterData"),
        _ev("t4", query="kolika km", tool="get_MileageReports"),
    ]
    out = extract_query_patterns(events, min_repeats=3, min_distinct_tools=2)
    assert len(out) == 1
    assert out[0]["query"] == "kolika km"
    assert out[0]["total_occurrences"] == 4
    assert out[0]["distinct_tools"] == 3
    # MasterData picked most (2/4)
    first_tool = next(iter(out[0]["tool_distribution"]))
    assert first_tool == "get_MasterData"


def test_query_patterns_stable_routing_not_flagged():
    """Same query always picks same tool → consistent, no flag."""
    events = [
        _ev(f"t{i}", query="kolika km", tool="get_MasterData")
        for i in range(5)
    ]
    out = extract_query_patterns(events)
    assert out == []


def test_query_patterns_min_repeats_filter():
    """Query seen only 2 times (under min_repeats=3) is NOT flagged."""
    events = [
        _ev("t1", query="rare query", tool="get_A"),
        _ev("t2", query="rare query", tool="get_B"),
    ]
    out = extract_query_patterns(events, min_repeats=3)
    assert out == []


def test_query_patterns_min_distinct_tools_filter():
    """Even with many repeats, must have ≥2 distinct tools to be a pattern."""
    events = [
        _ev(f"t{i}", query="stable q", tool="get_X")
        for i in range(10)
    ]
    out = extract_query_patterns(events, min_repeats=3, min_distinct_tools=2)
    assert out == []


def test_query_patterns_sorted_by_total_occurrences_desc():
    events = [
        # query_high seen 5 times across 2 tools
        *[_ev(f"a{i}", query="query_high", tool=f"get_{i % 2}") for i in range(5)],
        # query_low seen 3 times across 2 tools
        *[_ev(f"b{i}", query="query_low", tool=f"get_{i % 2}") for i in range(3)],
    ]
    out = extract_query_patterns(events, min_repeats=3, min_distinct_tools=2)
    assert out[0]["query"] == "query_high"
    assert out[1]["query"] == "query_low"


# --------------------------------------------------------------------------
# extract_confidence_distribution
# --------------------------------------------------------------------------


def test_confidence_distribution_buckets_correctly():
    events = [
        _ev("t1", tool="get_A", confidence=0.1),   # 0.0-0.3
        _ev("t2", tool="get_A", confidence=0.45),  # 0.3-0.5
        _ev("t3", tool="get_A", confidence=0.65),  # 0.5-0.7
        _ev("t4", tool="get_A", confidence=0.85),  # 0.7-0.9
        _ev("t5", tool="get_A", confidence=0.99),  # 0.9-1.0
        _ev("t6", tool="get_A", confidence=1.0),   # also 0.9-1.0
    ]
    dist = extract_confidence_distribution(events)
    by_bucket = {r["bucket"]: r["picks"] for r in dist}
    assert by_bucket["0.0-0.3"] == 1
    assert by_bucket["0.3-0.5"] == 1
    assert by_bucket["0.5-0.7"] == 1
    assert by_bucket["0.7-0.9"] == 1
    assert by_bucket["0.9-1.0"] == 2  # 0.99 and 1.0 both fit here


def test_confidence_distribution_negation_rate_per_bucket():
    """Low-conf bucket with high negation rate → threshold tuning signal."""
    events = []
    # 0.3-0.5 bucket: 4 picks, 3 negations (75% rate)
    for i in range(4):
        events.append(_ev(f"2026-05-17T10:0{i}:00", tool="get_X", confidence=0.4))
        events.append(_ev(f"2026-05-17T10:0{i}:30", is_negation=(i < 3)))
    # 0.7-0.9 bucket: 4 picks, 0 negations (0% rate)
    for i in range(4):
        events.append(_ev(f"2026-05-17T11:0{i}:00", tool="get_X", confidence=0.85))
        events.append(_ev(f"2026-05-17T11:0{i}:30", is_negation=False))

    dist = extract_confidence_distribution(events)
    low = next(r for r in dist if r["bucket"] == "0.3-0.5")
    high = next(r for r in dist if r["bucket"] == "0.7-0.9")
    assert low["negation_rate"] == 0.75
    assert high["negation_rate"] == 0.0


def test_confidence_distribution_empty_buckets_have_zero_rate():
    """No data in a bucket → picks=0, rate=0 (not NaN)."""
    dist = extract_confidence_distribution([])
    for row in dist:
        assert row["picks"] == 0
        assert row["negation_rate"] == 0.0


# --------------------------------------------------------------------------
# extract_tool_coverage
# --------------------------------------------------------------------------


def test_tool_coverage_picked_vs_unpicked():
    all_tools = {"get_A", "get_B", "get_C", "get_D"}
    events = [_ev("t1", tool="get_A"), _ev("t2", tool="get_B")]
    cov = extract_tool_coverage(events, all_tools)
    assert cov["picked_count"] == 2
    assert cov["unpicked_count"] == 2
    assert cov["coverage_pct"] == 50.0
    assert sorted(cov["picked"]) == ["get_A", "get_B"]
    assert sorted(cov["unpicked"]) == ["get_C", "get_D"]


def test_tool_coverage_ignores_unknown_picked():
    """Tool picked but not in all_tool_ids set (e.g., removed from registry)
    should NOT count as covered."""
    all_tools = {"get_A", "get_B"}
    events = [_ev("t1", tool="get_C")]  # C not in registry
    cov = extract_tool_coverage(events, all_tools)
    assert cov["picked_count"] == 0
    assert cov["unpicked_count"] == 2


def test_tool_coverage_empty_registry():
    """No tools at all → coverage_pct = 0 (no division by zero)."""
    cov = extract_tool_coverage([], set())
    assert cov["coverage_pct"] == 0.0


# --------------------------------------------------------------------------
# render_markdown_report — smoke tests
# --------------------------------------------------------------------------


def test_render_report_includes_all_sections():
    md = render_markdown_report(
        total_events=10,
        today="2026-05-17",
        negation_rates=[NegationRateRow("get_A", picks=5, negation_count=2)],
        query_patterns=[{
            "query": "kolika km", "total_occurrences": 4,
            "distinct_tools": 2,
            "tool_distribution": {"get_A": 3, "get_B": 1},
        }],
        confidence_dist=extract_confidence_distribution([]),
        tool_coverage={"picked_count": 1, "unpicked_count": 1,
                       "coverage_pct": 50.0,
                       "picked": ["get_A"], "unpicked": ["get_B"]},
    )
    assert "Active learning report" in md
    assert "2026-05-17" in md
    assert "negation rate" in md.lower()
    assert "Confidence distribution" in md
    assert "Tool coverage" in md
    assert "`get_A`" in md
    assert "kolika km" in md


def test_render_report_empty_inputs_does_not_crash():
    md = render_markdown_report(
        total_events=0,
        today="2026-05-17",
        negation_rates=[],
        query_patterns=[],
        confidence_dist=extract_confidence_distribution([]),
        tool_coverage=None,
    )
    assert "0" in md  # total_events
    assert "Nema podataka" in md or "Nema query" in md


def test_render_report_caps_unpicked_list_at_50():
    """Long unpicked list shows first 50 + '+N more'."""
    unpicked = [f"tool_{i}" for i in range(100)]
    md = render_markdown_report(
        total_events=1,
        today="2026-05-17",
        negation_rates=[],
        query_patterns=[],
        confidence_dist=extract_confidence_distribution([]),
        tool_coverage={"picked_count": 0, "unpicked_count": 100,
                       "coverage_pct": 0.0,
                       "picked": [], "unpicked": unpicked},
    )
    assert "+50 more" in md  # 100 - 50
    assert "tool_0" in md
    assert "tool_49" in md
    # tool_50 onwards NOT explicitly listed
    assert "tool_60" not in md


# --------------------------------------------------------------------------
# Integration: realistic mixed sequence
# --------------------------------------------------------------------------


def test_realistic_sequence_produces_actionable_insights():
    """End-to-end: synthetic week of telemetry with known patterns.
    Verifies the extractors find the patterns we planted."""
    events = []
    # Plant: tool_A picked 5 times, 4 followed by negation (80% bad)
    for i in range(5):
        events.append(_ev(f"2026-05-17T0{i}:00:00",
                          query="moja km", tool="tool_A", confidence=0.4))
        events.append(_ev(f"2026-05-17T0{i}:00:30",
                          is_negation=(i < 4)))
    # Plant: tool_B picked 10 times, never negated (clean)
    for i in range(10):
        events.append(_ev(f"2026-05-18T0{i}:00:00",
                          query=f"q{i}", tool="tool_B", confidence=0.85))

    neg_rates = extract_negation_per_tool(events)
    # tool_A worst
    assert neg_rates[0].tool_id == "tool_A"
    assert neg_rates[0].rate == 0.8
    # tool_B clean
    assert neg_rates[-1].tool_id == "tool_B"
    assert neg_rates[-1].rate == 0.0

    # Confidence dist shows 0.3-0.5 bucket has high neg rate
    dist = extract_confidence_distribution(events)
    low = next(r for r in dist if r["bucket"] == "0.3-0.5")
    high = next(r for r in dist if r["bucket"] == "0.7-0.9")
    assert low["negation_rate"] > high["negation_rate"]
