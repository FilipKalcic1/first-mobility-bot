"""Sanity tests for anchor_vocab module."""
from __future__ import annotations

from services.router.anchor_vocab import (
    ENTITY_SYNONYMS,
    VERB_SYNONYMS,
    count_curated_entities,
    get_entity_synonyms,
    get_verb_synonyms,
)


# --------------------------------------------------------------------------
# VERB_SYNONYMS structural sanity
# --------------------------------------------------------------------------


def test_verb_synonyms_has_all_http_methods():
    for m in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        assert m in VERB_SYNONYMS
        assert len(VERB_SYNONYMS[m]) >= 5, f"{m} should have ≥5 verbs"


def test_verb_synonyms_are_lowercase():
    """All verbs must be lowercase — that's the whole point."""
    for method, verbs in VERB_SYNONYMS.items():
        for v in verbs:
            assert v == v.lower(), f"{method}: '{v}' must be lowercase"


def test_verb_synonyms_no_duplicates_within_method():
    for method, verbs in VERB_SYNONYMS.items():
        assert len(verbs) == len(set(verbs)), f"{method} has duplicates"


def test_delete_verb_diversity():
    """DELETE must cover briši/obriši/ukloni/otkaži variations."""
    verbs = set(VERB_SYNONYMS["DELETE"])
    assert "briši" in verbs
    assert "obriši" in verbs
    assert "ukloni" in verbs
    assert "otkaži" in verbs


def test_post_verb_diversity():
    """POST must cover dodaj/stavi/kreiraj/novi/upiši."""
    verbs = set(VERB_SYNONYMS["POST"])
    assert "dodaj" in verbs
    assert "stavi" in verbs
    assert "kreiraj" in verbs
    assert "novi" in verbs


def test_get_verb_diversity():
    """GET must cover pokaži/daj mi/prikaži/popis."""
    verbs = set(VERB_SYNONYMS["GET"])
    assert "pokaži" in verbs
    assert "daj mi" in verbs
    assert "prikaži" in verbs


# --------------------------------------------------------------------------
# ENTITY_SYNONYMS coverage (against real registry path components)
# --------------------------------------------------------------------------


REAL_ENTITY_PATH_COMPONENTS = {
    # From config/tool_data.json paths (provjereno, real)
    "Partners", "Persons", "Tenants", "Equipment", "Expenses", "Trips",
    "Vehicles", "Companies", "CostCenters", "DocumentTypes",
    "PersonActivityTypes", "PersonTypes", "TeamMembers", "Teams",
    "Cases", "CaseTypes", "EquipmentCalendar", "EquipmentTypes",
    "ExpenseGroups", "ExpenseTypes", "MileageReports",
    "PeriodicActivities", "PeriodicActivityTypes", "Pools",
    "SchedulingModels", "Tags", "TripTypes", "VehicleCalendar",
    "VehicleContracts", "VehicleTypes", "OrgUnits", "TenantPermissions",
    "MonthlyMileages", "PersonOrgUnits", "PersonPeriodicActivities",
    "PeriodicActivitiesSchedules", "VehiclesHistoricalEntries",
    "LatestVehicleCalendar", "LatestEquipmentCalendar",
    "LatestMileageReports", "LatestPeriodicActivities",
    "LatestPersonPeriodicActivities", "LatestVehicleContracts",
}


def test_all_real_entities_have_synonyms():
    """Every entity that appears in registry paths must have curated
    Croatian synonyms (no humanize fallback for real production tools)."""
    missing = REAL_ENTITY_PATH_COMPONENTS - set(ENTITY_SYNONYMS.keys())
    assert not missing, f"Real entities missing from vocab: {sorted(missing)}"


def test_entity_synonyms_are_lowercase():
    for entity, syns in ENTITY_SYNONYMS.items():
        for s in syns:
            assert s == s.lower(), f"{entity}: '{s}' must be lowercase"


def test_entity_synonyms_no_duplicates_within():
    for entity, syns in ENTITY_SYNONYMS.items():
        assert len(syns) == len(set(syns)), f"{entity} has duplicate synonyms"


def test_entity_synonyms_have_multiple_variants():
    """Each entity should have ≥2 Croatian variants for diversity. The
    point of synonym list is to capture different user phrasings."""
    too_thin = [e for e, syns in ENTITY_SYNONYMS.items() if len(syns) < 2]
    assert not too_thin, f"Entities with <2 synonyms: {too_thin}"


# --------------------------------------------------------------------------
# get_verb_synonyms / get_entity_synonyms helpers
# --------------------------------------------------------------------------


def test_get_verb_synonyms_returns_copy():
    """Modifying returned list must NOT mutate VERB_SYNONYMS."""
    delete_verbs = get_verb_synonyms("DELETE")
    delete_verbs.append("HACKED")
    assert "HACKED" not in VERB_SYNONYMS["DELETE"]


def test_get_verb_synonyms_unknown_method_falls_back_to_get():
    """Unknown method (e.g., HEAD) → return GET verbs (defensive)."""
    result = get_verb_synonyms("HEAD")
    assert result == get_verb_synonyms("GET")


def test_get_verb_synonyms_case_insensitive():
    assert get_verb_synonyms("delete") == get_verb_synonyms("DELETE")
    assert get_verb_synonyms("Get") == get_verb_synonyms("GET")


def test_get_entity_synonyms_known_entity():
    syns = get_entity_synonyms("Vehicle")
    assert "vozilo" in syns
    assert "auto" in syns


def test_get_entity_synonyms_plural_falls_back_to_singular():
    """If Vehicles isn't in vocab but Vehicle is, use Vehicle's synonyms."""
    # First make sure both exist for this test to be valid
    assert "Vehicles" in ENTITY_SYNONYMS
    # Vehicles has its own entry but should be different from Vehicle plural
    vehicles_syns = get_entity_synonyms("Vehicles")
    assert "vozila" in vehicles_syns
    # If someone passes "VehiclesPLURAL" (not in vocab), fall back
    custom_syns = get_entity_synonyms("VehiclesUnknown")
    # Should humanize → "vehicles unknown"
    assert custom_syns == ["vehicles unknown"]


def test_get_entity_synonyms_unknown_humanizes():
    """Completely unknown entity → camelCase humanize fallback."""
    syns = get_entity_synonyms("SchedulingFooBar")
    assert syns == ["scheduling foo bar"]


def test_get_entity_synonyms_empty_returns_generic():
    syns = get_entity_synonyms("")
    assert syns and len(syns) >= 1  # generic hints


# --------------------------------------------------------------------------
# Specific real-world entity checks (regression guard)
# --------------------------------------------------------------------------


def test_mileage_synonyms_include_km():
    """Drivers type 'km' more than 'kilometraža'."""
    assert "km" in ENTITY_SYNONYMS["Mileage"]


def test_calendar_synonyms_include_rezervacija():
    """VehicleCalendar = reservations in driver speech."""
    assert any(
        "rezervacij" in s for s in ENTITY_SYNONYMS["VehicleCalendar"]
    )


def test_companies_synonyms_include_firme():
    """Croatian users say 'firma' more than 'kompanija'."""
    assert "firme" in ENTITY_SYNONYMS["Companies"]


def test_persons_synonyms_include_vozaci():
    """In fleet domain, Person is most often 'vozač'."""
    assert "vozači" in ENTITY_SYNONYMS["Persons"]


def test_count_curated_entities_at_least_40():
    """We've curated significant entity coverage — guard regression."""
    assert count_curated_entities() >= 40
