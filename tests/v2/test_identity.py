"""Tests for v2 L0 — IdentityContext.

Pin behavior — does NOT depend on real Azure / MobilityOne. All
external calls are stubbed via fakes (no MagicMock fairy dust that
hides real bugs).
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from services.v2.identity import (
    IdentityContext,
    IdentitySnapshot,
    _CACHE_TTL_SECONDS,
)


# --------------------------------------------------------------------------
# Fakes (deliberately not MagicMock — explicit + readable)
# --------------------------------------------------------------------------


class FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    async def get(self, key):
        return self.store.get(key)

    async def setex(self, key, ttl, value):
        self.store[key] = value
        self.ttls[key] = ttl

    async def delete(self, key):
        self.store.pop(key, None)
        self.ttls.pop(key, None)


@dataclass
class FakeApiResponse:
    success: bool
    data: object = None
    status_code: int = 200


class FakeGateway:
    """Fake API gateway. Records calls + returns scripted responses."""

    def __init__(self):
        self.calls: list[dict] = []
        self.scripted: list[FakeApiResponse] = []

    def queue(self, response: FakeApiResponse):
        self.scripted.append(response)

    async def call(self, **kwargs):
        self.calls.append(kwargs)
        if self.scripted:
            return self.scripted.pop(0)
        return FakeApiResponse(success=False, status_code=500)


class FakeSettings:
    MOBILITY_TENANT_ID = "tenant-fake-uuid"


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_phone_normalization_strips_plus_and_zeros():
    redis, gateway, settings = FakeRedis(), FakeGateway(), FakeSettings()
    gateway.queue(FakeApiResponse(success=True, data=[]))  # persons miss
    ctx = IdentityContext(redis, gateway, settings)

    snap = await ctx.resolve("+385 95 508-7196")

    assert snap.phone == "385955087196"
    # Cache key normalized — only one entry per logical phone
    assert "v2:identity:385955087196" in redis.store


@pytest.mark.asyncio
async def test_unknown_phone_caches_negative_result():
    redis, gateway, settings = FakeRedis(), FakeGateway(), FakeSettings()
    gateway.queue(FakeApiResponse(success=True, data=[]))

    ctx = IdentityContext(redis, gateway, settings)
    snap = await ctx.resolve("385999999999")

    assert snap.is_known is False
    assert snap.is_first_contact is True
    # Negative cache prevents hammering Persons endpoint
    raw = redis.store["v2:identity:385999999999"]
    cached = json.loads(raw)
    assert cached["is_known"] is False


@pytest.mark.asyncio
async def test_known_phone_resolves_full_context():
    redis, gateway, settings = FakeRedis(), FakeGateway(), FakeSettings()
    gateway.queue(FakeApiResponse(success=True, data=[
        {
            "Id": "person-uuid-1",
            "FirstName": "Marko",
            "LastName": "Marić",
            "TenantId": "tenant-x",
        }
    ]))
    gateway.queue(FakeApiResponse(success=True, data={
        "VehicleId": "veh-1",
        "VehicleName": "VW Golf",
        "LicencePlate": "BG-1234",
        "VIN": "WVWZZZ1KZ8M001234",
        "LastMileage": 45000,
        "LeasingCompany": "Leasing AB d.o.o.",
        "Co2Emission": 119.5,
        "RegistrationExpiry": "2026-08-15",
    }))

    ctx = IdentityContext(redis, gateway, settings)
    snap = await ctx.resolve("385955087196")

    assert snap.is_known is True
    assert snap.person_id == "person-uuid-1"
    assert snap.full_name == "Marko Marić"
    assert snap.first_name == "Marko"
    assert snap.tenant_id == "tenant-x"
    assert snap.vehicle_name == "VW Golf"
    assert snap.licence_plate == "BG-1234"
    assert snap.last_mileage == 45000
    assert snap.leasing_company == "Leasing AB d.o.o."
    assert snap.co2_emission == 119.5
    assert snap.registration_expiry == "2026-08-15"
    assert snap.is_first_contact is True

    # Two API calls: persons + masterdata
    assert len(gateway.calls) == 2
    assert gateway.calls[0]["service"] == "tenantmgt"
    assert gateway.calls[0]["path"] == "/Persons"
    assert gateway.calls[1]["service"] == "automation"
    assert gateway.calls[1]["path"] == "/MasterData"


@pytest.mark.asyncio
async def test_second_call_uses_cache_zero_api_hits():
    redis, gateway, settings = FakeRedis(), FakeGateway(), FakeSettings()
    gateway.queue(FakeApiResponse(success=True, data=[
        {"Id": "p1", "FirstName": "A", "LastName": "B", "TenantId": "t1"}
    ]))
    gateway.queue(FakeApiResponse(success=True, data={
        "VehicleName": "Test", "LicencePlate": "XY-1"
    }))
    ctx = IdentityContext(redis, gateway, settings)

    snap1 = await ctx.resolve("385955087196")
    assert snap1.is_first_contact is True
    api_hits_first = len(gateway.calls)

    snap2 = await ctx.resolve("385955087196")
    assert snap2.is_first_contact is False  # cache hit
    assert snap2.vehicle_name == "Test"
    assert snap2.licence_plate == "XY-1"

    # No NEW API hits on second call
    assert len(gateway.calls) == api_hits_first


@pytest.mark.asyncio
async def test_persons_api_failure_yields_degraded_snapshot_no_crash():
    redis, gateway, settings = FakeRedis(), FakeGateway(), FakeSettings()
    gateway.queue(FakeApiResponse(success=False, status_code=503))
    ctx = IdentityContext(redis, gateway, settings)

    snap = await ctx.resolve("385955087196")

    assert snap.is_known is False
    # MasterData NOT called when persons failed
    assert len(gateway.calls) == 1


@pytest.mark.asyncio
async def test_masterdata_failure_keeps_personid_intact():
    redis, gateway, settings = FakeRedis(), FakeGateway(), FakeSettings()
    gateway.queue(FakeApiResponse(success=True, data=[
        {"Id": "person-1", "FirstName": "A", "LastName": "B", "TenantId": "t1"}
    ]))
    gateway.queue(FakeApiResponse(success=False, status_code=500))
    ctx = IdentityContext(redis, gateway, settings)

    snap = await ctx.resolve("385955087196")

    # Person still resolved; only vehicle data missing
    assert snap.is_known is True
    assert snap.person_id == "person-1"
    assert snap.full_name == "A B"
    assert snap.vehicle_name is None


@pytest.mark.asyncio
async def test_invalidate_drops_cache():
    redis, gateway, settings = FakeRedis(), FakeGateway(), FakeSettings()
    gateway.queue(FakeApiResponse(success=True, data=[
        {"Id": "p1", "FirstName": "X", "LastName": "Y", "TenantId": "t1"}
    ]))
    gateway.queue(FakeApiResponse(success=True, data={
        "VehicleName": "T", "LicencePlate": "P-1"
    }))
    ctx = IdentityContext(redis, gateway, settings)

    await ctx.resolve("385955087196")
    assert "v2:identity:385955087196" in redis.store

    await ctx.invalidate("385955087196")
    assert "v2:identity:385955087196" not in redis.store


@pytest.mark.asyncio
async def test_corrupt_cache_falls_back_to_fresh_resolution():
    redis, gateway, settings = FakeRedis(), FakeGateway(), FakeSettings()
    redis.store["v2:identity:385955087196"] = "not json {{"
    gateway.queue(FakeApiResponse(success=True, data=[
        {"Id": "p1", "FirstName": "Fresh", "LastName": "User", "TenantId": "t1"}
    ]))
    gateway.queue(FakeApiResponse(success=True, data={
        "VehicleName": "Auto", "LicencePlate": "BG-1"
    }))
    ctx = IdentityContext(redis, gateway, settings)

    snap = await ctx.resolve("385955087196")
    assert snap.full_name == "Fresh User"  # corrupt cache ignored


@pytest.mark.asyncio
async def test_redis_unavailable_does_not_crash():
    """Bot must serve users even if Redis is down (degraded mode)."""

    class BrokenRedis:
        async def get(self, key):
            raise ConnectionError("redis down")

        async def setex(self, *a, **kw):
            raise ConnectionError("redis down")

        async def delete(self, *a, **kw):
            raise ConnectionError("redis down")

    gateway, settings = FakeGateway(), FakeSettings()
    gateway.queue(FakeApiResponse(success=True, data=[
        {"Id": "p1", "FirstName": "Svejedno", "LastName": "Radi", "TenantId": "t1"}
    ]))
    gateway.queue(FakeApiResponse(success=True, data={
        "VehicleName": "Auto", "LicencePlate": "X-1"
    }))

    ctx = IdentityContext(BrokenRedis(), gateway, settings)
    snap = await ctx.resolve("385955087196")
    assert snap.is_known is True
    assert snap.full_name == "Svejedno Radi"


@pytest.mark.asyncio
async def test_ttl_is_set_correctly():
    redis, gateway, settings = FakeRedis(), FakeGateway(), FakeSettings()
    gateway.queue(FakeApiResponse(success=True, data=[]))
    ctx = IdentityContext(redis, gateway, settings)

    await ctx.resolve("385955087196")
    assert redis.ttls["v2:identity:385955087196"] == _CACHE_TTL_SECONDS


@pytest.mark.asyncio
async def test_empty_phone_returns_unknown_no_api_call():
    redis, gateway, settings = FakeRedis(), FakeGateway(), FakeSettings()
    ctx = IdentityContext(redis, gateway, settings)

    snap = await ctx.resolve("")
    assert snap.is_known is False
    assert len(gateway.calls) == 0


@pytest.mark.asyncio
async def test_expected_tenant_id_mismatch_invalidates_cache():
    """R1 regression: defense-in-depth against cross-tenant cache bleed.
    If the webhook layer resolved phone X → tenant B, but a stale cache
    entry says tenant A, we must invalidate and re-fetch — NOT return the
    poisoned snapshot."""
    redis, gateway, settings = FakeRedis(), FakeGateway(), FakeSettings()
    # First fetch caches phone X under tenant-A
    gateway.queue(FakeApiResponse(success=True, data=[
        {"Id": "p-A", "FirstName": "A", "LastName": "A", "TenantId": "tenant-A"}
    ]))
    gateway.queue(FakeApiResponse(success=True, data={
        "VehicleName": "VW", "LicencePlate": "X-1",
    }))
    ctx = IdentityContext(redis, gateway, settings)
    snap_A = await ctx.resolve("385955087196")
    assert snap_A.tenant_id == "tenant-A"
    assert "v2:identity:385955087196" in redis.store

    # Webhook resolved THIS phone to tenant-B (e.g. user got migrated, or
    # this is a tenant-leak attempt). We must NOT serve tenant-A's cached
    # snapshot. Queue a fresh Persons response that now reports tenant-B.
    gateway.queue(FakeApiResponse(success=True, data=[
        {"Id": "p-B", "FirstName": "B", "LastName": "B", "TenantId": "tenant-B"}
    ]))
    gateway.queue(FakeApiResponse(success=True, data={
        "VehicleName": "Skoda", "LicencePlate": "Y-2",
    }))

    snap_B = await ctx.resolve("385955087196", expected_tenant_id="tenant-B")
    assert snap_B.tenant_id == "tenant-B"
    assert snap_B.person_id == "p-B"
    assert snap_B.vehicle_name == "Skoda"


@pytest.mark.asyncio
async def test_expected_tenant_mismatch_at_persons_returns_unknown():
    """If webhook said tenant-B but Persons API returns tenant-A, refuse
    to bind. Don't cache, don't return the snapshot."""
    redis, gateway, settings = FakeRedis(), FakeGateway(), FakeSettings()
    gateway.queue(FakeApiResponse(success=True, data=[
        {"Id": "p1", "FirstName": "A", "LastName": "B", "TenantId": "tenant-A"}
    ]))
    # MasterData would not be called because we refuse early. No queue.
    ctx = IdentityContext(redis, gateway, settings)
    snap = await ctx.resolve("385955087196", expected_tenant_id="tenant-B")
    assert snap.is_known is False
    assert snap.tenant_id is None
    # Cache write skipped — webhook + Persons disagree
    assert "v2:identity:385955087196" not in redis.store


@pytest.mark.asyncio
async def test_persona_defaults_to_driver_when_no_override_file(tmp_path, monkeypatch):
    """Phase D 2026-05-16: anyone not in tenant's personas.json is "driver"."""
    from services.v2 import identity as identity_mod

    # Reset module-level cache to avoid cross-test bleed
    identity_mod.IdentityContext._persona_overrides_cache.clear()
    identity_mod.IdentityContext._persona_overrides_mtime.clear()
    monkeypatch.setattr(identity_mod, "_TENANTS_DIR_REL", tmp_path)

    redis, gateway, settings = FakeRedis(), FakeGateway(), FakeSettings()
    gateway.queue(FakeApiResponse(success=True, data=[
        {"Id": "p1", "FirstName": "Vozač", "LastName": "Test", "TenantId": "tenant-A"}
    ]))
    gateway.queue(FakeApiResponse(success=True, data={"VehicleName": "Auto"}))

    ctx = IdentityContext(redis, gateway, settings)
    snap = await ctx.resolve("385955087196")
    assert snap.tenant_id == "tenant-A"
    assert snap.persona == "driver"


