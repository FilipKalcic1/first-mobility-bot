"""Tests for CatalogScoper — per-(tenant, persona) tool catalog narrowing.

These tests use synthetic tool_data + tenant configs so they don't depend
on the real 950-tool catalog or any Damir audit being applied.
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
    """6 synthetic tools spanning the four persona buckets."""
    return {
        "tools": {
            "get_MasterData": {
                "operation_id": "get_MasterData",
                "personas_strict": ["driver"],
            },
            "post_AddMileage": {
                "operation_id": "post_AddMileage",
                "personas_strict": ["driver"],
            },
            "get_Expenses_Agg": {
                "operation_id": "get_Expenses_Agg",
                "personas_strict": ["manager"],
            },
            "post_Companies": {
                "operation_id": "post_Companies",
                "personas_strict": ["admin"],
            },
            "get_VehicleInputHelper_DistinctMakes": {
                "operation_id": "get_VehicleInputHelper_DistinctMakes",
                "personas_strict": ["internal"],
            },
            # Tool with no strict tag (legacy / missing audit) → only available
            # when no persona filter is applied.
            "get_Untagged": {
                "operation_id": "get_Untagged",
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

def test_no_tenant_config_no_persona_returns_all_tools(tool_data, tenants_dir):
    """Worst case: nothing configured at all → all tools allowed (legacy fallback)."""
    scoper = CatalogScoper(tool_data=tool_data, tenants_dir=tenants_dir)
    scope = scoper.scope(tenant_id=None, persona=None)
    assert scope == frozenset(tool_data["tools"].keys())


def test_persona_filter_only_keeps_matching_tools(tool_data, tenants_dir):
    """No tenant config but persona=driver → only the 2 driver-tagged tools."""
    scoper = CatalogScoper(tool_data=tool_data, tenants_dir=tenants_dir)
    scope = scoper.scope(tenant_id="any", persona="driver")
    assert scope == frozenset({"get_MasterData", "post_AddMileage"})


def test_default_tenant_subset_narrows_base(tool_data, tenants_dir):
    """_default config narrows the candidate set before persona filtering.

    Faza 14 (Filip 2026-05-19): persona is hierarchical — manager sees
    driver+manager tools, admin sees all three. Tests reflect that.
    """
    _write_tenant(tenants_dir, "_default", [
        "get_MasterData", "post_AddMileage", "get_Expenses_Agg",
    ])
    scoper = CatalogScoper(tool_data=tool_data, tenants_dir=tenants_dir)

    # Persona=driver intersected with _default → 2 driver tools
    driver_scope = scoper.scope(tenant_id="anyone", persona="driver")
    assert driver_scope == frozenset({"get_MasterData", "post_AddMileage"})

    # Persona=manager → hierarchy {driver, manager} intersected with _default
    # → driver tools (2) + manager tool (1) = 3 total
    mgr_scope = scoper.scope(tenant_id="anyone", persona="manager")
    assert mgr_scope == frozenset({
        "get_MasterData", "post_AddMileage", "get_Expenses_Agg",
    })

    # Admin → hierarchy {driver, manager, admin} intersected with _default
    # → all 3 in the list (post_Companies excluded from _default)
    admin_scope = scoper.scope(tenant_id="anyone", persona="admin")
    assert admin_scope == frozenset({
        "get_MasterData", "post_AddMileage", "get_Expenses_Agg",
    })


def test_per_tenant_override_takes_precedence(tool_data, tenants_dir):
    """tenants/{X}/tool_subset.json wins over _default."""
    _write_tenant(tenants_dir, "_default", ["get_MasterData"])
    _write_tenant(tenants_dir, "tenant_b", [
        "post_AddMileage", "get_Expenses_Agg",
    ])
    scoper = CatalogScoper(tool_data=tool_data, tenants_dir=tenants_dir)

    # tenant_a → falls back to _default (only 1 driver tool)
    a_scope = scoper.scope(tenant_id="tenant_a", persona="driver")
    assert a_scope == frozenset({"get_MasterData"})

    # tenant_b → uses its own subset (1 driver tool from its list)
    b_scope = scoper.scope(tenant_id="tenant_b", persona="driver")
    assert b_scope == frozenset({"post_AddMileage"})

    # tenant_b manager (Faza 14 hierarchy: {driver, manager}) →
    # both tools in tenant_b's subset (post_AddMileage = driver, get_Expenses_Agg = manager)
    b_mgr = scoper.scope(tenant_id="tenant_b", persona="manager")
    assert b_mgr == frozenset({"post_AddMileage", "get_Expenses_Agg"})


def test_empty_tenant_subset_falls_through_to_default(tool_data, tenants_dir):
    """Tenant file present but empty allowed_tool_ids → fall through to _default."""
    _write_tenant(tenants_dir, "_default", ["get_MasterData", "post_AddMileage"])
    _write_tenant(tenants_dir, "tenant_broken", [])  # empty list
    scoper = CatalogScoper(tool_data=tool_data, tenants_dir=tenants_dir)

    scope = scoper.scope(tenant_id="tenant_broken", persona="driver")
    assert scope == frozenset({"get_MasterData", "post_AddMileage"})


def test_internal_tools_filtered_out_for_driver_persona(tool_data, tenants_dir):
    """The whole point — internal-only tools never reach a driver's router."""
    scoper = CatalogScoper(tool_data=tool_data, tenants_dir=tenants_dir)
    scope = scoper.scope(tenant_id=None, persona="driver")
    assert "get_VehicleInputHelper_DistinctMakes" not in scope


