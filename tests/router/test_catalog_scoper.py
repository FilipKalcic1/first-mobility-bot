"""Tests for CatalogScoper — per-tenant tool catalog narrowing.

These tests use synthetic tool_data + tenant configs so they don't depend
on the real 950-tool catalog or any Damir audit being applied.

Persona filter was removed 2026-05-28 (Filip rip) — backend OAuth scope is
the real ACL. Scoper now does tenant_subset + methods + drop_internal only.
The `personas_strict` field stays in fixtures because the real registry has
it (harmless metadata), but no test asserts on persona behavior.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.router.catalog_scoper import CatalogScoper


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tool_data() -> dict:
    """6 synthetic tools across HTTP methods + an internal helper."""
    return {
        "tools": {
            "get_MasterData": {
                "operation_id": "get_MasterData", "method": "GET",
                "personas_strict": ["driver"],
            },
            "post_AddMileage": {
                "operation_id": "post_AddMileage", "method": "POST",
                "personas_strict": ["driver"],
            },
            "get_Expenses_Agg": {
                "operation_id": "get_Expenses_Agg", "method": "GET",
                "personas_strict": ["manager"],
            },
            "post_Companies": {
                "operation_id": "post_Companies", "method": "POST",
                "personas_strict": ["admin"],
            },
            "get_VehicleInputHelper_DistinctMakes": {
                "operation_id": "get_VehicleInputHelper_DistinctMakes", "method": "GET",
                "personas_strict": ["internal"],
            },
            "get_Untagged": {
                "operation_id": "get_Untagged", "method": "GET",
                "personas_strict": None,
            },
        }
    }


@pytest.fixture
def tenants_dir(tmp_path: Path) -> Path:
    """Empty tenants dir; specific tests will populate it."""
    d = tmp_path / "tenants"
    d.mkdir()
    return d


def _write_tenant(tenants_dir: Path, tenant_id: str, allowed: list[str]) -> None:
    sub = tenants_dir / tenant_id
    sub.mkdir(exist_ok=True)
    (sub / "tool_subset.json").write_text(
        json.dumps({
            "tenant_id": tenant_id,
            "allowed_tool_ids": allowed,
        }),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_no_tenant_config_returns_all_tools(tool_data, tenants_dir):
    """Worst case: nothing configured at all → all tools allowed (legacy fallback)."""
    scoper = CatalogScoper(tool_data=tool_data, tenants_dir=tenants_dir)
    scope = scoper.scope(tenant_id=None)
    assert scope == frozenset(tool_data["tools"].keys())


def test_default_tenant_subset_narrows_base(tool_data, tenants_dir):
    """_default config narrows the candidate set to its allowed list."""
    _write_tenant(tenants_dir, "_default", [
        "get_MasterData", "post_AddMileage", "get_Expenses_Agg",
    ])
    scoper = CatalogScoper(tool_data=tool_data, tenants_dir=tenants_dir)

    scope = scoper.scope(tenant_id="anyone")
    assert scope == frozenset({
        "get_MasterData", "post_AddMileage", "get_Expenses_Agg",
    })


def test_per_tenant_override_takes_precedence(tool_data, tenants_dir):
    """tenants/{X}/tool_subset.json wins over _default."""
    _write_tenant(tenants_dir, "_default", ["get_MasterData"])
    _write_tenant(tenants_dir, "tenant_b", [
        "post_AddMileage", "get_Expenses_Agg",
    ])
    scoper = CatalogScoper(tool_data=tool_data, tenants_dir=tenants_dir)

    # tenant_a → falls back to _default
    a_scope = scoper.scope(tenant_id="tenant_a")
    assert a_scope == frozenset({"get_MasterData"})

    # tenant_b → uses its own subset
    b_scope = scoper.scope(tenant_id="tenant_b")
    assert b_scope == frozenset({"post_AddMileage", "get_Expenses_Agg"})


def test_empty_tenant_subset_results_in_unrestricted_scope(tool_data, tenants_dir):
    """Tenant file present but empty `allowed_tool_ids` → _tenant_subset ignores
    the file (warning logged) and returns None → scope() falls back to "all
    tools allowed" (NOT to _default — the loader picked the tenant path and
    never re-checks _default). Documents current behavior."""
    _write_tenant(tenants_dir, "_default", ["get_MasterData", "post_AddMileage"])
    _write_tenant(tenants_dir, "tenant_broken", [])
    scoper = CatalogScoper(tool_data=tool_data, tenants_dir=tenants_dir)

    scope = scoper.scope(tenant_id="tenant_broken")
    assert scope == frozenset(tool_data["tools"].keys())


def test_methods_filter_keeps_only_matching_method(tool_data, tenants_dir):
    """methods={'GET'} narrows to GET-only tools."""
    scoper = CatalogScoper(tool_data=tool_data, tenants_dir=tenants_dir)
    scope = scoper.scope(tenant_id=None, methods=frozenset({"GET"}))
    assert scope == frozenset({
        "get_MasterData", "get_Expenses_Agg",
        "get_VehicleInputHelper_DistinctMakes", "get_Untagged",
    })


def test_methods_filter_post(tool_data, tenants_dir):
    """methods={'POST'} narrows to POST-only tools."""
    scoper = CatalogScoper(tool_data=tool_data, tenants_dir=tenants_dir)
    scope = scoper.scope(tenant_id=None, methods=frozenset({"POST"}))
    assert scope == frozenset({"post_AddMileage", "post_Companies"})


def test_drop_internal_filters_internal_helpers(tool_data, tenants_dir):
    """drop_internal=True removes tools matching the internal-helper regex
    (e.g. _DistinctMakes/_GroupBy/_ProjectTo/...). Used by Model A action
    picker to keep candidate cards clean of UI-helper endpoints."""
    scoper = CatalogScoper(tool_data=tool_data, tenants_dir=tenants_dir)
    scope = scoper.scope(tenant_id=None, drop_internal=True)
    assert "get_VehicleInputHelper_DistinctMakes" not in scope
    # User-facing tools (incl. _Agg which is NOT in the regex) still present
    assert "get_MasterData" in scope
    assert "get_Expenses_Agg" in scope


def test_cache_returns_identical_frozenset(tool_data, tenants_dir):
    """Repeat calls with same args must be cached (no I/O on second call)."""
    scoper = CatalogScoper(tool_data=tool_data, tenants_dir=tenants_dir)
    a = scoper.scope(tenant_id="x")
    b = scoper.scope(tenant_id="x")
    assert a is b  # same frozenset object — cache hit


def test_cache_separates_by_methods_and_drop_internal(tool_data, tenants_dir):
    """Different (methods, drop_internal) combos cached independently."""
    scoper = CatalogScoper(tool_data=tool_data, tenants_dir=tenants_dir)
    get_scope = scoper.scope(tenant_id="x", methods=frozenset({"GET"}))
    post_scope = scoper.scope(tenant_id="x", methods=frozenset({"POST"}))
    no_internal = scoper.scope(tenant_id="x", drop_internal=True)
    assert get_scope != post_scope
    assert get_scope != no_internal


def test_mtime_invalidation_picks_up_updated_tool_subset(tool_data, tenants_dir):
    """When tool_subset.json is updated mid-process, next scope() call sees
    the new content — old workers don't need restart to pick up deploy pushes."""
    import os

    _write_tenant(tenants_dir, "_default", ["get_MasterData"])
    scoper = CatalogScoper(tool_data=tool_data, tenants_dir=tenants_dir)

    scope_v1 = scoper.scope(tenant_id="anyone")
    assert scope_v1 == frozenset({"get_MasterData"})

    # Simulate a deploy push: rewrite tool_subset.json with a different list.
    # Bump mtime explicitly so FS second-resolution doesn't mask the change.
    subset_path = tenants_dir / "_default" / "tool_subset.json"
    new_path_mtime = subset_path.stat().st_mtime + 2.0
    _write_tenant(tenants_dir, "_default", [
        "get_MasterData", "post_AddMileage",
    ])
    os.utime(subset_path, (new_path_mtime, new_path_mtime))

    scope_v2 = scoper.scope(tenant_id="anyone")
    assert scope_v2 == frozenset({"get_MasterData", "post_AddMileage"}), (
        f"expected cache invalidation after file update, got {scope_v2}"
    )
    assert scope_v1 is not scope_v2