@pytest.mark.asyncio
async def test_persona_override_file_maps_phone_to_manager(tmp_path, monkeypatch):
    """personas.json maps specific phones to manager/admin; others stay driver."""
    from services.v2 import identity as identity_mod

    identity_mod.IdentityContext._persona_overrides_cache.clear()
    identity_mod.IdentityContext._persona_overrides_mtime.clear()
    # Patch via the absolute path the production code resolves.
    # The code does: repo_root / _TENANTS_DIR_REL / tenant_id / "personas.json"
    # We replace _TENANTS_DIR_REL with an absolute tmp_path so the join gives our test dir.
    monkeypatch.setattr(identity_mod, "_TENANTS_DIR_REL", tmp_path)

    tenant_dir = tmp_path / "tenant-A"
    tenant_dir.mkdir()
    (tenant_dir / "personas.json").write_text(
        json.dumps({
            "385955087196": "manager",
            "385991234567": "admin",
        }),
        encoding="utf-8",
    )

    # User with manager-tagged phone
    redis, gateway, settings = FakeRedis(), FakeGateway(), FakeSettings()
    gateway.queue(FakeApiResponse(success=True, data=[
        {"Id": "p1", "FirstName": "Manager", "LastName": "X", "TenantId": "tenant-A"}
    ]))
    gateway.queue(FakeApiResponse(success=True, data={"VehicleName": "Auto"}))
    ctx = IdentityContext(redis, gateway, settings)
    snap = await ctx.resolve("385955087196")
    assert snap.persona == "manager"

    # Different phone in same tenant → still driver
    redis2, gateway2 = FakeRedis(), FakeGateway()
    gateway2.queue(FakeApiResponse(success=True, data=[
        {"Id": "p2", "FirstName": "Other", "LastName": "Y", "TenantId": "tenant-A"}
    ]))
    gateway2.queue(FakeApiResponse(success=True, data={"VehicleName": "Auto"}))
    ctx2 = IdentityContext(redis2, gateway2, settings)
    snap2 = await ctx2.resolve("385000000000")
    assert snap2.persona == "driver"