def test_cache_returns_identical_frozenset(tool_data, tenants_dir):
    """Repeat calls with same args must be cached (no I/O on second call)."""
    scoper = CatalogScoper(tool_data=tool_data, tenants_dir=tenants_dir)
    a = scoper.scope(tenant_id="x", persona="driver")
    b = scoper.scope(tenant_id="x", persona="driver")
    assert a is b  # same frozenset object — cache hit


def test_tool_with_no_personas_strict_filtered_out_by_persona(tool_data, tenants_dir):
    """get_Untagged has personas_strict=None — excluded when any persona is set."""
    scoper = CatalogScoper(tool_data=tool_data, tenants_dir=tenants_dir)
    scope = scoper.scope(tenant_id=None, persona="driver")
    assert "get_Untagged" not in scope

    # But when no persona filter is applied AND no tenant subset, it's present
    scope_no_filter = scoper.scope(tenant_id=None, persona=None)
    assert "get_Untagged" in scope_no_filter


def test_mtime_invalidation_picks_up_updated_tool_subset(tool_data, tenants_dir):
    """When tool_subset.json is updated mid-process, next scope() call sees
    the new content — old workers don't need restart to pick up deploy pushes."""
    import os
    import time

    # Initial config: only get_MasterData allowed
    _write_tenant(tenants_dir, "_default", ["get_MasterData"])
    scoper = CatalogScoper(tool_data=tool_data, tenants_dir=tenants_dir)

    scope_v1 = scoper.scope(tenant_id="anyone", persona="driver")
    assert scope_v1 == frozenset({"get_MasterData"})

    # Simulate a deploy push: rewrite tool_subset.json with a different list.
    # Bump mtime explicitly so filesystem-second-resolution doesn't mask
    # the change (some FS only have 1s mtime granularity).
    subset_path = tenants_dir / "_default" / "tool_subset.json"
    new_path_mtime = subset_path.stat().st_mtime + 2.0
    _write_tenant(tenants_dir, "_default", [
        "get_MasterData", "post_AddMileage",
    ])
    os.utime(subset_path, (new_path_mtime, new_path_mtime))

    scope_v2 = scoper.scope(tenant_id="anyone", persona="driver")
    assert scope_v2 == frozenset({"get_MasterData", "post_AddMileage"}), (
        f"expected cache invalidation after file update, got {scope_v2}"
    )
    # Sanity: not the same object as before (truly recomputed)
    assert scope_v1 is not scope_v2


