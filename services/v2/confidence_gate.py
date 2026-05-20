"""L5 — Confidence Gate.

Pure function — RouterResult → GateDecision. Decides whether the LLM
router's output is trustworthy enough to execute, or fall back to a
clarify/fallback message.

Decision rules (post-Phase-4):
  HIGH (execute):   anchor_score >= 0.78  AND  confidence >= 0.7  AND  no disagreement
  MEDIUM (confirm): anchor_score >= 0.65  AND  confidence >= 0.5
  LOW (fallback):   anything else — surface top-3 candidates as clarify cards

`disagreement` is derived from RouterResult.top_candidates — if the LLM's
picked tool is not the anchor top-1, treat as disagreement and downgrade.

Fallback strategy (CLAUDE.md §1.2 fail loud):
  - top-3 candidates rendered as Croatian text "Razmišljaš li o ovome:"
  - generic "kontaktiraj managera" message when no signal at all
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from services.router.llm_router import RouterResult


# Thresholds — tuned to be conservative defaults. Production traffic can
# refine these once we have labelled-outcome telemetry.
MIN_ANCHOR_HIGH = 0.78
MIN_CONF_HIGH = 0.7
MIN_ANCHOR_MEDIUM = 0.65
MIN_CONF_MEDIUM = 0.5

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
    fallback_candidates: tuple = ()  # tuple of (tool_id, score) tuples


def decide(router_result: "RouterResult", identity_known: bool = True) -> GateDecision:
    """RouterResult → GateDecision.

    Caller routes:
      EXECUTE              → straight to L7 (mutations still bounce L6)
      EXECUTE_WITH_CONFIRM → L6 with single confirm
      FALLBACK             → return fallback_message to user
    """
    r = router_result

    if r.error:
        return GateDecision(
            decision=DECISION_FALLBACK,
            fallback_message=_generic_fallback(),
            log_reason=f"error:{r.error}",
        )

    if not r.tool_id:
        return GateDecision(
            decision=DECISION_FALLBACK,
            fallback_message=_generic_fallback(),
            log_reason="no_tool",
        )

    # Disagreement = LLM picked something other than anchor #1
    anchor_top_id = (
        r.top_candidates[0][0] if r.top_candidates else None
    )
    disagreement = bool(anchor_top_id and r.tool_id != anchor_top_id)

    anchor_ok_high = r.anchor_score >= MIN_ANCHOR_HIGH
    conf_ok_high = r.confidence >= MIN_CONF_HIGH

    if anchor_ok_high and conf_ok_high and not disagreement:
        return GateDecision(
            decision=DECISION_EXECUTE,
            log_reason=(
                f"high_conf anchor={r.anchor_score:.2f} conf={r.confidence:.2f}"
            ),
        )

    if r.anchor_score >= MIN_ANCHOR_MEDIUM and r.confidence >= MIN_CONF_MEDIUM:
        return GateDecision(
            decision=DECISION_EXECUTE_WITH_CONFIRM,
            log_reason=(
                f"medium_conf anchor={r.anchor_score:.2f} "
                f"conf={r.confidence:.2f} disagree={disagreement}"
            ),
        )

    # Low: bail out. Surface top-3 candidates for clarify-cards UX.
    fallback_cands = tuple(r.top_candidates[:3]) if r.top_candidates else ()
    return GateDecision(
        decision=DECISION_FALLBACK,
        fallback_message=_context_fallback(r),
        fallback_candidates=fallback_cands,
        log_reason=(
            f"low_conf anchor={r.anchor_score:.2f} conf={r.confidence:.2f}"
        ),
    )


def _generic_fallback() -> str:
    return (
        "Nisam siguran kako odgovoriti na ovo pitanje. "
        "Pokušaj pitati npr. \"koji je moj auto\", \"moja kilometraža\", "
        "ili \"rezerviraj auto sutra\". Ili kontaktiraj svog managera."
    )


def _context_fallback(router_result: "RouterResult") -> str:
    """Render top-3 candidates as Croatian text fallback."""
    if not router_result.top_candidates:
        return _generic_fallback()
    if router_result.anchor_score < 0.5:
        return _generic_fallback()
    top = router_result.top_candidates[:3]
    bullets = "\n".join(f"• {tid}" for tid, _ in top)
    return (
        "Nisam siguran točno što tražiš. Razmisliš li o ovome:\n"
        f"{bullets}\n"
        "— odgovori s opisom što ti treba pa ću pokušati ponovo."
    )
