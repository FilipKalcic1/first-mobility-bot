"""Tests for L6 mutation_gate.decide_mutation().

Pure method-based gate (Filip 2026-05-27, "maksimalna uniformnost"): the
decision depends ONLY on the HTTP method — uniform across all 950 tools, zero
per-tool data. GET → AUTO; every write → single Da/Ne CONFIRM (DELETE gets
stronger wording). Value/field verification is the engine's echo, not the gate;
earlier per-tool range/critical-field special cases were removed.
"""
from __future__ import annotations

from services.v2.mutation_gate import (
    DECISION_AUTO, DECISION_CONFIRM, decide_mutation,
)


def test_get_is_auto():
    d = decide_mutation("GET")
    assert d.decision == DECISION_AUTO
    assert d.confirm_message == ""


def test_post_is_single_confirm():
    d = decide_mutation("POST", entity_label="novi unos")
    assert d.decision == DECISION_CONFIRM
    assert "novi unos" in d.confirm_message
    assert d.confirm_message.endswith("Da/Ne")


def test_put_is_single_confirm():
    assert decide_mutation("PUT").decision == DECISION_CONFIRM


def test_patch_is_single_confirm():
    d = decide_mutation("PATCH", entity_label="osobu Marko")
    assert d.decision == DECISION_CONFIRM
    assert "Marko" in d.confirm_message


def test_delete_is_single_confirm_with_strong_warning():
    d = decide_mutation("DELETE", entity_label="rezervaciju")
    assert d.decision == DECISION_CONFIRM
    assert "rezervaciju" in d.confirm_message
    assert "TRAJNO" in d.confirm_message
    assert "nepovratna" in d.confirm_message


def test_method_is_case_insensitive():
    assert decide_mutation("get").decision == DECISION_AUTO
    assert decide_mutation("delete").decision == DECISION_CONFIRM


def test_entity_label_defaults_when_missing():
    d = decide_mutation("POST")
    assert d.decision == DECISION_CONFIRM
    assert "akciju" in d.confirm_message


def test_unknown_or_empty_method_is_confirm_not_auto():
    """Defensive: only GET is AUTO. Anything unrecognized → CONFIRM (never a
    silent write)."""
    assert decide_mutation("").decision == DECISION_CONFIRM
    assert decide_mutation("WEIRD").decision == DECISION_CONFIRM


# ---- Uniformity (Filip 2026-05-27): no per-tool special-casing ----

def test_former_critical_field_tool_gets_plain_confirm():
    """patch_Vehicles_id changing LicencePlate used to get a special 'kritične
    podatke' warning. Now it's a plain PATCH confirm like any other — the echo
    shows the new value, the gate stays uniform."""
    d = decide_mutation("PATCH", entity_label="vozilo")
    assert d.decision == DECISION_CONFIRM
    assert "kritične" not in d.confirm_message
    assert "⚠️" not in d.confirm_message      # no per-tool red flag
    assert d.confirm_message == "Potvrđuješ vozilo? Da/Ne"


def test_former_range_tool_gets_plain_confirm_regardless_of_value():
    """post_AddMileage with an absurd value used to trip an out-of-range ⚠️.
    Now every write confirms identically — value sanity is the echo's job."""
    d = decide_mutation("POST", entity_label="VW Golf")
    assert d.decision == DECISION_CONFIRM
    assert "izvan" not in d.confirm_message
    assert "⚠️" not in d.confirm_message
    assert d.confirm_message == "Potvrđuješ VW Golf? Da/Ne"


def test_decision_has_no_warning_attribute():
    """The reusable `warning` field was removed with the per-tool specials."""
    d = decide_mutation("POST")
    assert not hasattr(d, "warning")
