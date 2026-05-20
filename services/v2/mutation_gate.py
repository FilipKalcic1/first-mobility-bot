"""L6 — Mutation Safeguard. Single-confirm policy (Filip 2026-05-16).

Doctrine update: dropped DOUBLE-confirm for DELETE / critical fields /
out-of-range. Three-turn flow (delete request + Da + POTVRDA + Da) is
infeasible UX — user closes WhatsApp mid-flow → bot stuck in pending
state. **Always exactly ONE Da/Ne confirm for any mutation.**

Decision matrix (current):
  AUTO         GET only (read-only path)
  CONFIRM      POST/PUT/PATCH/DELETE (any) — single Da/Ne with full
               context. Confirm message strength scales with risk:
                 - normal write: "Potvrđuješ akciju?"
                 - DELETE: "⚠️ TRAJNO BRISANJE — siguran?"
                 - critical fields: "Mijenjaš kritične podatke — siguran?"
                 - out-of-range: "Vrijednost je neuobičajena — siguran?"

Previously DECISION_DOUBLE existed for the 2-step flow; removed entirely
along with downstream STAGE_DOUBLE_FIRST/SECOND in pending_mutation.py
and engine.py branches that handled them.

Outcome:
  AUTO         caller executes immediately (GET only)
  CONFIRM      caller renders single confirm dialog, waits for "Da/Ne"
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


DECISION_AUTO = "auto"
DECISION_CONFIRM = "confirm"


# Default "expected range" rules. Per-tool, per-param.
# Real production values come from config/mutation_ranges.json (per
# CLAUDE.md: data in JSON, not Python). For v2 POC we ship sensible
# defaults that match the curl primjeri from UPITI.txt.
_DEFAULT_RANGES = {
    "post_AddMileage": {
        "Value": {"min": 0, "max": 1_000_000, "max_jump_from_last": 5000},
    },
    "post_VehicleCalendar": {
        # Booking: from-time within next 90 days, to-time after from
        "from_time_max_days_ahead": 90,
        "to_time_after_from": True,
    },
}

# Critical fields per tool — changing these requires double-confirm.
_CRITICAL_FIELDS = {
    "patch_Vehicles_id": {"LicencePlate", "VIN"},
    "put_Vehicles_id":   {"LicencePlate", "VIN"},
    "put_VehicleContracts_id": {"YearlyMileageLimit", "ContractEnd"},
}


@dataclass(frozen=True)
class MutationDecision:
    decision: str               # DECISION_*
    confirm_message: str = ""   # rendered Croatian, ready to send
    log_reason: str = ""


def decide_mutation(
    tool_id: str,
    method: str,
    params: dict,
    last_known_values: Optional[dict] = None,
    entity_label: str = "",
) -> MutationDecision:
    """Decide mutation gate level. Pure function.

    Per CLAUDE.md §1.3 — every POST/PUT/PATCH/DELETE requires explicit
    Da/Ne confirmation. AUTO is permitted only for GET (read-only).
    """
    method_upper = (method or "").upper()
    if method_upper == "GET":
        return MutationDecision(
            decision=DECISION_AUTO, log_reason="read_only"
        )

    # DELETE → single confirm with strong wording (irreversible action)
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

    # Critical-field changes → single confirm with strong wording
    critical = _CRITICAL_FIELDS.get(tool_id, set())
    if critical and any(k in critical for k in (params or {}).keys()):
        return MutationDecision(
            decision=DECISION_CONFIRM,
            confirm_message=(
                f"⚠️ Mijenjaš kritične podatke o {entity_label or 'zapisu'}. "
                "Promjena se odmah primjenjuje. "
                "Odgovori DA za potvrdu, NE za otkazivanje."
            ),
            log_reason="critical_field_single_confirm",
        )

    # Out-of-range → single confirm with red-flag wording
    ranges = _DEFAULT_RANGES.get(tool_id) or {}
    out_of_range = _check_range(tool_id, params, ranges, last_known_values)
    if out_of_range:
        return MutationDecision(
            decision=DECISION_CONFIRM,
            confirm_message=(
                f"⚠️ Vrijednost je izvan očekivanih granica ({out_of_range}). "
                f"Sigurno potvrđuješ {entity_label or 'akciju'}? Da/Ne"
            ),
            log_reason=f"out_of_range_single_confirm:{out_of_range}",
        )

    # All other POST/PUT/PATCH → single confirm.
    # CLAUDE.md §1.3 mandate: NO mutation without explicit Da from user.
    return MutationDecision(
        decision=DECISION_CONFIRM,
        confirm_message=(
            f"Potvrđuješ {entity_label or 'akciju'}? Da/Ne"
        ),
        log_reason=f"{method_upper.lower()}_single_confirm",
    )


def _check_range(
    tool_id: str,
    params: dict,
    ranges: dict,
    last_known: Optional[dict],
) -> Optional[str]:
    """Return short description of first range violation, or None."""
    if not ranges:
        return None

    if tool_id == "post_AddMileage":
        v = (params or {}).get("Value")
        cfg = ranges.get("Value") or {}
        if v is None or not isinstance(v, (int, float)):
            return "vrijednost km nije broj"
        if v < cfg.get("min", 0) or v > cfg.get("max", 9_999_999):
            return "kilometraža izvan razumnih granica"
        last_km = (last_known or {}).get("last_mileage")
        if isinstance(last_km, (int, float)):
            jump = abs(v - last_km)
            limit = cfg.get("max_jump_from_last", 5000)
            if jump > limit:
                return (
                    f"skok od {jump} km u odnosu na zadnji "
                    f"unos (max {limit})"
                )
        return None

    # Generic fallthrough — extend per tool as needed.
    return None