@pytest.mark.asyncio
async def test_persona_survives_cache_roundtrip(tmp_path, monkeypatch):
    """Persona must be serialized in to_cache_dict and restored from cache."""
    from services.v2 import identity as identity_mod

    identity_mod.IdentityContext._persona_overrides_cache.clear()
    identity_mod.IdentityContext._persona_overrides_mtime.clear()
    monkeypatch.setattr(identity_mod, "_TENANTS_DIR_REL", tmp_path)

    tenant_dir = tmp_path / "tenant-A"
    tenant_dir.mkdir()
    (tenant_dir / "personas.json").write_text(
        json.dumps({"385955087196": "admin"}), encoding="utf-8",
    )

    redis, gateway, settings = FakeRedis(), FakeGateway(), FakeSettings()
    gateway.queue(FakeApiResponse(success=True, data=[
        {"Id": "p1", "FirstName": "A", "LastName": "B", "TenantId": "tenant-A"}
    ]))
    gateway.queue(FakeApiResponse(success=True, data={"VehicleName": "X"}))

    ctx = IdentityContext(redis, gateway, settings)
    snap1 = await ctx.resolve("385955087196")
    assert snap1.persona == "admin"

    # Second call hits the cache
    snap2 = await ctx.resolve("385955087196")
    assert snap2.is_first_contact is False
    assert snap2.persona == "admin"


