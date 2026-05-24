"""L0 — Identity & Context.

Resolves the user's identity (personId) and pre-fetches master data on
first contact. Caches in Redis with 30-second TTL — caps staleness if
admin deactivates a driver mid-session at 1 minute worst case (hit at
second 29, served until next refresh).

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

NEVER raises on transient API errors — returns degraded snapshot
(is_known=False or partial vehicle data) and logs a warning. The router
decides whether to proceed or apologize.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional

logger = logging.getLogger(__name__)

# TTL conservative — Pass 4 DEVIL critique: admin may deactivate driver
# mid-session. 30s caps the staleness window at one minute worst case
# (hit at second 29, served until next refresh). Trade-off: 1 extra
# Persons+MasterData call per minute of conversation. Worth the safety.
_CACHE_TTL_SECONDS = 30

# Redis key prefix.
_REDIS_PREFIX = "v2:identity:"

# Persona resolution: MobilityOne API doesn't (yet) expose role on the
# /Persons response, so we read per-tenant phone→persona overrides from
# config/tenants/{tenant_id}/personas.json. Anyone not in the override
# file is treated as "driver" — the most-restricted (safest) default.
Persona = Literal["driver", "manager", "admin"]
_DEFAULT_PERSONA: Persona = "driver"
# Repo-relative path; resolved against project root at lookup time.
_TENANTS_DIR_REL = Path("config") / "tenants"


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
    # Org context from /Persons response — names for the greeting, IDs for
    # context-param injection (Filip 2026-05-24: 28 tools require CompanyId/
    # OrgUnitId as context params; /Persons returns these IDs so we inject them
    # the same way as person_id/vehicle_id instead of letting them silently
    # fall through → 422).
    company_name: Optional[str] = None
    org_unit_name: Optional[str] = None
    company_id: Optional[str] = None
    org_unit_id: Optional[str] = None
    # Drives persona-restricted routing (Phase D 2026-05-16). Defaults to
    # "driver" if not overridden in config/tenants/{tenant_id}/personas.json.
    persona: Persona = _DEFAULT_PERSONA

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

    def to_cache_dict(self) -> dict:
        """Serialize for Redis. is_first_contact is recomputed on load."""
        return {
            "person_id": self.person_id,
            "full_name": self.full_name,
            "first_name": self.first_name,
            "tenant_id": self.tenant_id,
            "company_name": self.company_name,
            "org_unit_name": self.org_unit_name,
            "company_id": self.company_id,
            "org_unit_id": self.org_unit_id,
            "persona": self.persona,
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
            company_name=data.get("company_name"),
            org_unit_name=data.get("org_unit_name"),
            company_id=data.get("company_id"),
            org_unit_id=data.get("org_unit_id"),
            persona=data.get("persona") or _DEFAULT_PERSONA,
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

    def __init__(self, redis_client, gateway, settings, tenant_resolver=None):
        self._redis = redis_client
        self._gateway = gateway
        self._settings = settings
        # Optional dependency: when provided, identity lazy-onboards
        # successful Persons resolutions into user_mappings (so subsequent
        # messages can skip the API roundtrip via tenant_resolver). Tests
        # pass None; production factory wires the real singleton.
        self._tenant_resolver = tenant_resolver

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

    @staticmethod
    def _national_significant_number(phone: str) -> str:
        """Reduce a phone to its national significant number (NSN), so we can
        match across the formats MobilityOne actually stores Phone in.

        Verified live 2026-05-24: one tenant holds `385955087196`,
        `+385916659757` AND `0915245777` — the bot's old exact `Phone(=)`
        match only hit the bare `385…` form, leaving `+`/`0`-stored users
        unidentifiable. Steps: digits only → drop `00`/`385` country code
        (length-guarded so a national number that merely starts with 385 is
        not truncated) → drop one leading trunk `0`. All three examples
        collapse to the same NSN (e.g. `955087196`/`916659757`/`915245777`)."""
        digits = "".join(c for c in (phone or "") if c.isdigit())
        if digits.startswith("00"):
            digits = digits[2:]
        if digits.startswith("385") and len(digits) >= 11:
            digits = digits[3:]
        if digits.startswith("0"):
            digits = digits[1:]
        return digits

    async def resolve(
        self,
        phone: str,
        *,
        expected_tenant_id: Optional[str] = None,
    ) -> IdentitySnapshot:
        """Get identity snapshot for a phone. Caches both hit + miss.

        ``expected_tenant_id`` is a defense-in-depth check for multi-tenant
        safety: if provided (e.g. by the webhook layer that already resolved
        the phone→tenant binding via tenant_resolver), and the cached
        snapshot's tenant_id does NOT match, the cache entry is treated as
        stale/poisoned, dropped, and re-populated from MobilityOne. Prevents
        cross-tenant cache bleed in unusual deployment topologies
        (dev/prod sharing Redis, blue-green, or tenant migration).
        """
        normalized = self._normalize_phone(phone)
        if not normalized:
            logger.warning("identity resolve called with empty phone")
            return IdentitySnapshot(phone=phone, is_first_contact=True)

        cached = await self._read_cache(normalized)
        if cached is not None:
            if expected_tenant_id and cached.tenant_id != expected_tenant_id:
                logger.error(
                    "identity cache poisoned for phone ****%s: cached "
                    "tenant=%s but webhook resolved tenant=%s — invalidating "
                    "and re-fetching to prevent cross-tenant leak.",
                    normalized[-4:] if len(normalized) >= 4 else normalized,
                    cached.tenant_id, expected_tenant_id,
                )
                await self.invalidate(normalized)
                # fall through to re-fetch
            else:
                return cached

        # Cache miss BUT this may still NOT be the user's first contact —
        # the Redis snapshot expires after 30s (admin-deactivation safety),
        # but "have we ever welcomed this phone" is a lifetime fact that
        # lives in user_mappings (Postgres). Looking there prevents the bot
        # from re-firing the WELCOME message every >30s gap. Best-effort:
        # if the resolver lookup fails, default to True (safer — better to
        # re-greet than miss a legitimate first contact).
        already_welcomed = await self._was_phone_welcomed_before(normalized)
        snap = IdentitySnapshot(
            phone=normalized, is_first_contact=not already_welcomed
        )
        await self._populate_from_persons(snap)
        if snap.is_known and snap.person_id:
            await self._populate_from_masterdata(snap)
        # Resolve persona AFTER tenant_id is set, so we can read the
        # tenant's phone→persona override file.
        if snap.tenant_id:
            snap.persona = self._resolve_persona(snap.tenant_id, normalized)

        # Lazy onboarding: first time we successfully identify a phone,
        # write it to user_mappings so tenant_resolver can skip the
        # MobilityOne roundtrip on future messages. Best-effort —
        # if Postgres write fails, the user still gets a response from
        # this turn (we have the snapshot in hand); next message just
        # re-hits the API. NEVER raises.
        if snap.is_known and snap.tenant_id and snap.person_id:
            await self._maybe_upsert_user_mapping(snap)

        # Final guard: if caller specified expected_tenant_id and Persons
        # returned a DIFFERENT tenant, refuse — do not cache, return unknown.
        if (
            expected_tenant_id
            and snap.tenant_id
            and snap.tenant_id != expected_tenant_id
        ):
            logger.error(
                "identity Persons-API tenant mismatch for phone ****%s: "
                "API says %s, webhook says %s — refusing to bind identity.",
                normalized[-4:] if len(normalized) >= 4 else normalized,
                snap.tenant_id, expected_tenant_id,
            )
            return IdentitySnapshot(
                phone=normalized, is_first_contact=False, is_known=False,
            )

        await self._write_cache(normalized, snap)
        return snap

    async def _was_phone_welcomed_before(self, normalized: str) -> bool:
        """Check user_mappings for a prior successful resolve of this phone.

        Used to decide is_first_contact: TRUE only if the phone has NEVER
        been resolved before (lifetime, not session). Without this check,
        every Redis cache miss (~every 30s) would re-trigger the WELCOME
        message — bad UX.

        tenant_resolver normalizes to E.164 (with +), identity strips +,
        so we prepend + before passing through. Best-effort: returns False
        (= "treat as first contact") on any failure.
        """
        if self._tenant_resolver is None:
            return False
        try:
            e164 = normalized if normalized.startswith("+") else "+" + normalized
            existing_tenant = await self._tenant_resolver.resolve_tenant_for_phone(e164)
            return existing_tenant is not None
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "welcomed-before check failed for ****%s: %s — defaulting to first contact",
                normalized[-4:] if len(normalized) >= 4 else normalized, e,
            )
            return False

    async def _maybe_upsert_user_mapping(self, snap: IdentitySnapshot) -> None:
        """Best-effort: write the just-resolved identity into user_mappings.

        No-op when tenant_resolver is not configured (tests). Production
        engine factory wires the real resolver singleton.

        Phone format: tenant_resolver uses E.164 (leading +); we prepend +
        to match its normalization before passing through. Never raises —
        onboarding is a cache optimization, not a hard dependency.
        """
        if self._tenant_resolver is None:
            return
        try:
            e164 = snap.phone if snap.phone.startswith("+") else "+" + snap.phone
            await self._tenant_resolver.upsert_user_mapping(
                e164,
                tenant_id=snap.tenant_id,
                person_id=snap.person_id,
                display_name=snap.full_name,
            )
        except Exception as e:  # noqa: BLE001 — onboarding never blocks
            logger.warning(
                "user_mappings onboarding skipped for ****%s: %s",
                snap.phone[-4:] if snap.phone else "", e,
            )

    async def invalidate(self, phone: str) -> None:
        """Drop cache entry — useful after the user updates mileage etc."""
        normalized = self._normalize_phone(phone)
        if not normalized:
            return
        try:
            await self._redis.delete(_REDIS_PREFIX + normalized)
        except Exception as e:  # noqa: BLE001 — never crash the bot
            logger.warning("identity cache invalidate failed: %s", e)

    # ---- persona resolution (Phase D 2026-05-16) ----

    # Process-level cache: tenant_id → {normalized_phone: persona}
    # Loaded lazily once per tenant; reloaded on file mtime change.
    _persona_overrides_cache: dict[str, dict[str, "Persona"]] = {}
    _persona_overrides_mtime: dict[str, float] = {}

    @classmethod
    def _resolve_persona(cls, tenant_id: str, normalized_phone: str) -> "Persona":
        """Look up tenant's phone→persona map; fall back to driver.

        File: config/tenants/{tenant_id}/personas.json
            {
              "385951234567": "admin",
              "385991234567": "manager"
            }

        Anyone not listed is implicitly "driver". Missing file = all driver.
        """
        overrides = cls._load_persona_overrides(tenant_id)
        return overrides.get(normalized_phone, _DEFAULT_PERSONA)

    @classmethod
    def _load_persona_overrides(cls, tenant_id: str) -> dict[str, "Persona"]:
        # Repo root = three levels up from this file (services/v2/identity.py)
        repo_root = Path(__file__).resolve().parents[2]
        path = repo_root / _TENANTS_DIR_REL / tenant_id / "personas.json"
        try:
            mtime = path.stat().st_mtime if path.exists() else 0.0
        except OSError:
            return cls._persona_overrides_cache.get(tenant_id, {})

        cached_mtime = cls._persona_overrides_mtime.get(tenant_id, -1.0)
        if mtime == cached_mtime and tenant_id in cls._persona_overrides_cache:
            return cls._persona_overrides_cache[tenant_id]

        overrides: dict[str, Persona] = {}
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    for phone, persona in raw.items():
                        if persona in ("driver", "manager", "admin") and isinstance(phone, str):
                            overrides[phone.strip()] = persona  # type: ignore[assignment]
            except (OSError, ValueError) as e:
                logger.warning(
                    "personas.json unreadable for tenant=%s: %s — defaulting all to driver",
                    tenant_id, e,
                )
        cls._persona_overrides_cache[tenant_id] = overrides
        cls._persona_overrides_mtime[tenant_id] = mtime
        return overrides

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

    async def _query_persons(self, filter_str: str) -> tuple[bool, list]:
        """GET /tenantmgt/Persons?Filter=... → (api_ok, [person dicts]).

        `api_ok` distinguishes a SUCCESSFUL-but-empty result (genuine "no such
        row") from a transient failure (503/timeout) — the caller only does the
        contains fallback on a real empty, never on a failure. Uses
        ``settings.MOBILITY_TENANT_ID`` as x-tenant (Damir pilot = one tenant)."""
        try:
            response = await self._gateway.call(
                method="GET",
                service="tenantmgt",
                path="/Persons",
                query_params={"Filter": filter_str},
                tenant_id=self._settings.MOBILITY_TENANT_ID,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("persons lookup error: %s", type(e).__name__)
            return False, []
        if not response.success:
            logger.warning(
                "persons HTTP %s", response.status_code or "unknown",
            )
            return False, []
        return True, self._extract_items(response.data)

    async def _find_person_by_phone(self, phone: str) -> Optional[dict]:
        """Find the UNIQUE Person whose stored Phone matches `phone`, robust to
        MobilityOne's inconsistent phone formats (`385…`/`+385…`/`0…`).

        EXACT-first (precise, and the common case — numbers stored as bare
        `385…` resolve in one call, preserving prior behavior). Only when the
        exact filter SUCCEEDS but finds nothing do we fall back to a
        format-robust match: query ``Phone(contains){nsn}`` (substring catches
        the `+`/`0`-stored variants), then post-verify by EXACT NSN equality
        and require a UNIQUE match — `contains` could match a longer number and
        we must never bind the wrong person. We do NOT fall back on a transient
        API failure (no point re-querying a down endpoint)."""
        api_ok, items = await self._query_persons(f"Phone(=){phone}")
        if items:
            return items[0]
        nsn = self._national_significant_number(phone)
        if api_ok and len(nsn) >= 8:
            _, items = await self._query_persons(f"Phone(contains){nsn}")
            matches = [
                p for p in items
                if self._national_significant_number(
                    p.get("Phone") or p.get("phone") or ""
                ) == nsn
            ]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                logger.warning(
                    "ambiguous phone match: %d persons share NSN ***%s — "
                    "refusing to bind identity",
                    len(matches), nsn[-3:],
                )
        return None

    async def _populate_from_persons(self, snap: IdentitySnapshot) -> None:
        """Resolve personId + name + tenant by matching the WhatsApp sender's
        number against the Person ``Phone`` field (see _find_person_by_phone
        for the format-robust matching).

        Uses ``settings.MOBILITY_TENANT_ID`` as the x-tenant header. For
        Damir's pilot (one tenant) this is the right call. The Persons
        response carries the user's actual ``TenantId``, which the rest of
        the bot uses for subsequent tenant-scoped calls (MasterData, tools).

        Multi-tenant (1000+ tenants) deployment: this single env var is the
        bottleneck and will need re-architecture once we know how MobilityOne
        wants to handle cross-tenant phone lookup. NOT solved here.
        """
        person = await self._find_person_by_phone(snap.phone)
        if person is None:
            # Phone not registered (or no unique NSN match). Known-unknown —
            # cached so we don't hammer the API on every poll.
            snap.is_known = False
            return

        snap.person_id = person.get("Id") or person.get("id")
        first_name = person.get("FirstName") or person.get("firstName") or ""
        last_name = person.get("LastName") or person.get("lastName") or ""
        snap.first_name = first_name or None
        snap.full_name = (
            f"{first_name} {last_name}".strip() or None
        )
        # Org context for personalized greeting (FACT 2: Persons response
        # carries CompanyName + OrgUnitName per processed_tool_registry).
        snap.company_name = person.get("CompanyName") or person.get("companyName")
        snap.org_unit_name = person.get("OrgUnitName") or person.get("orgUnitName")
        # IDs for context-param injection (Filip 2026-05-24). Verified live:
        # /Persons returns CompanyId + OrgUnitId.
        snap.company_id = person.get("CompanyId") or person.get("companyId")
        snap.org_unit_id = person.get("OrgUnitId") or person.get("orgUnitId")
        # Strict tenant binding (multi-tenant safety): the TenantId field is
        # the ONLY authoritative source. If Persons omits it, treat the user
        # as unknown rather than silently routing into a default tenant.
        snap.tenant_id = person.get("TenantId") or person.get("tenantId")
        if not snap.tenant_id:
            logger.error(
                "persons response missing TenantId for phone ****%s — "
                "refusing to bind identity to env default. Phone treated as unknown.",
                snap.phone[-4:] if snap.phone else "",
            )
            snap.person_id = None
            snap.first_name = None
            snap.full_name = None
            snap.is_known = False
            return
        snap.is_known = bool(snap.person_id)

    async def _populate_from_masterdata(self, snap: IdentitySnapshot) -> None:
        """GET /automation/MasterData?personId={id} → vehicle context."""
        if not snap.tenant_id:
            # Multi-tenant safety: refuse to call MasterData without an
            # authoritative tenant_id. Vehicle context is optional —
            # downstream layers handle "no vehicle" gracefully.
            return
        try:
            response = await self._gateway.call(
                method="GET",
                service="automation",
                path="/MasterData",
                query_params={"personId": snap.person_id},
                tenant_id=snap.tenant_id,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("masterdata lookup error: %s", type(e).__name__)
            return

        if not response.success:
            logger.warning(
                "masterdata HTTP %s for person=%s",
                response.status_code or "unknown",
                snap.person_id,
            )
            return

        # MasterData responds with a single record (not a list per docs).
        data = response.data
        if isinstance(data, list):
            data = data[0] if data else {}
        if not isinstance(data, dict):
            return

        # Verified live 2026-05-24: MasterData carries the vehicle's id under
        # `Id` (cross-checked == Vehicles[plate].Id); `VehicleId` is null. The
        # old code read only VehicleId → vehicle_id stayed None → every
        # vehicle-scoped tool (context-param VehicleId) failed.
        snap.vehicle_id = (
            data.get("VehicleId") or data.get("vehicleId") or data.get("Id")
        )
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
        """MobilityOne wraps list responses under varying envelope keys.
        Accept the common ones (the bot had never parsed a live /Persons
        response, and the envelope key is not guaranteed to be `data`)."""
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in (
                "data", "Data", "items", "Items", "Result", "Results", "value",
            ):
                inner = data.get(key)
                if isinstance(inner, list):
                    return inner
            # Single-record dict acts like list of one.
            if "Id" in data or "id" in data:
                return [data]
        return []