# ---------------------------------------------------------------------------
# Faza 14 (Filip 2026-05-19) — hierarchical persona scope
# ---------------------------------------------------------------------------


def test_driver_persona_sees_only_driver_tools(tool_data, tenants_dir):
    """Driver-tier user gets the smallest scope — just driver-tagged tools."""
    scoper = CatalogScoper(tool_data=tool_data, tenants_dir=tenants_dir)
    scope = scoper.scope(tenant_id=None, persona="driver")
    # Two driver tools in fixture; admin/manager/internal/untagged all out.
    assert scope == frozenset({"get_MasterData", "post_AddMileage"})


def test_manager_persona_sees_driver_and_manager_tools(tool_data, tenants_dir):
    """Manager-tier user inherits driver scope — natural fleet supervision flow."""
    scoper = CatalogScoper(tool_data=tool_data, tenants_dir=tenants_dir)
    scope = scoper.scope(tenant_id=None, persona="manager")
    assert scope == frozenset({
        "get_MasterData", "post_AddMileage",  # driver
        "get_Expenses_Agg",                    # manager
    })


def test_admin_persona_sees_all_user_facing_tools(tool_data, tenants_dir):
    """Admin-tier user inherits driver+manager. Internal and untagged tools
    remain excluded (untagged has personas_strict=None, internal is its
    own bucket outside the hierarchy)."""
    scoper = CatalogScoper(tool_data=tool_data, tenants_dir=tenants_dir)
    scope = scoper.scope(tenant_id=None, persona="admin")
    assert scope == frozenset({
        "get_MasterData", "post_AddMileage",  # driver
        "get_Expenses_Agg",                    # manager
        "post_Companies",                       # admin
    })


def test_agg_tools_are_NOT_filtered_as_internal_2026_05_19(tool_data, tenants_dir):
    """Faza 2026-05-19 (Filip bench finding): _Agg suffix was REMOVED
    from drop_internal regex. Manager-facing aggregation tools
    (e.g. get_CostCenters_Agg) must reach user candidate set; persona
    filter handles the few _Agg tools marked personas_strict=['internal']."""
    from services.router.catalog_scoper import is_internal_helper
    # These _Agg tools are tagged personas_strict=['manager'] in real registry
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


def test_unknown_persona_falls_back_to_exact_match(tool_data, tenants_dir):
    """Faza 14 contract: PERSONA_HIERARCHY dict only knows driver/manager/admin.
    A custom persona name (e.g., 'internal' for system jobs) falls back to
    exact-match — preserving legacy behavior for non-standard roles."""
    scoper = CatalogScoper(tool_data=tool_data, tenants_dir=tenants_dir)
    scope = scoper.scope(tenant_id=None, persona="internal")
    # Only the internal-tagged tool matches (no hierarchy, just exact match)
    assert scope == frozenset({"get_VehicleInputHelper_DistinctMakes"})


def test_mtime_invalidation_picks_up_added_tenant_override(tool_data, tenants_dir):
    """When a tenant-specific override file is ADDED post-startup, the next
    scope() call uses it instead of _default."""
    _write_tenant(tenants_dir, "_default", ["get_MasterData", "post_AddMileage"])
    scoper = CatalogScoper(tool_data=tool_data, tenants_dir=tenants_dir)

    # First call sees _default (no tenant_b override yet)
    scope_default = scoper.scope(tenant_id="tenant_b", persona="driver")
    assert scope_default == frozenset({"get_MasterData", "post_AddMileage"})

    # Now create tenant_b-specific override
    _write_tenant(tenants_dir, "tenant_b", ["post_AddMileage"])

    scope_override = scoper.scope(tenant_id="tenant_b", persona="driver")
    assert scope_override == frozenset({"post_AddMileage"}), (
        "tenant override should be picked up on next call, not cached as _default"
    )
