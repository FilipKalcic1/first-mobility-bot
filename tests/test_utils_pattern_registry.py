"""Unit tests for services/utils/pattern_registry.py — regex + intent detection."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from services.utils.pattern_registry import (  # noqa: E402
    PatternRegistry,
    QueryIntent,
    detect_intent,
    get_injectable_context,
    is_user_specific,
    normalize_context_key,
    should_skip_person_id_injection,
)


# --- UUID detection ---

def test_uuid_valid():
    assert PatternRegistry.is_uuid("12345678-1234-1234-1234-123456789abc")
    assert PatternRegistry.is_uuid("ABCDEF12-3456-7890-ABCD-EF1234567890")


def test_uuid_invalid():
    assert not PatternRegistry.is_uuid("not-a-uuid")
    assert not PatternRegistry.is_uuid("")
    assert not PatternRegistry.is_uuid("12345678-1234-1234-1234")


def test_find_uuids_in_text():
    text = "rezervacija 12345678-1234-1234-1234-123456789abc je gotova"
    found = PatternRegistry.find_uuids(text)
    assert found == ["12345678-1234-1234-1234-123456789abc"]


def test_find_uuids_empty():
    assert PatternRegistry.find_uuids("") == []
    assert PatternRegistry.find_uuids("plain text") == []


# --- Croatian plate detection ---

def test_croatian_plate_valid_formats():
    assert PatternRegistry.is_croatian_plate("ZG1234AB")
    assert PatternRegistry.is_croatian_plate("ZG-1234-AB")
    assert PatternRegistry.is_croatian_plate("ZG 1234 AB")
    assert PatternRegistry.is_croatian_plate("DA053F")  # 1-letter suffix


def test_croatian_plate_invalid():
    assert not PatternRegistry.is_croatian_plate("12345")
    assert not PatternRegistry.is_croatian_plate("")
    assert not PatternRegistry.is_croatian_plate("ABC123XYZ")


def test_find_plates_in_text():
    plates = PatternRegistry.find_plates("daj mi ZG-1234-AB i DA053F")
    assert "ZG-1234-AB" in plates
    assert "DA053F" in plates


# --- VIN detection ---

def test_vin_valid():
    assert PatternRegistry.is_vin("1HGCM82633A123456")  # 17 chars no I/O/Q


def test_vin_invalid_contains_forbidden_chars():
    assert not PatternRegistry.is_vin("1HGCM82633A12345I")  # I forbidden
    assert not PatternRegistry.is_vin("12345")  # too short


# --- Email detection ---

def test_email_valid():
    assert PatternRegistry.is_email("damir@example.com")
    assert PatternRegistry.is_email("user.name+tag@sub.example.co")


def test_email_invalid():
    assert not PatternRegistry.is_email("not-an-email")
    assert not PatternRegistry.is_email("")
    assert not PatternRegistry.is_email("@no-local.com")


# --- Context-key canonicalization ---

def test_normalize_context_key_person_aliases():
    assert normalize_context_key("PersonId") == "person_id"
    assert normalize_context_key("userid") == "person_id"
    assert normalize_context_key("DriverId") == "person_id"
    assert normalize_context_key("AssignedToId") == "person_id"


def test_normalize_context_key_vehicle_aliases():
    assert normalize_context_key("VehicleId") == "vehicle_id"
    assert normalize_context_key("car_id") == "vehicle_id"
    assert normalize_context_key("AssetId") == "vehicle_id"


def test_normalize_context_key_tenant_aliases():
    assert normalize_context_key("TenantId") == "tenant_id"
    assert normalize_context_key("X-Tenant-Id") == "tenant_id"


def test_normalize_context_key_unknown():
    assert normalize_context_key("foobar") is None
    assert normalize_context_key("") is None
    assert normalize_context_key(None) is None


# --- User-specific query detection ---

def test_user_specific_croatian_possessives():
    assert is_user_specific("moja vozila")
    assert is_user_specific("imam li dodijeljena vozila")
    assert is_user_specific("koja vozila pripadaju meni")


def test_user_specific_english():
    assert is_user_specific("my vehicles")
    assert is_user_specific("do i have a car assigned to me")


def test_user_specific_negative():
    assert not is_user_specific("popis vozila tvrtke")
    assert not is_user_specific("")
    assert not is_user_specific("koliko ima zaposlenika")


# --- PersonId skip list ---

def test_skip_person_id_for_shared_data_tools():
    assert should_skip_person_id_injection("get_available_vehicles")
    assert should_skip_person_id_injection("get_location_list")
    assert should_skip_person_id_injection("get_site_settings")
    assert should_skip_person_id_injection("update_config_value")


def test_skip_person_id_negative():
    assert not should_skip_person_id_injection("get_my_reservations")
    assert not should_skip_person_id_injection("")


# --- Injectable context ---

def test_injectable_context_extracts_person_and_tenant():
    ctx = {"PersonId": "p1", "TenantId": "t1", "irrelevant": "x"}
    out = get_injectable_context(ctx)
    assert out == {"person_id": "p1", "tenant_id": "t1"}


def test_injectable_context_ignores_none_values():
    ctx = {"PersonId": None, "TenantId": "t1"}
    assert get_injectable_context(ctx) == {"tenant_id": "t1"}


def test_injectable_context_empty():
    assert get_injectable_context({}) == {}


# --- Intent detection ---

def test_intent_read_default():
    assert detect_intent("daj mi popis vozila") == QueryIntent.READ
    assert detect_intent("koje rezervacije imam") == QueryIntent.READ


def test_intent_write_croatian():
    assert detect_intent("rezerviraj vozilo") == QueryIntent.WRITE
    assert detect_intent("kreiraj novi slučaj") == QueryIntent.WRITE
    assert detect_intent("dodaj korisnika") == QueryIntent.WRITE


def test_intent_delete_croatian():
    assert detect_intent("otkaži rezervaciju 123") == QueryIntent.DELETE
    assert detect_intent("obriši slučaj") == QueryIntent.DELETE


def test_intent_delete_precedence_over_write():
    # If both delete and write keywords present, DELETE wins (matched first).
    assert detect_intent("otkaži i kreiraj novu") == QueryIntent.DELETE


def test_intent_empty():
    assert detect_intent("") == QueryIntent.READ
    assert detect_intent(None) == QueryIntent.READ
