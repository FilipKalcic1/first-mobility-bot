"""Unit tests for multi_intent_detector."""
from __future__ import annotations

import pytest

from services.v2.multi_intent_detector import detect, MultiIntentResult


# ---- Multi-intent detected ----

@pytest.mark.parametrize("text", [
    "pokaži km i rezerviraj sutra",
    "obriši rezervaciju i unesi 30000 km",
    "pokaži rezervacije, te otkaži onu za sutra",
    "rezerviraj auto pa pokaži moje km",
    "unesi 30000 km i pošalji email Marku",
])
def test_multi_intent_detected(text):
    out = detect(text)
    assert out.detected is True
    assert "Jednu po jednu" in out.clarify_message


def test_clarify_lists_parts():
    out = detect("pokaži km i rezerviraj sutra")
    assert out.detected
    assert len(out.parts) >= 2


# ---- Single-intent (single-category) NOT flagged ----

@pytest.mark.parametrize("text", [
    "kolika km",
    "moje rezervacije",
    "pokaži km",
    "pokaži km i registraciju",  # multiple FIELDS, single read intent
    "obriši rezervaciju za sutra",
    "udario sam autom",
    "Bok",
    "trebam auto sutra",
])
def test_single_intent_not_flagged(text):
    out = detect(text)
    assert out.detected is False


def test_empty_query_not_flagged():
    assert detect("").detected is False
    assert detect("   ").detected is False


# ---- Edge: words like "i" inside a single-intent ----

def test_short_word_after_i_not_split():
    """'i ja' should not split — too short to be action verb."""
    out = detect("rezerviraj i ja sutra")
    # Action verb 'rezerviraj' present, but no second action category
    assert out.detected is False


# ---- Robustness ----

def test_dataclass_defaults():
    r = MultiIntentResult(detected=False)
    assert r.parts == []
    assert r.clarify_message == ""


def test_clarify_message_includes_first_part():
    out = detect("pokaži km i rezerviraj sutra")
    assert out.detected
    # First part should be in the message
    assert "pokaži" in out.clarify_message.lower() or "km" in out.clarify_message.lower()


# ---- Faza 4 Bug #3 (Filip 2026-05-17): empty parts fragments ----

def test_trailing_conjunction_does_not_trigger_multi_intent():
    """'pokaži km i' has only 1 action category → already returns False
    via category check. Defensive: ensure no crash, no fake clarify."""
    out = detect("pokaži km i")
    assert out.detected is False


@pytest.mark.parametrize("text", [
    # Two action verbs separated by ", i" then trailing junk
    "pokaži km, i  ",
    "pokaži km, i ,",
    # Double conjunction (typo'd input)
    "pokaži km i i rezerviraj",
])
def test_malformed_conjunction_input_does_not_crash(text):
    """Whatever malformed input, detector must not raise and must not
    emit a clarify message with blank parts."""
    out = detect(text)
    # Either correctly detected (real multi-intent) OR not detected — but
    # if detected, parts must be ≥2 and non-empty.
    if out.detected:
        assert len(out.parts) >= 2
        for p in out.parts:
            assert p.strip(), f"empty part in {out.parts!r}"
