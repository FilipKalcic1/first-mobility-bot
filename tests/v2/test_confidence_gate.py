"""Tests for L5 confidence_gate.decide()."""
from __future__ import annotations

from services.v2.confidence_gate import (
    DECISION_EXECUTE, DECISION_EXECUTE_WITH_CONFIRM, DECISION_FALLBACK,
    GateDecision, decide,
)
from services.v2.recognition import RecognitionResult, ToolCandidate


def _r(**kw):
    base = dict(
        tool_id="get_X", flow_name=None, params={},
        rationale="x" * 50, llm_confidence=0.9, anchor_score=0.85,
        combined_confidence=0.85, disagreement=False,
        candidates=[],
    )
    base.update(kw)
    return RecognitionResult(**base)


def test_high_confidence_executes():
    g = decide(_r(anchor_score=0.85, llm_confidence=0.9, rationale="dovoljno dugačak razlog za odluku alata XYZ"))
    assert g.decision == DECISION_EXECUTE


def test_low_anchor_falls_back():
    g = decide(_r(anchor_score=0.4, llm_confidence=0.9))
    assert g.decision == DECISION_FALLBACK


def test_low_llm_conf_falls_back_via_medium():
    g = decide(_r(anchor_score=0.85, llm_confidence=0.4))
    # anchor ok, rationale ok, llm low → falls into medium check
    # llm 0.4 < 0.5 medium-floor → fallback
    assert g.decision == DECISION_FALLBACK


def test_short_rationale_falls_back():
    g = decide(_r(rationale="kratko"))
    # short rationale fails high-conf gate AND medium gate (rationale_ok req)
    assert g.decision == DECISION_FALLBACK


def test_disagreement_downgrades_to_confirm_in_medium_zone():
    g = decide(_r(disagreement=True, anchor_score=0.7, llm_confidence=0.7,
                  rationale="dovoljno dugačak razlog za odabir alata X umjesto Y"))
    assert g.decision == DECISION_EXECUTE_WITH_CONFIRM


def test_no_tool_falls_back():
    g = decide(_r(tool_id=None, flow_name=None))
    assert g.decision == DECISION_FALLBACK


def test_error_falls_back():
    r = RecognitionResult(error="some_error", candidates=[])
    g = decide(r)
    assert g.decision == DECISION_FALLBACK


def test_context_fallback_includes_top_candidates_when_anchor_decent():
    candidates = [
        ToolCandidate(tool_id="get_A", anchor_score=0.55,
                      purpose="Dohvat liste vozila"),
        ToolCandidate(tool_id="get_B", anchor_score=0.5,
                      purpose="Pregled rezervacija"),
        ToolCandidate(tool_id="get_C", anchor_score=0.45,
                      purpose="Statistika troškova"),
    ]
    r = _r(anchor_score=0.55, llm_confidence=0.4,
           rationale="kratko", candidates=candidates)
    g = decide(r)
    assert g.decision == DECISION_FALLBACK
    # Should mention candidates
    assert "Dohvat liste vozila" in g.fallback_message


def test_context_fallback_generic_when_anchor_too_low():
    r = _r(anchor_score=0.3, llm_confidence=0.2, rationale="x")
    g = decide(r)
    assert g.decision == DECISION_FALLBACK
    # Generic fallback (no candidate bullets)
    assert "•" not in g.fallback_message


def test_fallback_exposes_candidates_for_clarify_ui():
    """Fallback decisions surface top-3 candidates so the engine can render
    Top-3 cards via clarify_ui (Infobip interactive list message)."""
    candidates = [
        ToolCandidate(tool_id="get_A", anchor_score=0.7, purpose="Vozilo info"),
        ToolCandidate(tool_id="get_B", anchor_score=0.65, purpose="Rezervacije"),
        ToolCandidate(tool_id="get_C", anchor_score=0.6, purpose="Kilometraža"),
        ToolCandidate(tool_id="get_D", anchor_score=0.55, purpose="Troškovi"),
    ]
    r = _r(anchor_score=0.7, llm_confidence=0.4,
           rationale="x", candidates=candidates)
    g = decide(r)
    assert g.decision == DECISION_FALLBACK
    # Top-3 candidates surfaced for clarify_ui rendering
    assert len(g.fallback_candidates) == 3
    assert g.fallback_candidates[0].tool_id == "get_A"
    assert g.fallback_candidates[2].tool_id == "get_C"


def test_fallback_no_candidates_when_recognition_had_none():
    """If recognition returned no candidates (E retrieval fail), fallback_candidates is empty."""
    r = _r(anchor_score=0.3, llm_confidence=0.2, rationale="x", candidates=[])
    g = decide(r)
    assert g.decision == DECISION_FALLBACK
    assert g.fallback_candidates == ()


def test_fallback_uses_clarify_ui_text_format():
    """Text fallback should use clarify_ui rendering (emoji-numbered list)."""
    candidates = [
        ToolCandidate(tool_id="get_VehicleCalendar", anchor_score=0.6,
                      purpose="Lista mojih rezervacija."),
        ToolCandidate(tool_id="delete_VehicleCalendar_id", anchor_score=0.55,
                      purpose="Otkaži pojedinačnu rezervaciju."),
        ToolCandidate(tool_id="put_VehicleCalendar_id", anchor_score=0.5,
                      purpose="Promijeni rezervaciju."),
    ]
    r = _r(anchor_score=0.6, llm_confidence=0.3,
           rationale="x", candidates=candidates)
    g = decide(r)
    assert g.decision == DECISION_FALLBACK
    # clarify_ui renders with emoji numbering
    assert "1️⃣" in g.fallback_message or "1." in g.fallback_message
