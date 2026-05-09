"""Unit tests for negation_handler."""
from __future__ import annotations

import pytest

from services.v2.negation_handler import detect, NegationResult


@pytest.mark.parametrize("text", [
    "nemoj rezervirati",
    "nemoj otkazati",
    "nemoj obrisati",
    "ne, otkaži",
    "ne, odustani",
    "odustajem",
    "zaboravi to",
    "zaboravi rezervaciju",
    "cancel that",
    "nevermind",
    "nema veze",
])
def test_negation_detected(text):
    out = detect(text)
    assert out.detected is True
    assert out.confidence > 0.5
    assert out.response


@pytest.mark.parametrize("text", [
    "rezerviraj auto",
    "kolika km",
    "obriši rezervaciju",
    "Bok",
    "Da",
    "Ne",  # bare "Ne" — handled by pending-mutation parser, not negation
    "ne znam",  # not a negation of action
])
def test_no_negation(text):
    out = detect(text)
    assert out.detected is False


def test_empty_query():
    assert detect("").detected is False


def test_dataclass_defaults():
    r = NegationResult(detected=False)
    assert r.confidence == 0.0
    assert r.response == ""
