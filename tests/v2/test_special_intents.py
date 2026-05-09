"""Tests for v2 L1 — special intents."""
from __future__ import annotations

import pytest

from services.v2.special_intents import detect_special_intent


# --------------------------------------------------------------------------
# Welcome (first contact)
# --------------------------------------------------------------------------


def test_first_contact_welcomes_with_name_and_vehicle():
    m = detect_special_intent(
        "bok",
        is_first_contact=True,
        first_name="Marko",
        vehicle_name="VW Golf BG-1234",
    )
    assert m is not None
    assert m.intent == "WELCOME"
    assert "Marko" in m.response
    assert "VW Golf BG-1234" in m.response


def test_first_contact_welcomes_without_vehicle_falls_back():
    m = detect_special_intent(
        "kolika mi je km",
        is_first_contact=True,
        first_name="Ana",
    )
    assert m.intent == "WELCOME"
    assert "Ana" in m.response
    # Should still mention what bot can do
    assert "kilometraž" in m.response.lower() or "rezervacij" in m.response.lower()


def test_first_contact_without_name_still_works():
    m = detect_special_intent(
        "hej", is_first_contact=True
    )
    assert m.intent == "WELCOME"
    assert "Bok" in m.response


# --------------------------------------------------------------------------
# Returning-user greetings (NOT first contact)
# --------------------------------------------------------------------------


def test_returning_user_bok_short_ack():
    m = detect_special_intent("bok", is_first_contact=False, first_name="X")
    assert m is not None
    assert m.intent == "GREETING"
    # Short ack, NOT full welcome
    assert len(m.response) < 80
    assert "X" in m.response


def test_returning_user_pozdrav_acked():
    m = detect_special_intent(
        "Pozdrav!", is_first_contact=False, first_name="X"
    )
    assert m.intent == "GREETING"


# --------------------------------------------------------------------------
# GDPR (legal precedence — fires even on first contact)
# --------------------------------------------------------------------------


def test_gdpr_delete_supersedes_welcome():
    m = detect_special_intent(
        "obriši me iz sustava", is_first_contact=True, first_name="X"
    )
    assert m.intent == "GDPR_DELETE"
    assert any(
        s["action"] == "audit_log_gdpr_delete" for s in m.side_effects
    )


def test_gdpr_delete_diacritic_insensitive():
    m = detect_special_intent("OBRIŠI ME", is_first_contact=False)
    assert m.intent == "GDPR_DELETE"


def test_gdpr_delete_english():
    m = detect_special_intent("delete my data please", is_first_contact=False)
    assert m.intent == "GDPR_DELETE"


def test_gdpr_export():
    m = detect_special_intent(
        "izvezi moje podatke", is_first_contact=False
    )
    assert m.intent == "GDPR_EXPORT"
    assert any(
        s["action"] == "audit_log_gdpr_export" for s in m.side_effects
    )


# --------------------------------------------------------------------------
# Handover
# --------------------------------------------------------------------------


def test_handover_real_person():
    m = detect_special_intent(
        "trebam pomoć čovjeka",
        is_first_contact=False,
    )
    assert m.intent == "HANDOVER"
    assert any(
        s["action"] == "queue_human_handover" for s in m.side_effects
    )


def test_handover_english():
    m = detect_special_intent(
        "I want to talk to a human", is_first_contact=False
    )
    assert m.intent == "HANDOVER"


# --------------------------------------------------------------------------
# Help
# --------------------------------------------------------------------------


def test_help_lists_capabilities():
    m = detect_special_intent("pomoć", is_first_contact=False)
    assert m.intent == "HELP"
    # Lists at least 4 capabilities
    assert m.response.count("•") >= 4


def test_help_what_can_you_do():
    m = detect_special_intent("što sve možeš", is_first_contact=False)
    assert m.intent == "HELP"


# --------------------------------------------------------------------------
# Non-special queries (should return None for routing)
# --------------------------------------------------------------------------


def test_non_special_query_returns_none():
    assert detect_special_intent(
        "koji je moj auto", is_first_contact=False
    ) is None


def test_random_text_returns_none():
    assert detect_special_intent("kolika mi je kilometraža", is_first_contact=False) is None


# --------------------------------------------------------------------------
# Word-boundary safety (avoid substring false positives)
# --------------------------------------------------------------------------


def test_help_substring_false_positive_avoided():
    # "pomoć" is in HELP triggers. "pomocna" / "kompromitirao" should not match
    m = detect_special_intent(
        "kompromitirao sam podatke", is_first_contact=False
    )
    # Either None, or HELP isn't matched
    assert m is None or m.intent != "HELP"


def test_human_in_unrelated_word_not_matched():
    # "human" trigger shouldn't fire on "humanitarian"
    m = detect_special_intent(
        "humanitarne donacije", is_first_contact=False
    )
    assert m is None or m.intent != "HANDOVER"


# --------------------------------------------------------------------------
# Longest-match (tie-breaker)
# --------------------------------------------------------------------------


def test_longer_phrase_wins_over_shorter_match():
    # "pomoć" is HELP; "trebam pomoć čovjeka" is HANDOVER (longer).
    m = detect_special_intent(
        "trebam pomoć čovjeka", is_first_contact=False
    )
    assert m.intent == "HANDOVER"


# --------------------------------------------------------------------------
# Empty / edge inputs
# --------------------------------------------------------------------------


def test_empty_query_returns_none_when_not_first_contact():
    assert detect_special_intent("", is_first_contact=False) is None


def test_empty_query_first_contact_still_welcomes():
    # Even an empty first message → welcome (rare but possible)
    m = detect_special_intent("", is_first_contact=True, first_name="X")
    assert m is not None
    assert m.intent == "WELCOME"
