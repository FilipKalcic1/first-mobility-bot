"""L6 — Mutation Safeguard. Single-confirm policy (Filip 2026-05-16).

Doctrine: **always exactly ONE Da/Ne confirm for any mutation.** Three-turn
double-confirm is infeasible UX (user closes WhatsApp mid-flow → bot stuck).

Decision is PURELY method-based — uniform across all 950 tools, zero per-tool
data (Filip 2026-05-27, "maksimalna uniformnost"):
  AUTO     GET only (read-only)
  CONFIRM  POST/PUT/PATCH/DELETE — single Da/Ne. DELETE gets stronger
           "TRAJNO BRISANJE" wording (still method-derived, so still uniform).

Value / field verification is NOT this gate's job — the engine's echo
("Provjeri prije slanja: …") shows the user every value it will send before
they confirm, uniformly for every tool. Earlier per-tool special cases
(out-of-range range checks, a hardcoded critical-fields list) were removed:
they were the one non-uniform, data-dependent part, only ever changed the
WORDING (never the decision — every write confirms regardless), and the echo
covers the same ground for all tools. Enum / business-rule validation is the
backend's job (reactive 4xx → ApiErrorTranslator), not the gate's.

Outcome:
  AUTO     caller executes immediately (GET only)
  CONFIRM  caller renders single confirm dialog, waits for "Da/Ne"
"""
from __future__ import annotations

from dataclasses import dataclass


DECISION_AUTO = "auto"
DECISION_CONFIRM = "confirm"


@dataclass(frozen=True)
class MutationDecision:
    decision: str               # DECISION_*
    confirm_message: str = ""   # rendered Croatian, ready to send
    log_reason: str = ""


def decide_mutation(method: str, entity_label: str = "") -> MutationDecision:
    """Decide the gate level from the HTTP method alone. Pure function.

    Per CLAUDE.md §1.3 — every POST/PUT/PATCH/DELETE requires explicit Da/Ne
    confirmation. AUTO is permitted only for GET (read-only).
    """
    method_upper = (method or "").upper()
    if method_upper == "GET":
        return MutationDecision(decision=DECISION_AUTO, log_reason="read_only")

    # DELETE → single confirm with strong wording (irreversible action).
    if method_upper == "DELETE":
        return MutationDecision(
            decision=DECISION_CONFIRM,
            confirm_message=(
                f"⚠️ TRAJNO BRISANJE: sigurno želiš obrisati "
                f"{entity_label or 'ovaj zapis'}? Ova akcija je nepovratna. "
                "Odgovori DA za potvrdu, NE za otkazivanje."
            ),
            log_reason="delete_single_confirm",
        )

    # All other writes (POST/PUT/PATCH) → single confirm.
    return MutationDecision(
        decision=DECISION_CONFIRM,
        confirm_message=f"Potvrđuješ {entity_label or 'akciju'}? Da/Ne",
        log_reason=f"{method_upper.lower()}_single_confirm",
    )
