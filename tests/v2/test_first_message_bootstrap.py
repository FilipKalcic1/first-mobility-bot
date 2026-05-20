"""First-message bootstrap flow (2026-05-16).

Tests Filip's specified UX:
  1. User sends ANY first message (greeting OR non-greeting)
  2. Bot calls /Persons + /MasterData
  3. Bot replies with personalized welcome (name + company + vehicle + mileage)
  4. Bot tells user "send your query in the next message"
  5. user_mappings is upserted so subsequent messages skip the API roundtrip

Explicit fakes (no MagicMock).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pytest

from services.v2.identity import IdentityContext, IdentitySnapshot
from services.v2.special_intents import detect_special_intent


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

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


@dataclass
class FakeApiResponse:
    success: bool
    data: object = None
    status_code: int = 200


class FakeGateway:
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


class CapturingTenantResolver:
    """Captures upsert_user_mapping calls so tests can assert on them.

    Also simulates user_mappings lookup: phones present in `known_phones`
    return their tenant via resolve_tenant_for_phone (= "we've welcomed
    this user before"). Other phones return None.
    """
    def __init__(self, known_phones: dict[str, str] | None = None):
        self.upserts: list[dict] = []
        # phone (E.164) → tenant_id
        self.known_phones: dict[str, str] = dict(known_phones or {})

    async def upsert_user_mapping(
        self, phone, *, tenant_id, person_id=None, display_name=None,
    ):
        self.upserts.append({
            "phone": phone,
            "tenant_id": tenant_id,
            "person_id": person_id,
            "display_name": display_name,
        })
        # Mirror the side-effect: after upsert, phone is known
        self.known_phones[phone] = tenant_id
        return True

    async def resolve_tenant_for_phone(self, phone):
        return self.known_phones.get(phone)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_first_persons_resolve_upserts_user_mapping_in_e164_format():
    """After a successful /Persons + /MasterData, the resolver upserts the
    phone in E.164 form (with leading +) so tenant_resolver lookups match."""
    redis, gateway, settings = FakeRedis(), FakeGateway(), FakeSettings()
    resolver = CapturingTenantResolver()
    gateway.queue(FakeApiResponse(success=True, data=[
        {
            "Id": "p-uuid-1",
            "FirstName": "Marko", "LastName": "Marić",
            "TenantId": "tenant-x",
            "CompanyName": "Damir d.o.o.",
            "OrgUnitName": "Vozni park",
        }
    ]))
    gateway.queue(FakeApiResponse(success=True, data={
        "VehicleName": "VW Golf", "LicencePlate": "ZG-1234",
        "LastMileage": 45000,
    }))

    ctx = IdentityContext(redis, gateway, settings, tenant_resolver=resolver)
    snap = await ctx.resolve("385955087196")  # bot's normalization: no +

    # Identity resolved
    assert snap.is_known is True
    assert snap.full_name == "Marko Marić"
    assert snap.tenant_id == "tenant-x"
    assert snap.company_name == "Damir d.o.o."
    assert snap.org_unit_name == "Vozni park"

    # Upsert happened with E.164 phone (with +) to match tenant_resolver
    assert len(resolver.upserts) == 1
    upsert = resolver.upserts[0]
    assert upsert["phone"] == "+385955087196"
    assert upsert["tenant_id"] == "tenant-x"
    assert upsert["person_id"] == "p-uuid-1"
    assert upsert["display_name"] == "Marko Marić"


@pytest.mark.asyncio
async def test_no_upsert_when_persons_lookup_fails():
    """Failed Persons → no person_id → no upsert (don't write garbage rows)."""
    redis, gateway, settings = FakeRedis(), FakeGateway(), FakeSettings()
    resolver = CapturingTenantResolver()
    gateway.queue(FakeApiResponse(success=False, status_code=503))

    ctx = IdentityContext(redis, gateway, settings, tenant_resolver=resolver)
    snap = await ctx.resolve("385955087196")

    assert snap.is_known is False
    assert resolver.upserts == []


@pytest.mark.asyncio
async def test_no_resolver_means_no_upsert_attempt():
    """Test default: when tenant_resolver=None, identity still works but
    skips the onboarding write. Keeps tests fast (no DB)."""
    redis, gateway, settings = FakeRedis(), FakeGateway(), FakeSettings()
    gateway.queue(FakeApiResponse(success=True, data=[
        {"Id": "p1", "FirstName": "A", "LastName": "B", "TenantId": "t1"}
    ]))
    gateway.queue(FakeApiResponse(success=True, data={
        "VehicleName": "Car", "LicencePlate": "X-1",
    }))

    ctx = IdentityContext(redis, gateway, settings)  # tenant_resolver=None
    snap = await ctx.resolve("385955087196")

    # Identity still works
    assert snap.is_known is True
    assert snap.first_name == "A"
    # No assertion about upsert — it's a no-op when resolver is None


@pytest.mark.asyncio
async def test_welcome_fires_once_per_lifetime_even_across_cache_expiry():
    """OPCIJA A (welcome once per lifetime): after the first successful
    resolve writes user_mappings, subsequent cache misses MUST NOT mark
    is_first_contact=True. Prevents WELCOME spam every >30s gap."""
    redis, gateway, settings = FakeRedis(), FakeGateway(), FakeSettings()
    resolver = CapturingTenantResolver()  # phone NOT in known_phones initially

    # First message: Persons + MasterData responses queued
    gateway.queue(FakeApiResponse(success=True, data=[
        {"Id": "p1", "FirstName": "Marko", "LastName": "M", "TenantId": "t-x"}
    ]))
    gateway.queue(FakeApiResponse(success=True, data={
        "VehicleName": "VW", "LicencePlate": "ZG-1",
    }))

    ctx = IdentityContext(redis, gateway, settings, tenant_resolver=resolver)
    snap_first = await ctx.resolve("385955087196")
    assert snap_first.is_first_contact is True, "first ever resolve should set is_first_contact=True"
    # Resolver was updated to know this phone now
    assert "+385955087196" in resolver.known_phones

    # Simulate Redis cache expiry (>30s gap) — delete the cached snapshot
    redis.store.pop("v2:identity:385955087196", None)

    # Second cache miss after user_mappings is populated
    gateway.queue(FakeApiResponse(success=True, data=[
        {"Id": "p1", "FirstName": "Marko", "LastName": "M", "TenantId": "t-x"}
    ]))
    gateway.queue(FakeApiResponse(success=True, data={
        "VehicleName": "VW", "LicencePlate": "ZG-1",
    }))

    snap_second = await ctx.resolve("385955087196")
    assert snap_second.is_first_contact is False, (
        "second resolve (after cache expiry but with user_mappings populated) "
        "MUST NOT mark is_first_contact=True — that would re-fire WELCOME"
    )
    # Snapshot itself still resolves normally
    assert snap_second.is_known is True
    assert snap_second.first_name == "Marko"


@pytest.mark.asyncio
async def test_welcome_fires_first_time_when_user_mappings_empty():
    """Belt-and-suspenders: confirms TRUE first contact still triggers welcome."""
    redis, gateway, settings = FakeRedis(), FakeGateway(), FakeSettings()
    # known_phones is EMPTY → resolve_tenant_for_phone returns None
    resolver = CapturingTenantResolver(known_phones={})

    gateway.queue(FakeApiResponse(success=True, data=[
        {"Id": "p1", "FirstName": "Iva", "LastName": "I", "TenantId": "t-y"}
    ]))
    gateway.queue(FakeApiResponse(success=True, data={"VehicleName": "X", "LicencePlate": "Y"}))

    ctx = IdentityContext(redis, gateway, settings, tenant_resolver=resolver)
    snap = await ctx.resolve("385999999999")
    assert snap.is_first_contact is True


@pytest.mark.asyncio
async def test_welcomed_check_failure_defaults_to_first_contact():
    """If the resolver throws when checking, default to first contact (safer
    UX: better to re-greet than miss a legitimate first contact)."""

    class BrokenResolver:
        async def resolve_tenant_for_phone(self, phone):
            raise RuntimeError("redis down")

        async def upsert_user_mapping(self, *a, **kw):
            return False

    redis, gateway, settings = FakeRedis(), FakeGateway(), FakeSettings()
    gateway.queue(FakeApiResponse(success=True, data=[
        {"Id": "p1", "FirstName": "A", "LastName": "B", "TenantId": "t1"}
    ]))
    gateway.queue(FakeApiResponse(success=True, data={"VehicleName": "X", "LicencePlate": "Y"}))

    ctx = IdentityContext(redis, gateway, settings, tenant_resolver=BrokenResolver())
    snap = await ctx.resolve("385955087196")
    # Resolver lookup failed → default True → WELCOME would fire (safe)
    assert snap.is_first_contact is True
    assert snap.is_known is True


@pytest.mark.asyncio
async def test_upsert_failure_does_not_break_identity_resolve():
    """Onboarding must NEVER block the user's response. Resolver throws,
    identity still returns the snapshot."""

    class FlakeyResolver:
        async def upsert_user_mapping(self, *a, **kw):
            raise RuntimeError("db down")

    redis, gateway, settings = FakeRedis(), FakeGateway(), FakeSettings()
    gateway.queue(FakeApiResponse(success=True, data=[
        {"Id": "p1", "FirstName": "Iva", "LastName": "I", "TenantId": "t1"}
    ]))
    gateway.queue(FakeApiResponse(success=True, data={
        "VehicleName": "X", "LicencePlate": "Y",
    }))

    ctx = IdentityContext(redis, gateway, settings, tenant_resolver=FlakeyResolver())
    snap = await ctx.resolve("385955087196")

    assert snap.is_known is True
    assert snap.full_name == "Iva I"


# ---------------------------------------------------------------------------
# Welcome template — verifies Filip's UX spec
# ---------------------------------------------------------------------------

def test_welcome_includes_name_company_vehicle_mileage_and_invite():
    """The first-message greeting must include all available context AND
    explicitly invite the user to send their query in the next message."""
    match = detect_special_intent(
        "asdf",  # ANY query triggers welcome on first contact
        is_first_contact=True,
        first_name="Marko",
        company_name="Damir d.o.o.",
        vehicle_name="VW Golf",
        licence_plate="ZG-1234",
        last_mileage=45000,
    )
    assert match is not None
    assert match.intent == "WELCOME"
    r = match.response
    assert "Marko" in r
    assert "Damir d.o.o." in r
    assert "VW Golf" in r
    assert "ZG-1234" in r
    assert "45.000" in r or "45000" in r  # Croatian thousands separator OR raw
    assert "sljedeć" in r.lower()  # "u sljedećoj poruci"


def test_welcome_handles_no_vehicle_gracefully():
    """User without dodijeljeno vozilo gets reservation prompt."""
    match = detect_special_intent(
        "Bok!",
        is_first_contact=True,
        first_name="Iva",
        company_name="Test d.o.o.",
        # No vehicle fields
    )
    assert match is not None
    r = match.response
    assert "Iva" in r
    assert "Test d.o.o." in r
    assert "rezerviraj" in r.lower()


def test_welcome_triggers_on_any_first_message_not_just_greeting():
    """The whole point: ANY first message, not just 'Bok!'."""
    for query in ("asdf", "kolika mi je km", "trebam pomoć", "12345", "??"):
        match = detect_special_intent(
            query,
            is_first_contact=True,
            first_name="Marko",
            vehicle_name="VW Golf",
        )
        assert match is not None and match.intent == "WELCOME", (
            f"Query {query!r} did not trigger WELCOME on first contact"
        )


def test_welcome_does_not_trigger_on_second_message():
    """is_first_contact=False → no welcome, normal routing path."""
    match = detect_special_intent(
        "kolika km",
        is_first_contact=False,
        first_name="Marko",
        vehicle_name="VW Golf",
    )
    # Either None (not a special intent) or GREETING (if "kolika" matched
    # something else) — but NOT WELCOME.
    assert match is None or match.intent != "WELCOME"