@pytest.mark.asyncio
async def test_persons_without_tenantid_is_treated_as_unknown():
    """R3 regression: Persons API may omit TenantId. We refuse to bind to
    the env-default tenant — that was a silent cross-tenant leak. Instead
    the user is treated as unknown, and MasterData is NOT called."""
    redis, gateway, settings = FakeRedis(), FakeGateway(), FakeSettings()
    gateway.queue(FakeApiResponse(success=True, data=[
        # Note: NO TenantId — simulates API contract drift
        {"Id": "person-1", "FirstName": "Marko", "LastName": "Marić"}
    ]))
    # No second response queued — MasterData must NOT be called
    ctx = IdentityContext(redis, gateway, settings)

    snap = await ctx.resolve("385955087196")

    assert snap.is_known is False
    assert snap.person_id is None
    assert snap.tenant_id is None
    assert snap.full_name is None
    # MasterData call MUST be skipped — only Persons was called
    assert len(gateway.calls) == 1
    assert gateway.calls[0]["path"] == "/Persons"


# --------------------------------------------------------------------------
# Robust phone matching (Filip 2026-05-24) — MobilityOne stores Phone in
# inconsistent formats (385…/+385…/0…); exact-first then NSN-contains fallback.
# --------------------------------------------------------------------------


def test_national_significant_number_collapses_all_formats():
    """All stored variants of one number reduce to the same NSN."""
    nsn = IdentityContext._national_significant_number
    assert nsn("385955087196") == "955087196"     # bare E.164
    assert nsn("+385916659757") == "916659757"     # with +
    assert nsn("0915245777") == "915245777"        # local trunk 0
    assert nsn("00385955087196") == "955087196"    # 00 intl prefix
    assert nsn("385 95-508 7196") == "955087196"   # separators
    # The same person reachable two ways collapses identically.
    assert nsn("385955087196") == nsn("0955087196")


