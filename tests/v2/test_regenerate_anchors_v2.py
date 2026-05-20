"""Tests for scripts/regenerate_anchors_v2.py template generator.

Verifies deterministic anchor generation for known input shapes,
without touching real tool_data.json.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
# scripts/ is not a package — add explicitly
scripts_dir = REPO_ROOT / "scripts"
if str(scripts_dir) not in sys.path:
    sys.path.insert(0, str(scripts_dir))

# Direct module import (avoid scripts package convention)
import importlib.util
spec = importlib.util.spec_from_file_location(
    "regenerate_anchors_v2", scripts_dir / "regenerate_anchors_v2.py"
)
regen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(regen)


# --------------------------------------------------------------------------
# parse_operation
# --------------------------------------------------------------------------


def test_parse_simple_get_collection():
    out = regen.parse_operation("get_Persons", "/Persons")
    assert out["method"] == "GET"
    assert out["primary"] == "Persons"
    assert out["modifier"] == ""
    assert out["has_id"] is False


def test_parse_delete_with_id():
    out = regen.parse_operation(
        "delete_VehicleCalendar_id", "/VehicleCalendar/{id}"
    )
    assert out["method"] == "DELETE"
    assert out["primary"] == "VehicleCalendar"
    assert out["has_id"] is True


def test_parse_nested_document():
    out = regen.parse_operation(
        "delete_VehicleCalendar_id_documents_documentId",
        "/VehicleCalendar/{id}/documents/{documentId}",
    )
    assert out["method"] == "DELETE"
    # Last entity in path = "documents" (lowercase, sub-resource)
    assert out["primary"] == "documents"
    assert out["modifier"] == "VehicleCalendar"
    assert out["has_id"] is True


def test_parse_post_persons_documents():
    out = regen.parse_operation(
        "post_Persons_id_documents", "/Persons/{id}/documents"
    )
    assert out["method"] == "POST"
    assert out["primary"] == "documents"
    assert out["modifier"] == "Persons"
    assert out["has_id"] is True


def test_parse_latest_variant_treated_as_single_entity():
    out = regen.parse_operation(
        "delete_LatestVehicleCalendar", "/LatestVehicleCalendar"
    )
    assert out["primary"] == "LatestVehicleCalendar"
    assert out["modifier"] == ""


# --------------------------------------------------------------------------
# canonicalize_entity_key
# --------------------------------------------------------------------------


def test_canonicalize_lowercase_becomes_uppercase():
    assert regen.canonicalize_entity_key("documents") == "Documents"


def test_canonicalize_already_capital_unchanged():
    assert regen.canonicalize_entity_key("VehicleCalendar") == "VehicleCalendar"


def test_canonicalize_empty_returns_empty():
    assert regen.canonicalize_entity_key("") == ""


# --------------------------------------------------------------------------
# verb_to_infinitive
# --------------------------------------------------------------------------


def test_verb_infinitive_known_mapping():
    assert regen.verb_to_infinitive("briši") == "obrisati"
    assert regen.verb_to_infinitive("dodaj") == "dodati"
    assert regen.verb_to_infinitive("promijeni") == "promijeniti"


def test_verb_infinitive_multiword_takes_first_then_falls_back():
    """For multi-word verbs ('daj mi'), takes first word ('daj').
    If not in map, returns verb itself unchanged."""
    # 'daj' is not in map → fallback to "daj mi" as whole string
    out = regen.verb_to_infinitive("daj mi")
    assert isinstance(out, str)
    assert out  # not empty
    # 'briši' IS in map and first word
    assert regen.verb_to_infinitive("briši nešto") == "obrisati"


def test_verb_infinitive_unknown_falls_back():
    """Unknown verb returns itself (deterministic, no crash)."""
    assert regen.verb_to_infinitive("haxx0r") == "haxx0r"


# --------------------------------------------------------------------------
# generate_anchors_for_tool — integration
# --------------------------------------------------------------------------


def test_generate_anchors_returns_six_or_fewer():
    tool = {"method": "DELETE", "path": "/Persons/{id}", "intent_summary": "X"}
    anchors = regen.generate_anchors_for_tool("delete_Persons_id", tool)
    assert 1 <= len(anchors) <= 6


def test_generate_anchors_lowercase():
    """All generated anchors must be lowercase (anchor quality rule)."""
    tool = {"method": "POST", "path": "/Persons", "intent_summary": "X"}
    anchors = regen.generate_anchors_for_tool("post_Persons", tool)
    for a in anchors:
        assert a == a.lower(), f"non-lowercase: {a!r}"


def test_generate_anchors_no_duplicates():
    tool = {"method": "DELETE", "path": "/VehicleCalendar/{id}", "intent_summary": "X"}
    anchors = regen.generate_anchors_for_tool("delete_VehicleCalendar_id", tool)
    assert len(anchors) == len(set(anchors))


def test_generate_anchors_delete_uses_delete_verbs():
    """DELETE tool anchors should use briši/obriši/ukloni/otkaži variants."""
    tool = {"method": "DELETE", "path": "/Persons/{id}", "intent_summary": "X"}
    anchors = regen.generate_anchors_for_tool("delete_Persons_id", tool)
    combined = " ".join(anchors).lower()
    delete_verbs_found = sum(
        1 for v in ("briši", "obriši", "ukloni", "otkaži", "izbriši", "makni")
        if v in combined
    )
    assert delete_verbs_found >= 2, f"expected ≥2 delete verbs in {anchors}"


def test_generate_anchors_post_uses_post_verbs():
    tool = {"method": "POST", "path": "/Persons", "intent_summary": "X"}
    anchors = regen.generate_anchors_for_tool("post_Persons", tool)
    combined = " ".join(anchors).lower()
    post_verbs_found = sum(
        1 for v in ("dodaj", "stavi", "kreiraj", "novi", "upiši")
        if v in combined
    )
    assert post_verbs_found >= 2, f"expected ≥2 post verbs in {anchors}"


def test_generate_anchors_includes_id_hint_when_path_has_id():
    """Tools with {id} in path should mention 'po id' or similar."""
    tool = {"method": "DELETE", "path": "/Persons/{id}", "intent_summary": "X"}
    anchors = regen.generate_anchors_for_tool("delete_Persons_id", tool)
    combined = " ".join(anchors).lower()
    assert "po id" in combined


def test_generate_anchors_includes_entity_synonym():
    """Anchors should contain the Croatian entity synonym."""
    tool = {"method": "GET", "path": "/Persons", "intent_summary": "X"}
    anchors = regen.generate_anchors_for_tool("get_Persons", tool)
    combined = " ".join(anchors).lower()
    # Should mention osobe/vozači/zaposlenici (one of Croatian synonyms)
    assert any(
        s in combined for s in ("osobe", "vozači", "zaposlenici")
    ), f"expected Croatian synonym in {anchors}"


def test_generate_anchors_includes_ultra_short_for_fallback():
    """1-word entity anchor must be present for short query fallback."""
    tool = {"method": "GET", "path": "/Vehicles", "intent_summary": "X"}
    anchors = regen.generate_anchors_for_tool("get_Vehicles", tool)
    # One anchor should be just the entity (1-2 words)
    short_anchors = [a for a in anchors if len(a.split()) <= 2]
    assert len(short_anchors) >= 1, f"no short anchor in {anchors}"


def test_generate_anchors_idempotent():
    """Re-running with same input gives same output (deterministic)."""
    tool = {"method": "POST", "path": "/Persons/{id}/documents", "intent_summary": "X"}
    out1 = regen.generate_anchors_for_tool("post_Persons_id_documents", tool)
    out2 = regen.generate_anchors_for_tool("post_Persons_id_documents", tool)
    assert out1 == out2


def test_generate_anchors_for_unknown_entity_falls_back_humanize():
    """Entity not in vocab → humanized fallback (anchors still produced)."""
    tool = {"method": "GET", "path": "/SomeNewThing", "intent_summary": "X"}
    anchors = regen.generate_anchors_for_tool("get_SomeNewThing", tool)
    assert len(anchors) > 0
    # Anchors should contain humanized version
    combined = " ".join(anchors).lower()
    assert "some new thing" in combined or "new thing" in combined or "thing" in combined


# --------------------------------------------------------------------------
# is_internal helper
# --------------------------------------------------------------------------


def test_is_internal_recognizes_helper_suffixes():
    """Internal helpers have _suffix endings (underscored before keyword)."""
    assert regen.is_internal("get_Vehicles_DistinctMakes") is True
    assert regen.is_internal("get_Vehicles_Agg") is True
    assert regen.is_internal("get_Vehicles_GroupBy") is True
    assert regen.is_internal("get_Vehicles_ProjectTo") is True
    assert regen.is_internal("post_X_DeleteByCriteria") is True
    assert regen.is_internal("get_Vehicles") is False
    assert regen.is_internal("post_Persons") is False