def test_agg_tools_are_NOT_filtered_as_internal_2026_05_19(tool_data, tenants_dir):
    """2026-05-19 (Filip bench finding): `_Agg` suffix was REMOVED from the
    drop_internal regex. Manager-facing aggregation tools (e.g.
    get_CostCenters_Agg) must reach the candidate set. Other suffixes stay."""
    from services.router.catalog_scoper import is_internal_helper
    assert is_internal_helper("get_CostCenters_Agg") is False
    assert is_internal_helper("get_Companies_Agg") is False
    assert is_internal_helper("get_LatestPersonPeriodicActivities_Agg") is False
    # Other suffixes still classified as internal
    assert is_internal_helper("get_Foo_DistinctMakes") is True
    assert is_internal_helper("get_Foo_GroupBy") is True
    assert is_internal_helper("get_Foo_ProjectTo") is True
    assert is_internal_helper("get_Foo_metadata") is True
    assert is_internal_helper("delete_Foo_DeleteByCriteria") is True
    assert is_internal_helper("post_Foo_multipatch") is True


def test_mtime_invalidation_picks_up_added_tenant_override(tool_data, tenants_dir):
    """When a tenant-specific override file is ADDED post-startup, the next
    scope() call uses it instead of _default."""
    _write_tenant(tenants_dir, "_default", ["get_MasterData", "post_AddMileage"])
    scoper = CatalogScoper(tool_data=tool_data, tenants_dir=tenants_dir)

    # First call sees _default (no tenant_b override yet)
    scope_default = scoper.scope(tenant_id="tenant_b")
    assert scope_default == frozenset({"get_MasterData", "post_AddMileage"})

    # Now create tenant_b-specific override
    _write_tenant(tenants_dir, "tenant_b", ["post_AddMileage"])

    scope_override = scoper.scope(tenant_id="tenant_b")
    assert scope_override == frozenset({"post_AddMileage"}), (
        "tenant override should be picked up on next call, not cached as _default"
    )
