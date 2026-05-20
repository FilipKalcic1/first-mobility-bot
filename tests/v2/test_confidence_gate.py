"""Tests for L5 confidence_gate.decide()."""
from __future__ import annotations

from dataclasses import dataclass, field

from services.v2.confidence_gate import (
    DECISION_EXECUTE, DECISION_EXECUTE_WITH_CONFIRM, DECISION_FALLBACK,
    decide,
)


# Lightweight stand-in for services.router.llm_router.RouterResult.
# Gate reads tool_id, error, anchor_score, confidence, top_candidates.
@dataclass
class RouterResultStub:
    tool_id: str | None = "get_X"
    params: dict = field(default_factory=dict)
    confidence: float = 0.9
    anchor_score: float = 0.85
    top_candidates: list = field(default_factory=list)
    error: str | None = None


def _r(**kw):
    return RouterResultStub(**kw)


def test_high_confidence_executes():
    g = decide(_r(anchor_score=0.85, confidence=0.9))
    assert g.decision == DECISION_EXECUTE


def test_low_anchor_falls_back():
    g = decide(_r(anchor_score=0.4, confidence=0.9))
    assert g.decision == DECISION_FALLBACK


def test_low_confidence_falls_back_via_medium():
    g = decide(_r(anchor_score=0.85, confidence=0.4))
    # anchor ok, conf < 0.5 medium-floor → fallback
    assert g.decision == DECISION_FALLBACK


def test_disagreement_downgrades_to_confirm():
    # LLM picked X but anchor top-1 was Y → disagreement → medium zone
    g = decide(_r(
        tool_id="get_X",
        anchor_score=0.7,
        confidence=0.7,
        top_candidates=[("get_Y", 0.8), ("get_X", 0.7)],
    ))
    assert g.decision == DECISION_EXECUTE_WITH_CONFIRM


def test_no_tool_falls_back():
    g = decide(_r(tool_id=None))
    assert g.decision == DECISION_FALLBACK


def test_error_falls_back():
    g = decide(_r(error="llm_error"))
    assert g.decision == DECISION_FALLBACK


def test_fallback_includes_top_candidates_when_anchor_decent():
    g = decide(_r(
        anchor_score=0.55,
        confidence=0.4,
        top_candidates=[("get_A", 0.55), ("get_B", 0.5), ("get_C", 0.45)],
    ))
    assert g.decision == DECISION_FALLBACK
    assert "get_A" in g.fallback_message


def test_fallback_generic_when_anchor_too_low():
    g = decide(_r(
        anchor_score=0.3,
        confidence=0.2,
        top_candidates=[("get_X", 0.3)],
    ))
    assert g.decision == DECISION_FALLBACK
    assert "kontaktiraj" in g.fallback_message.lower()


def test_medium_zone_executes_with_confirm():
    g = decide(_r(anchor_score=0.7, confidence=0.6))
    assert g.decision == DECISION_EXECUTE_WITH_CONFIRM


def test_fallback_candidates_tuple_surface():
    g = decide(_r(
        anchor_score=0.4, confidence=0.3,
        top_candidates=[("get_A", 0.4), ("get_B", 0.35)],
    ))
    assert g.decision == DECISION_FALLBACK
    assert g.fallback_candidates == (("get_A", 0.4), ("get_B", 0.35))
