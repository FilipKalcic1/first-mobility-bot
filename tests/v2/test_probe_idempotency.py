"""Tests for the pure logic of scripts/probe_idempotency.py (no network)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "probe_idempotency", _REPO / "scripts" / "probe_idempotency.py"
)
pi = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pi)


def test_extract_list_envelopes():
    assert pi._extract_list([{"Id": 1}]) == [{"Id": 1}]
    assert pi._extract_list({"Result": [{"Id": 1}]}) == [{"Id": 1}]
    assert pi._extract_list({"Items": [{"Id": 2}]}) == [{"Id": 2}]
    assert pi._extract_list({"Id": 9}) == [{"Id": 9}]   # single record
    assert pi._extract_list({}) == []
    assert pi._extract_list("nope") == []


def test_count_matching_marker_isolates_test_records():
    rows = [
        {"Description": "MARK", "Id": 1},
        {"Description": "other", "Id": 2},
        {"Description": "MARK", "Id": 3},
    ]
    assert pi._count_matching(rows, {"field": "Description", "value": "MARK"}) == 2
    assert pi._count_matching(rows, None) == 3  # no marker → total


def test_verdict_dedup_by_count_delta_one():
    v, _ = pi.verdict(0, 1, "A", "A", True, True)
    assert v == "DEDUP"


def test_verdict_dedup_by_same_id_even_if_count_noisy():
    # concurrent activity bumped count by 2, but both POSTs returned same id
    v, _ = pi.verdict(0, 2, "X", "X", True, True)
    assert v == "DEDUP"


def test_verdict_no_dedup_two_distinct_records():
    v, _ = pi.verdict(0, 2, "A", "B", True, True)
    assert v == "NO_DEDUP"


def test_verdict_inconclusive_when_post_failed():
    v, _ = pi.verdict(0, 0, None, None, True, False)
    assert v == "INCONCLUSIVE"


def test_example_config_is_valid_json_shape():
    ex = pi._EXAMPLE
    assert ex["create"]["body"]["AssigneeType"] == 1
    assert "{id}" in ex["delete"]["path"]
    assert ex["count"]["match"]["value"] == ex["create"]["body"]["Description"]
