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
    PERSONS_TENANT_ID = "tenant-persons-uuid"
    AUTOMATION_TENANT_ID = "tenant-auto-uuid"


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
    assert snap.errors == []

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
        {"Id": "p1", "FirstName": "A", "LastName": "B"}
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
    assert any("persons_http_503" in e for e in snap.errors)
    # MasterData NOT called when persons failed
    assert len(gateway.calls) == 1


@pytest.mark.asyncio
async def test_masterdata_failure_keeps_personid_intact():
    redis, gateway, settings = FakeRedis(), FakeGateway(), FakeSettings()
    gateway.queue(FakeApiResponse(success=True, data=[
        {"Id": "person-1", "FirstName": "A", "LastName": "B"}
    ]))
    gateway.queue(FakeApiResponse(success=False, status_code=500))
    ctx = IdentityContext(redis, gateway, settings)

    snap = await ctx.resolve("385955087196")

    # Person still resolved; only vehicle data missing
    assert snap.is_known is True
    assert snap.person_id == "person-1"
    assert snap.full_name == "A B"
    assert snap.vehicle_name is None
    assert any("masterdata_http_500" in e for e in snap.errors)


@pytest.mark.asyncio
async def test_invalidate_drops_cache():
    redis, gateway, settings = FakeRedis(), FakeGateway(), FakeSettings()
    gateway.queue(FakeApiResponse(success=True, data=[
        {"Id": "p1", "FirstName": "X", "LastName": "Y"}
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
        {"Id": "p1", "FirstName": "Fresh", "LastName": "User"}
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
        {"Id": "p1", "FirstName": "Svejedno", "LastName": "Radi"}
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
    assert "empty_phone" in snap.errors
    assert len(gateway.calls) == 0
