"""Tests for L6 mutation_gate.decide_mutation().

Per CLAUDE.md §1.3: every POST/PUT/PATCH/DELETE must require explicit
Da/Ne confirmation. AUTO is permitted only for GET (read-only).
"""
from __future__ import annotations

from services.v2.mutation_gate import (
    DECISION_AUTO, DECISION_CONFIRM, DECISION_DOUBLE, decide_mutation,
)


def test_get_is_auto():
    d = decide_mutation("get_X", "GET", {})
    assert d.decision == DECISION_AUTO


def test_delete_is_double():
    d = decide_mutation("delete_VehicleCalendar_id", "DELETE",
                        {"id": "x"}, entity_label="rezervaciju")
    assert d.decision == DECISION_DOUBLE
    assert "rezervaciju" in d.double_first_message
    assert "TRAJNO" in d.double_second_message


def test_critical_field_change_is_double():
    d = decide_mutation("patch_Vehicles_id", "PATCH",
                        {"LicencePlate": "ZG-1234"})
    assert d.decision == DECISION_DOUBLE


def test_in_range_mileage_requires_single_confirm():
    """In-range mutation no longer auto-executes — CLAUDE.md §1.3."""
    d = decide_mutation("post_AddMileage", "POST",
                        {"Value": 145000},
                        last_known_values={"last_mileage": 144500},
                        entity_label="VW Golf")
    assert d.decision == DECISION_CONFIRM
    assert "VW Golf" in d.confirm_message


def test_out_of_range_jump_escalates_to_double():
    """Out-of-range parameter is a red flag → escalate from CONFIRM to DOUBLE."""
    d = decide_mutation("post_AddMileage", "POST",
                        {"Value": 200000},  # jump 100k from last 100k
                        last_known_values={"last_mileage": 100000},
                        entity_label="VW Golf")
    assert d.decision == DECISION_DOUBLE
    assert "VW Golf" in d.double_first_message
    assert "izvan očekivanih granica" in d.double_first_message


def test_negative_mileage_escalates_to_double():
    d = decide_mutation("post_AddMileage", "POST", {"Value": -100})
    assert d.decision == DECISION_DOUBLE


def test_huge_mileage_escalates_to_double():
    d = decide_mutation("post_AddMileage", "POST", {"Value": 99999999})
    assert d.decision == DECISION_DOUBLE


def test_non_numeric_mileage_escalates_to_double():
    d = decide_mutation("post_AddMileage", "POST", {"Value": "abc"})
    assert d.decision == DECISION_DOUBLE


def test_unrecognized_post_tool_requires_confirm():
    """Tools without range rules still require single confirm —
    CLAUDE.md §1.3 doctrine: NO mutation without explicit Da."""
    d = decide_mutation("post_NewTool", "POST", {"foo": "bar"},
                        entity_label="novi unos")
    assert d.decision == DECISION_CONFIRM
    assert "novi unos" in d.confirm_message


def test_put_without_critical_fields_requires_confirm():
    d = decide_mutation("put_VehicleContracts_id", "PUT",
                        {"description": "new desc"},
                        entity_label="ugovor")
    assert d.decision == DECISION_CONFIRM


def test_patch_without_critical_fields_requires_confirm():
    d = decide_mutation("patch_Persons_id", "PATCH",
                        {"phone": "+385912345"},
                        entity_label="osobu Marko")
    assert d.decision == DECISION_CONFIRM
    assert "Marko" in d.confirm_message