@pytest.mark.asyncio
async def test_exact_match_resolves_in_one_persons_call():
    """Number stored as bare `385…` → exact filter hits immediately, no
    contains fallback (preserves prior behavior + API load)."""
    redis, gateway, settings = FakeRedis(), FakeGateway(), FakeSettings()
    gateway.queue(FakeApiResponse(success=True, data=[
        {"Id": "p1", "FirstName": "Filip", "LastName": "K",
         "Phone": "385955087196", "TenantId": "t1"}
    ]))
    gateway.queue(FakeApiResponse(success=True, data={"VehicleName": "VW"}))
    ctx = IdentityContext(redis, gateway, settings)

    snap = await ctx.resolve("385955087196")

    assert snap.is_known is True and snap.person_id == "p1"
    # persons (exact) + masterdata = 2 calls; NO contains fallback
    assert len(gateway.calls) == 2
    assert gateway.calls[0]["query_params"]["Filter"] == "Phone(=)385955087196"


@pytest.mark.asyncio
async def test_phone_stored_with_plus_resolves_via_contains_fallback():
    """Stored `+385916659757`; bot queries bare `385916659757` → exact empty →
    NSN-contains fallback finds + post-verifies the record."""
    redis, gateway, settings = FakeRedis(), FakeGateway(), FakeSettings()
    gateway.queue(FakeApiResponse(success=True, data=[]))          # exact miss
    gateway.queue(FakeApiResponse(success=True, data=[             # contains hit
        {"Id": "pm", "FirstName": "Mladen", "LastName": "H",
         "Phone": "+385916659757", "TenantId": "t1"}
    ]))
    gateway.queue(FakeApiResponse(success=True, data={"VehicleName": "Auto"}))
    ctx = IdentityContext(redis, gateway, settings)

    snap = await ctx.resolve("385916659757")

    assert snap.is_known is True and snap.person_id == "pm"
    assert gateway.calls[0]["query_params"]["Filter"] == "Phone(=)385916659757"
    assert gateway.calls[1]["query_params"]["Filter"] == "Phone(contains)916659757"


