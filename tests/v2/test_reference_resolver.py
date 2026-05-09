"""Unit tests for reference_resolver."""
from __future__ import annotations

import pytest

from services.v2.reference_resolver import (
    ReferenceResolution, detect_reference, resolve,
)


# ---- detect_reference ----

@pytest.mark.parametrize("text", [
    "ono prvo obriši",
    "to obriši",
    "isto i za prošli mjesec",
    "isto za sutra",
    "a što s onom drugom",
    "a sto sa proslim mjesecom",
    "još jedna",
    "i to isto",
])
def test_detects_reference_marker(text):
    assert detect_reference(text) is True


@pytest.mark.parametrize("text", [
    "kolika km",
    "obriši rezervaciju za sutra",
    "trebam auto",
    "Bok",
    "moje rezervacije",
    "pokaži mi km",
])
def test_no_reference_marker(text):
    assert detect_reference(text) is False


def test_empty_query():
    assert detect_reference("") is False


# ---- resolve ----

def test_no_marker_returns_not_detected():
    out = resolve("kolika km", recent_turns=[])
    assert out.detected is False


def test_marker_no_history_returns_clarify():
    out = resolve("to obriši", recent_turns=[])
    assert out.detected is True
    assert out.clarify_question is not None
    assert out.reason == "no_history"


def test_marker_with_history_rewrites():
    turns = [
        {"user": "moje rezervacije", "bot": "Imaš 3 rezervacije: ..."},
    ]
    out = resolve("to obriši", recent_turns=turns)
    assert out.detected is True
    assert out.rewritten_query
    assert "to obriši" in out.rewritten_query
    assert "kontekst" in out.rewritten_query.lower()
    assert out.reason == "resolved_with_context"


def test_history_with_only_user_no_bot():
    """If history has user turn but no bot response, clarify."""
    turns = [{"user": "moje rezervacije", "bot": ""}]
    out = resolve("to obriši", turns)
    assert out.detected is True
    assert out.clarify_question is not None
    assert out.reason == "no_bot_turn"


def test_picks_most_recent_bot_turn():
    turns = [
        {"user": "stari upit", "bot": "stari odgovor"},
        {"user": "noviji upit", "bot": "noviji odgovor"},
    ]
    out = resolve("isto za sutra", turns)
    assert out.detected
    assert "noviji" in out.rewritten_query


def test_dataclass_defaults():
    r = ReferenceResolution(detected=False)
    assert r.rewritten_query == ""
    assert r.clarify_question is None
    assert r.reason == ""
