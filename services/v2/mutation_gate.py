"""L6 — Mutation Safeguard. Absolute confirmation gate per CLAUDE.md §1.3.

CLAUDE.md doctrine (overrides earlier "Council Pass 4" design):
    "Ne postoji POST/PUT/PATCH/DELETE bez explicit user 'Da' potvrde
    s konkretnim podacima u confirm message-u (entity name + context,
    ne ID)."

Decision matrix:
  AUTO         GET only (read-only path)
  CONFIRM      POST/PUT/PATCH (any) — single Da/Ne confirm with full context
  DOUBLE       DELETE (any) OR critical-field PUT/PATCH
                 — first Da/Ne, then second confirmation

This is non-negotiable for 0-error tolerance domain. The earlier
"in-range auto-execute" path was a UX optimization that violates
the safety pillar. Range checking is preserved — it just becomes
DOUBLE confirm instead of CONFIRM when triggered, since out-of-range
is itself a red flag.

Outcome:
  AUTO         caller executes immediately (GET only)
  CONFIRM      caller renders single confirm dialog, waits for "Da/Ne"
  DOUBLE       caller renders FIRST confirm, then SECOND on "Da"
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


DECISION_AUTO = "auto"
DECISION_CONFIRM = "confirm"
DECISION_DOUBLE = "double_confirm"


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
    double_first_message: str = ""
    double_second_message: str = ""
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

    # DELETE → always double confirm (highest risk: irreversible)
    if method_upper == "DELETE":
        return MutationDecision(
            decision=DECISION_DOUBLE,
            double_first_message=(
                f"Sigurno želiš obrisati {entity_label or 'ovaj zapis'}? "
                "Ova akcija je nepovratna. Da/Ne"
            ),
            double_second_message=(
                "Posljednja potvrda — brisanje će biti TRAJNO. Da/Ne"
            ),
            log_reason="delete_double_confirm",
        )

    # Critical-field changes → double confirm
    critical = _CRITICAL_FIELDS.get(tool_id, set())
    if critical and any(k in critical for k in (params or {}).keys()):
        return MutationDecision(
            decision=DECISION_DOUBLE,
            double_first_message=(
                f"Mijenjaš kritične podatke o {entity_label or 'zapisu'}. "
                "Da/Ne"
            ),
            double_second_message=(
                "Sigurno potvrđuješ izmjenu? Promjena se odmah primjenjuje. Da/Ne"
            ),
            log_reason="critical_field_double_confirm",
        )

    # Out-of-range → escalate to DOUBLE confirm (red flag on top of mutation)
    ranges = _DEFAULT_RANGES.get(tool_id) or {}
    out_of_range = _check_range(tool_id, params, ranges, last_known_values)
    if out_of_range:
        return MutationDecision(
            decision=DECISION_DOUBLE,
            double_first_message=(
                f"Vrijednost je izvan očekivanih granica ({out_of_range}). "
                f"Potvrđuješ {entity_label or 'akciju'}? Da/Ne"
            ),
            double_second_message=(
                "Posljednja potvrda — vrijednost je neuobičajena. Stvarno "
                "potvrđuješ? Da/Ne"
            ),
            log_reason=f"out_of_range_double_confirm:{out_of_range}",
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