@pytest.mark.asyncio
async def test_phone_stored_local_zero_resolves_via_contains_fallback():
    """Stored `0915245777` (local) → resolved via NSN contains."""
    redis, gateway, settings = FakeRedis(), FakeGateway(), FakeSettings()
    gateway.queue(FakeApiResponse(success=True, data=[]))          # exact miss
    gateway.queue(FakeApiResponse(success=True, data=[
        {"Id": "pj", "FirstName": "marija", "LastName": "z",
         "Phone": "0915245777", "TenantId": "t1"}
    ]))
    gateway.queue(FakeApiResponse(success=True, data={"VehicleName": "Auto"}))
    ctx = IdentityContext(redis, gateway, settings)

    snap = await ctx.resolve("385915245777")  # Infobip sends E.164

    assert snap.is_known is True and snap.person_id == "pj"
    assert gateway.calls[1]["query_params"]["Filter"] == "Phone(contains)915245777"


@pytest.mark.asyncio
async def test_contains_fallback_post_verifies_exact_nsn():
    """A contains hit whose NSN does NOT equal the caller's is rejected (the
    substring matched a different, longer number) → user stays unknown."""
    redis, gateway, settings = FakeRedis(), FakeGateway(), FakeSettings()
    gateway.queue(FakeApiResponse(success=True, data=[]))          # exact miss
    gateway.queue(FakeApiResponse(success=True, data=[             # spurious
        {"Id": "px", "FirstName": "Wrong", "LastName": "Person",
         "Phone": "385915245777123", "TenantId": "t1"}  # NSN differs
    ]))
    ctx = IdentityContext(redis, gateway, settings)

    snap = await ctx.resolve("385915245777")

    assert snap.is_known is False
    assert snap.person_id is None


@pytest.mark.asyncio
async def test_contains_fallback_ambiguous_refuses_to_bind():
    """Two persons share the NSN → refuse to bind (never guess)."""
    redis, gateway, settings = FakeRedis(), FakeGateway(), FakeSettings()
    gateway.queue(FakeApiResponse(success=True, data=[]))          # exact miss
    gateway.queue(FakeApiResponse(success=True, data=[
        {"Id": "a", "FirstName": "A", "LastName": "A",
         "Phone": "385915245777", "TenantId": "t1"},
        {"Id": "b", "FirstName": "B", "LastName": "B",
         "Phone": "+385915245777", "TenantId": "t1"},
    ]))
    ctx = IdentityContext(redis, gateway, settings)

    snap = await ctx.resolve("385915245777")

    assert snap.is_known is False
    assert snap.person_id is None


@pytest.mark.asyncio
async def test_api_failure_does_not_trigger_contains_fallback():
    """A 503 on the exact query must NOT trigger a second (contains) query —
    no point re-hitting a down endpoint. Exactly one persons call."""
    redis, gateway, settings = FakeRedis(), FakeGateway(), FakeSettings()
    gateway.queue(FakeApiResponse(success=False, status_code=503))
    ctx = IdentityContext(redis, gateway, settings)

    snap = await ctx.resolve("385916659757")

    assert snap.is_known is False
    assert len(gateway.calls) == 1


