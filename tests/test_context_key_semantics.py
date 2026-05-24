"""
Verify the V6.1 migration eliminates the 'every context_key defaults to
person_id' bug, without damaging the V6 alias entries.

Run:
    python scripts/migrate_registry_v6.py
    python scripts/migrate_registry_v6_1.py
    pytest tests/test_context_key_semantics.py -v
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "config" / "processed_tool_registry.json"


@pytest.fixture(scope="module")
def registry() -> list[dict]:
    if not TARGET.exists():
        pytest.fail(
            f"{TARGET} missing. Run migrate_registry_v6.py then migrate_registry_v6_1.py."
        )
    raw = json.loads(TARGET.read_text(encoding="utf-8"))
    return raw["tools"] if isinstance(raw, dict) and "tools" in raw else raw


def _find(tools: list[dict], op_id: str) -> dict:
    for t in tools:
        if t.get("operation_id") == op_id:
            return t
    raise AssertionError(f"missing operation_id {op_id}")


def _iter_context_params(tools: list[dict]):
    for t in tools:
        for pname, pdef in (t.get("parameters") or {}).items():
            if isinstance(pdef, dict) and pdef.get("dependency_source") == "context":
                yield t.get("operation_id"), pname, pdef


def test_addmileage_vehicle_id_points_to_vehicle(registry):
    t = _find(registry, "post_AddMileage")
    assert t["parameters"]["VehicleId"]["context_key"] == "vehicle_id", (
        "The AddMileage.VehicleId=person_id bug is back."
    )


def test_companies_tenant_id_points_to_tenant(registry):
    t = _find(registry, "post_Companies")
    assert t["parameters"]["TenantId"]["context_key"] == "tenant_id"


def test_orgunits_parent_points_to_orgunit(registry):
    t = _find(registry, "post_OrgUnits")
    assert t["parameters"]["ParentOrgUnitId"]["context_key"] == "orgunit_id"
    assert t["parameters"]["CompanyId"]["context_key"] == "company_id"
    assert t["parameters"]["TenantId"]["context_key"] == "tenant_id"


def test_no_tenant_param_still_routed_to_person(registry):
    """Zero TenantId-named params may still carry context_key='person_id'."""
    offenders = [
        f"{op}.{p}"
        for op, p, pdef in _iter_context_params(registry)
        if "tenant" in p.lower() and pdef.get("context_key") == "person_id"
    ]
    assert offenders == [], f"still routing tenant->person in: {offenders[:10]}"


def test_no_vehicle_param_still_routed_to_person(registry):
    """Same for Vehicle*Id / Equipment*Id."""
    import re
    rx = re.compile(r"(vehicle|equipment)[a-z]*id", re.I)
    offenders = [
        f"{op}.{p}"
        for op, p, pdef in _iter_context_params(registry)
        if rx.search(p) and pdef.get("context_key") == "person_id"
    ]
    assert offenders == [], f"still routing vehicle->person in: {offenders[:10]}"


def test_v6_aliases_still_intact(registry):
    """V6.1 must not clobber the output_key_aliases produced by V6."""
    assert _find(registry, "get_MasterData").get("output_key_aliases") == {"Id": "VehicleId"}
    assert _find(registry, "get_Persons").get("output_key_aliases") == {"Id": "PersonId"}

    # get_Persons.Filter was tagged context=person_id (vestigial; filter_template
    # is dead code, identity builds the Phone filter directly). The 2026-05-24
    # misclassification fix demoted it to user_input — a real Filter is a
    # user-typed search expression (e.g. manager "lista zaposlenika"), not an
    # auto-injected person UUID.
    persons_filter = _find(registry, "get_Persons")["parameters"]["Filter"]
    assert persons_filter["dependency_source"] == "user_input"
    assert _find(registry, "get_MasterData")["parameters"]["personId"]["required"] is True


def test_person_params_still_point_to_person(registry):
    """Sanity: params that really are personish must keep pointing at person_id."""
    person_params = [
        (op, p, pdef["context_key"])
        for op, p, pdef in _iter_context_params(registry)
        if p in ("PersonId", "DriverId", "UserId", "personId")
    ]
    assert person_params, "no person params found - scan is broken"
    for op, p, ck in person_params:
        assert ck == "person_id", f"{op}.{p} drifted to {ck}"
