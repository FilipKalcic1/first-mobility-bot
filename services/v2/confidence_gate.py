"""L5 — Confidence Gate.

Decides whether L3 RecognitionResult is trustworthy enough to execute,
or whether to bail out with the safe "Nisam siguran" fallback.

Triple AND criterion (Council Pass 4 DEVIL critique on calibration):
  - anchor_score >= MIN_ANCHOR    (0.78)
  - rationale len >= MIN_RATIONALE (30 chars — short = lazy)
  - llm_confidence >= MIN_LLM_CONF (0.7)
  - AND no disagreement (anchor #1 vs LLM choice)

Three outcome zones:
  HIGH       all 4 pass   → execute (mutation gate L6 still applies)
  MEDIUM     anchor + rationale OK, conf 0.5-0.7 OR disagreement → execute
                                                                with confirm
  LOW        anything below                                       → fallback

Fallback message strategy (CLAUDE.md §1.2 fail loud):
  - Top-3 cards via clarify_ui (preferred): user picks 1/2/3 with single tap.
    Empirical: A+B+C clarify-rescuable = 43.5% on entity_hybrid config —
    user-perceived accuracy ~2x of strict 1-shot.
  - Generic text fallback (when no candidates available): polite ask to
    reformulate or contact manager.
"""
from __future__ import annotations

from dataclasses import dataclass


# Thresholds.
MIN_ANCHOR = 0.78
MIN_RATIONALE_LEN = 30
MIN_LLM_CONF = 0.7

# Decision kinds.
DECISION_EXECUTE = "execute"
DECISION_EXECUTE_WITH_CONFIRM = "execute_with_confirm"
DECISION_FALLBACK = "fallback"


@dataclass(frozen=True)
class GateDecision:
    decision: str
    fallback_message: str = ""
    log_reason: str = ""
    # Top-3 candidates available when low-conf and we have something to show.
    # Caller may render via clarify_ui (Infobip interactive list) or fall
    # back to plain text if interactive messaging is unavailable.
    fallback_candidates: tuple = ()  # tuple of ToolCandidate-like (tool_id, purpose)


def decide(recognition_result, identity_known: bool = True) -> GateDecision:
    """Pure function — RecognitionResult → GateDecision.

    Caller routes:
      EXECUTE              → straight to L7 (mutations still bounce L6)
      EXECUTE_WITH_CONFIRM → L6 with single confirm
      FALLBACK             → return fallback_message to user
    """
    r = recognition_result

    if r.error:
        return GateDecision(
            decision=DECISION_FALLBACK,
            fallback_message=_generic_fallback(),
            log_reason=f"error:{r.error}",
        )

    if not r.tool_id and not r.flow_name:
        return GateDecision(
            decision=DECISION_FALLBACK,
            fallback_message=_generic_fallback(),
            log_reason="no_tool_or_flow",
        )

    rationale_ok = len(r.rationale or "") >= MIN_RATIONALE_LEN
    anchor_ok = r.anchor_score >= MIN_ANCHOR
    llm_ok = r.llm_confidence >= MIN_LLM_CONF
    no_disagreement = not r.disagreement

    if anchor_ok and rationale_ok and llm_ok and no_disagreement:
        return GateDecision(
            decision=DECISION_EXECUTE,
            log_reason=(
                f"high_conf anchor={r.anchor_score:.2f} "
                f"llm={r.llm_confidence:.2f}"
            ),
        )

    # Medium zone: anchor reasonable AND rationale present, but
    # something else weak (disagreement, lower llm_conf, or borderline).
    if r.anchor_score >= 0.65 and rationale_ok and r.llm_confidence >= 0.5:
        return GateDecision(
            decision=DECISION_EXECUTE_WITH_CONFIRM,
            log_reason=(
                f"medium_conf anchor={r.anchor_score:.2f} "
                f"llm={r.llm_confidence:.2f} "
                f"disagree={r.disagreement}"
            ),
        )

    # Low: bail out. Surface top-3 candidates for clarify-cards UX
    # (caller decides whether to render as interactive list or text).
    fallback_cands = tuple(r.candidates[:3]) if r.candidates else ()
    return GateDecision(
        decision=DECISION_FALLBACK,
        fallback_message=_context_fallback(r),
        fallback_candidates=fallback_cands,
        log_reason=(
            f"low_conf anchor={r.anchor_score:.2f} "
            f"llm={r.llm_confidence:.2f}"
        ),
    )


def _generic_fallback() -> str:
    return (
        "Nisam siguran kako odgovoriti na ovo pitanje. "
        "Pokušaj pitati npr. \"koji je moj auto\", \"moja kilometraža\", "
        "ili \"rezerviraj auto sutra\". Ili kontaktiraj svog managera."
    )


def _context_fallback(recognition_result) -> str:
    """Render Top-3 cards as text (universal fallback).

    For Infobip interactive list rendering, caller should use
    clarify_ui.render_infobip_list_message(...) directly with
    GateDecision.fallback_candidates instead of relying on this text.
    """
    if not recognition_result.candidates:
        return _generic_fallback()
    if recognition_result.anchor_score < 0.5:
        return _generic_fallback()
    # Render via clarify_ui for consistency. Lazy-import to avoid a
    # cycle at module import time.
    try:
        from services.v2.clarify_ui import build_clarify_options, render_text
        opts = build_clarify_options(recognition_result.candidates, max_cards=3)
        return render_text(opts, header="Razumio sam da ti treba jedno od:")
    except Exception:  # noqa: BLE001 — defensive, fall back to bullets
        top = recognition_result.candidates[:3]
        bullets = "\n".join(
            f"• {c.purpose[:80]}" for c in top if c.purpose
        )
        return (
            "Nisam siguran točno što tražiš. Razmisliš li o ovome:\n"
            f"{bullets}\n"
            "— odgovori s opisom što ti treba pa ću pokušati ponovo."
        )