@pytest.mark.asyncio
async def test_vehicle_id_falls_back_to_masterdata_id():
    """MasterData carries the vehicle id under `Id` (VehicleId is null live).
    vehicle_id must populate from `Id` so vehicle-scoped tools work."""
    redis, gateway, settings = FakeRedis(), FakeGateway(), FakeSettings()
    gateway.queue(FakeApiResponse(success=True, data=[
        {"Id": "p1", "FirstName": "F", "LastName": "K",
         "Phone": "385955087196", "TenantId": "t1"}
    ]))
    gateway.queue(FakeApiResponse(success=True, data={
        "Id": "49ede20e-vehicle",      # vehicle id lives here
        "VehicleId": None,
        "FullVehicleName": "DA053F VW Passat",
        "LicencePlate": "DA053F",
        "LastMileage": 30000,
    }))
    ctx = IdentityContext(redis, gateway, settings)

    snap = await ctx.resolve("385955087196")

    assert snap.vehicle_id == "49ede20e-vehicle"
    assert snap.vehicle_name == "DA053F VW Passat"
    assert snap.licence_plate == "DA053F"


def test_extract_items_handles_varied_envelope_keys():
    """/Persons + list endpoints wrap rows under different keys; all parse."""
    ex = IdentityContext._extract_items
    row = {"Id": "x"}
    assert ex([row]) == [row]
    assert ex({"data": [row]}) == [row]
    assert ex({"Items": [row]}) == [row]
    assert ex({"Result": [row]}) == [row]
    assert ex({"value": [row]}) == [row]
    assert ex({"Id": "x"}) == [{"Id": "x"}]      # single-record dict
    assert ex({"nope": 1}) == []


@pytest.mark.asyncio
async def test_company_and_orgunit_id_captured_and_cached():
    """Filip 2026-05-24: /Persons returns CompanyId + OrgUnitId — capture them
    (for context-param injection) + survive the Redis cache round-trip. Without
    this, 28 tools that need CompanyId/OrgUnitId as context silently 422'd."""
    redis, gateway, settings = FakeRedis(), FakeGateway(), FakeSettings()
    gateway.queue(FakeApiResponse(success=True, data=[
        {"Id": "p1", "FirstName": "F", "LastName": "K", "Phone": "385955087196",
         "TenantId": "t1", "CompanyId": "comp-7", "OrgUnitId": "ou-3"}
    ]))
    gateway.queue(FakeApiResponse(success=True, data={"VehicleName": "VW"}))
    ctx = IdentityContext(redis, gateway, settings)

    snap = await ctx.resolve("385955087196")
    assert snap.company_id == "comp-7"
    assert snap.org_unit_id == "ou-3"

    # Cache round-trip (2nd resolve = cache hit) must preserve them.
    snap2 = await ctx.resolve("385955087196")
    assert snap2.is_first_contact is False
    assert snap2.company_id == "comp-7"
    assert snap2.org_unit_id == "ou-3"


def test_minimal_identity_exposes_company_and_orgunit_keys():
    """_minimal_identity must expose company_id + orgunit_id with the EXACT
    keys the registry uses as context_key ('orgunit_id', no underscore), else
    the executor's identity_summary.get(context_key) misses."""
    from services.v2.engine import V2Engine
    snap = IdentitySnapshot(
        phone="385955087196", tenant_id="t1", person_id="p1",
        vehicle_id="v1", company_id="comp-7", org_unit_id="ou-3",
    )
    ident = V2Engine._minimal_identity(snap)
    assert ident["company_id"] == "comp-7"
    assert ident["orgunit_id"] == "ou-3"   # key matches registry context_key
    assert ident["tenant_id"] == "t1"
    assert ident["vehicle_id"] == "v1"
