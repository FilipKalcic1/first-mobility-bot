"""L0 — Identity & Context.

Resolves the user's identity (personId) and pre-fetches master data on
first contact. Caches in Redis with 5-minute TTL because:
  - mileage changes when driver writes it (rare)
  - registration / leasing change once a year
  - 5min keeps follow-up queries snappy without staleness

API contract — single class, single async method:

    ctx = IdentityContext(redis_client, gateway, settings)
    snapshot = await ctx.resolve(phone="385955087196")
    snapshot.person_id           # str | None
    snapshot.full_name           # "Marko Marić" | None
    snapshot.tenant_id           # str (multi-tenant)
    snapshot.vehicle_name        # "VW Golf" | None
    snapshot.licence_plate       # "BG-1234" | None
    snapshot.vin                 # ...
    snapshot.last_mileage        # int km | None
    snapshot.leasing_company     # str | None
    snapshot.is_first_contact    # True if not in cache (drives welcome)
    snapshot.is_known            # True if person_id resolved

If `person_id` cannot be resolved → snapshot.is_known = False, all
fields except `phone` and `is_first_contact` are None. Caller handles
"unknown user" scenario.

NEVER raises on transient API errors — returns degraded snapshot with
.errors list. The router decides whether to proceed or apologize.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

# TTL conservative — Pass 4 DEVIL critique: admin may deactivate driver
# mid-session. 30s caps the staleness window at one minute worst case
# (hit at second 29, served until next refresh). Trade-off: 1 extra
# Persons+MasterData call per minute of conversation. Worth the safety.
_CACHE_TTL_SECONDS = 30

# Redis key prefix.
_REDIS_PREFIX = "v2:identity:"


@dataclass
class IdentitySnapshot:
    """Frozen view of who the user is + their default vehicle context.

    All fields except `phone` are Optional — explicit None means "we
    tried to resolve and failed", absence of value (e.g. legacy cache
    miss) raises KeyError on access instead.
    """
    phone: str
    person_id: Optional[str] = None
    full_name: Optional[str] = None
    first_name: Optional[str] = None
    tenant_id: Optional[str] = None

    vehicle_id: Optional[str] = None
    vehicle_name: Optional[str] = None
    licence_plate: Optional[str] = None
    vin: Optional[str] = None
    last_mileage: Optional[int] = None
    leasing_company: Optional[str] = None
    co2_emission: Optional[float] = None
    registration_expiry: Optional[str] = None  # ISO date

    is_first_contact: bool = False
    is_known: bool = False
    errors: list[str] = field(default_factory=list)

    def to_cache_dict(self) -> dict:
        """Serialize for Redis. is_first_contact is recomputed on load."""
        return {
            "person_id": self.person_id,
            "full_name": self.full_name,
            "first_name": self.first_name,
            "tenant_id": self.tenant_id,
            "vehicle_id": self.vehicle_id,
            "vehicle_name": self.vehicle_name,
            "licence_plate": self.licence_plate,
            "vin": self.vin,
            "last_mileage": self.last_mileage,
            "leasing_company": self.leasing_company,
            "co2_emission": self.co2_emission,
            "registration_expiry": self.registration_expiry,
            "is_known": self.is_known,
        }

    @classmethod
    def from_cache_dict(cls, phone: str, data: dict) -> "IdentitySnapshot":
        return cls(
            phone=phone,
            person_id=data.get("person_id"),
            full_name=data.get("full_name"),
            first_name=data.get("first_name"),
            tenant_id=data.get("tenant_id"),
            vehicle_id=data.get("vehicle_id"),
            vehicle_name=data.get("vehicle_name"),
            licence_plate=data.get("licence_plate"),
            vin=data.get("vin"),
            last_mileage=data.get("last_mileage"),
            leasing_company=data.get("leasing_company"),
            co2_emission=data.get("co2_emission"),
            registration_expiry=data.get("registration_expiry"),
            is_first_contact=False,  # cache-hit means we've seen them
            is_known=bool(data.get("is_known", False)),
        )


class IdentityContext:
    """Resolves phone -> personId -> masterData via cached Redis state.

    The two upstream calls (Persons + MasterData) live in different
    MobilityOne services with potentially different tenants:
      - persons     -> /tenantmgt/Persons?Filter=Phone(=){phone}
      - masterdata  -> /automation/MasterData?personId={id}

    Tenant routing per service comes from settings; we don't hardcode
    here. The caller (gateway) handles auth header + tenant header.
    """

    def __init__(self, redis_client, gateway, settings):
        self._redis = redis_client
        self._gateway = gateway
        self._settings = settings

    @staticmethod
    def _normalize_phone(phone: str) -> str:
        """Strip whitespace, leading + and 00. Production phones are
        country-code prefixed (e.g. 385955087196).

        B3 note: this is intentionally distinct from
        `services.tenant_resolver._normalize_phone` which returns the
        same number prefixed with `+` (E.164). Different downstream
        APIs require different formats:
          - MobilityOne `/Persons?Filter=Phone(=){phone}` expects no `+`
          - Tenant lookup index keys include `+` (E.164 canonical)
        Do NOT consolidate without changing both API contracts.
        """
        s = (phone or "").strip().replace(" ", "").replace("-", "")
        if s.startswith("+"):
            s = s[1:]
        if s.startswith("00"):
            s = s[2:]
        return s

    async def resolve(self, phone: str) -> IdentitySnapshot:
        """Get identity snapshot for a phone. Caches both hit + miss."""
        normalized = self._normalize_phone(phone)
        if not normalized:
            return IdentitySnapshot(
                phone=phone, errors=["empty_phone"], is_first_contact=True
            )

        cached = await self._read_cache(normalized)
        if cached is not None:
            return cached

        snap = IdentitySnapshot(
            phone=normalized, is_first_contact=True
        )
        await self._populate_from_persons(snap)
        if snap.is_known and snap.person_id:
            await self._populate_from_masterdata(snap)

        await self._write_cache(normalized, snap)
        return snap

    async def invalidate(self, phone: str) -> None:
        """Drop cache entry — useful after the user updates mileage etc."""
        normalized = self._normalize_phone(phone)
        if not normalized:
            return
        try:
            await self._redis.delete(_REDIS_PREFIX + normalized)
        except Exception as e:  # noqa: BLE001 — never crash the bot
            logger.warning("identity cache invalidate failed: %s", e)

    # ------------------------------------------------------------------
    # Internals — kept small enough to hold in your head.
    # ------------------------------------------------------------------

    async def _read_cache(self, normalized: str) -> Optional[IdentitySnapshot]:
        try:
            raw = await self._redis.get(_REDIS_PREFIX + normalized)
        except Exception as e:  # noqa: BLE001
            logger.warning("identity cache read failed: %s", e)
            return None
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return IdentitySnapshot.from_cache_dict(normalized, data)
        except (ValueError, TypeError) as e:
            logger.warning("identity cache corrupt for %s: %s", normalized, e)
            return None

    async def _write_cache(
        self, normalized: str, snap: IdentitySnapshot
    ) -> None:
        try:
            await self._redis.setex(
                _REDIS_PREFIX + normalized,
                _CACHE_TTL_SECONDS,
                json.dumps(snap.to_cache_dict(), ensure_ascii=False),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("identity cache write failed: %s", e)

    async def _populate_from_persons(self, snap: IdentitySnapshot) -> None:
        """GET /tenantmgt/Persons?Filter=Phone(=){phone} → personId, name."""
        try:
            response = await self._gateway.call(
                method="GET",
                service="tenantmgt",
                path="/Persons",
                query_params={"Filter": f"Phone(=){snap.phone}"},
                tenant_id=self._settings.PERSONS_TENANT_ID,
            )
        except Exception as e:  # noqa: BLE001
            snap.errors.append(f"persons_lookup_error:{type(e).__name__}")
            return

        if not response.success:
            snap.errors.append(
                f"persons_http_{response.status_code or 'unknown'}"
            )
            return

        items = self._extract_items(response.data)
        if not items:
            # Phone not registered. Known-unknown — cached so we don't
            # hammer the API on every poll.
            snap.is_known = False
            return

        person = items[0]
        snap.person_id = person.get("Id") or person.get("id")
        first_name = person.get("FirstName") or person.get("firstName") or ""
        last_name = person.get("LastName") or person.get("lastName") or ""
        snap.first_name = first_name or None
        snap.full_name = (
            f"{first_name} {last_name}".strip() or None
        )
        snap.tenant_id = (
            person.get("TenantId")
            or person.get("tenantId")
            or self._settings.PERSONS_TENANT_ID
        )
        snap.is_known = bool(snap.person_id)

    async def _populate_from_masterdata(self, snap: IdentitySnapshot) -> None:
        """GET /automation/MasterData?personId={id} → vehicle context."""
        try:
            response = await self._gateway.call(
                method="GET",
                service="automation",
                path="/MasterData",
                query_params={"personId": snap.person_id},
                tenant_id=snap.tenant_id or self._settings.AUTOMATION_TENANT_ID,
            )
        except Exception as e:  # noqa: BLE001
            snap.errors.append(f"masterdata_lookup_error:{type(e).__name__}")
            return

        if not response.success:
            snap.errors.append(
                f"masterdata_http_{response.status_code or 'unknown'}"
            )
            return

        # MasterData responds with a single record (not a list per docs).
        data = response.data
        if isinstance(data, list):
            data = data[0] if data else {}
        if not isinstance(data, dict):
            return

        snap.vehicle_id = data.get("VehicleId") or data.get("vehicleId")
        snap.vehicle_name = (
            data.get("VehicleName")
            or data.get("FullVehicleName")
            or data.get("vehicleName")
        )
        snap.licence_plate = (
            data.get("LicencePlate") or data.get("licencePlate")
        )
        snap.vin = data.get("VIN") or data.get("Vin") or data.get("vin")
        last_km = data.get("LastMileage") or data.get("lastMileage")
        snap.last_mileage = int(last_km) if last_km is not None else None
        snap.leasing_company = (
            data.get("LeasingCompany") or data.get("leasingCompany")
        )
        co2 = data.get("Co2Emission") or data.get("co2Emission")
        snap.co2_emission = float(co2) if co2 is not None else None
        snap.registration_expiry = (
            data.get("RegistrationExpiry") or data.get("registrationExpiry")
        )

    @staticmethod
    def _extract_items(data: Any) -> list:
        """MobilityOne wraps lists in {data: [...]} sometimes."""
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            inner = data.get("data") or data.get("Data") or data.get("items")
            if isinstance(inner, list):
                return inner
            # Single-record dict acts like list of one.
            if "Id" in data or "id" in data:
                return [data]
        return []
