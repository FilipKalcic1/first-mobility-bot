"""Tests for L8 formatter.format_response()."""
from __future__ import annotations

from services.v2.formatter import (
    FIELD_LABELS_HR, format_response,
)


VEHICLE_PAYLOAD = {
    "VehicleName":     "VW Golf",
    "LicencePlate":    "ZG-1234-AB",
    "LastMileage":     42500,
    "VIN":             "WVWZZZ1KZ8W123456",
    "LeasingCompany":  "PBZ Leasing",
}


def test_vehicle_summary_renders_known_fields():
    r = format_response("vehicle_data_summary", VEHICLE_PAYLOAD)
    assert r.template_used == "vehicle_data_summary"
    assert "VW Golf" in r.text
    assert "ZG-1234-AB" in r.text
    assert "42500" in r.text
    assert "PBZ Leasing" in r.text


def test_vehicle_summary_handles_empty_dict():
    r = format_response("vehicle_data_summary", {})
    assert "Nemam podataka" in r.text


def test_vehicle_summary_handles_non_dict():
    r = format_response("vehicle_data_summary", "not a dict")
    assert "Nemam" in r.text


def test_vehicle_field_resolves_kilometraza():
    r = format_response(
        "vehicle_data_field", VEHICLE_PAYLOAD, field_hint="kolika mi je km"
    )
    assert "42500" in r.text
    assert "kilometraža" in r.text


def test_vehicle_field_resolves_registracija():
    r = format_response(
        "vehicle_data_field", VEHICLE_PAYLOAD, field_hint="koja mi je registracija"
    )
    assert "ZG-1234-AB" in r.text


def test_vehicle_field_falls_back_to_summary_on_unknown_hint():
    r = format_response(
        "vehicle_data_field", VEHICLE_PAYLOAD, field_hint="totalno_nepostojece_pitanje"
    )
    # Falls back to summary
    assert "VW Golf" in r.text


def test_vehicle_field_handles_missing_value():
    payload = dict(VEHICLE_PAYLOAD, LastMileage=None)
    r = format_response("vehicle_data_field", payload, field_hint="km")
    assert "Nemam" in r.text


def test_list_with_count_zero():
    r = format_response("list_with_count", [],
                       extra_context={"entity_label": "rezervacija"})
    assert "Nemaš" in r.text


def test_list_with_count_singular():
    r = format_response("list_with_count", [{"Name": "X"}],
                       extra_context={"entity_label": "rezervacija"})
    assert "1 rezervacija" in r.text


def test_list_with_count_plural_with_truncation():
    items = [{"Name": f"item{i}"} for i in range(8)]
    r = format_response("list_with_count", items,
                       extra_context={"entity_label": "stavki"})
    assert "8 stavki" in r.text
    assert "još 3" in r.text  # 8 total - 5 shown = 3 more


def test_list_unwraps_data_envelope():
    payload = {"data": [{"Name": "A"}, {"Name": "B"}]}
    r = format_response("list_with_count", payload,
                       extra_context={"entity_label": "rezervacija"})
    assert "2 rezervacija" in r.text


def test_empty_result_template():
    r = format_response("empty_result", None,
                       extra_context={"entity_label": "rezervacija"})
    assert "Nemam" in r.text and "rezervacija" in r.text


def test_mutation_success_template():
    r = format_response("mutation_success", {},
                       extra_context={"action": "Rezervacija"})
    assert "Rezervacija" in r.text and "uspješno" in r.text


def test_mutation_failed_template():
    r = format_response("mutation_failed", {},
                       extra_context={"action": "Brisanje", "reason": "403"})
    assert "Brisanje" in r.text and "403" in r.text


def test_fallback_template_uses_extra_message():
    r = format_response("fallback", {},
                       extra_context={"message": "Nisam te razumio."})
    assert r.text == "Nisam te razumio."


def test_fallback_template_default_message():
    r = format_response("fallback", {})
    assert "Nisam siguran" in r.text


def test_smart_default_none_data():
    r = format_response(None, None)
    assert "Nemam" in r.text


def test_smart_default_list_data():
    r = format_response(None, [{"Name": "A"}])
    assert r.template_used == "list_with_count"


def test_smart_default_dict_with_hint():
    r = format_response(None, VEHICLE_PAYLOAD, field_hint="km")
    assert "42500" in r.text


def test_smart_default_dict_no_hint():
    r = format_response(None, VEHICLE_PAYLOAD)
    assert "VW Golf" in r.text


def test_smart_default_scalar():
    r = format_response(None, 42, extra_context={"label": "Broj"})
    assert "Broj" in r.text and "42" in r.text


def test_unknown_template_falls_back_to_smart_default():
    r = format_response("nonexistent_template_xyz", VEHICLE_PAYLOAD)
    # Falls through smart_default → vehicle_data_summary
    assert "VW Golf" in r.text


def test_field_labels_hr_translates_known_keys():
    """Sanity check on translation map."""
    assert FIELD_LABELS_HR["LicencePlate"] == "registracija"
    assert FIELD_LABELS_HR["LastMileage"] == "kilometraža"
    assert FIELD_LABELS_HR["VIN"] == "VIN broj"


def test_generic_value_template_single_key():
    r = format_response("generic_value", {"Result": "OK"},
                       extra_context={"label": "Status"})
    assert "Status" in r.text and "OK" in r.text


# ---------------------------------------------------------------------------
# Feedback hint append (Damir-feedback ground-truth signal)
# ---------------------------------------------------------------------------
# Bot appends "nije točno" hint to read/mutate execute responses so the
# user has a deterministic phrase to flag wrong routing on the next turn.
# Skipped on welcome/help/clarify/empty/failed where the hint would be
# noise. Without these tests, a future formatter refactor could silently
# drop the hint and we'd lose the ground-truth signal.

_HINT_FRAGMENT = "nije točno"


def test_hint_appended_on_read_execute():
    r = format_response("vehicle_data_summary", VEHICLE_PAYLOAD)
    assert _HINT_FRAGMENT in r.text


def test_hint_appended_on_field_render():
    r = format_response("vehicle_data_field", VEHICLE_PAYLOAD,
                       field_hint="LicencePlate")
    assert _HINT_FRAGMENT in r.text


def test_hint_appended_on_generic_value():
    r = format_response("generic_value", {"Result": "OK"},
                       extra_context={"label": "Status"})
    assert _HINT_FRAGMENT in r.text


def test_hint_appended_on_mutation_success_with_distinct_phrasing():
    r = format_response("mutation_success", None,
                       extra_context={"action": "Rezervacija"})
    assert _HINT_FRAGMENT in r.text
    # Mutation hint phrasing differs ("prije potvrde") from read hint
    assert "prije potvrde" in r.text


def test_hint_NOT_appended_on_empty_result():
    r = format_response("empty_result", None,
                       extra_context={"entity_label": "vozila"})
    assert _HINT_FRAGMENT not in r.text


def test_hint_NOT_appended_on_fallback():
    r = format_response("fallback", None,
                       extra_context={"message": "Nisam siguran."})
    assert _HINT_FRAGMENT not in r.text


def test_hint_NOT_appended_on_mutation_failed():
    r = format_response("mutation_failed", None,
                       extra_context={"action": "Brisanje", "reason": "permission denied"})
    assert _HINT_FRAGMENT not in r.text
