"""Unit tests for registration-plate detection (Filip 2026-05-23).

Deterministic, no Azure. Covers Damir's fleet plate format plus the
false-positive guards (mileage numbers like "za 30500 km" must NOT read as
a plate, self-questions have no plate).
"""
from __future__ import annotations

from services.v2.entity_detector import detect_registration


def test_detects_plate_contiguous():
    assert detect_registration("daj mi km za škodu DA533") == "DA533"


def test_detects_plate_with_trailing_letter():
    assert detect_registration("tko vozi DA053F") == "DA053F"


def test_detects_plate_lowercase_input():
    assert detect_registration("km za da533") == "DA533"


def test_detects_plate_hyphenated():
    assert detect_registration("vozilo ZG-1234-AB") == "ZG1234AB"


def test_no_plate_in_self_query():
    assert detect_registration("kolika mi je km") is None


def test_mileage_action_not_a_false_plate():
    # "za 30500 km" must NOT be read as plate "ZA30500" (space breaks it).
    assert detect_registration("unesi 30500 km") is None
    assert detect_registration("za 30500 km") is None


def test_empty_query():
    assert detect_registration("") is None
    assert detect_registration(None) is None  # type: ignore[arg-type]
