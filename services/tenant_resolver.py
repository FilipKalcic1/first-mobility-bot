"""
TenantResolver — phone-number → TenantId for single-pod multi-tenant.

Source of truth: Postgres `user_mappings` table (already exists; see
models.UserMapping). Hot path goes through Redis cache so steady-state
lookup is sub-millisecond.

Design (V6.2 strict tenant isolation):

    resolve_tenant_for_phone(phone)
        ├─ Redis GET tenant_phone:{normalized}            ← O(1) hot path
        ├─ MISS → SELECT tenant_id FROM user_mappings
        │            WHERE phone_number = :p AND is_active = true
        │            LIMIT 1                              ← O(log n) on unique index
        ├─ HIT → Redis SETEX (TTL 1h) → return tenant_id
        └─ DB MISS / NULL tenant_id → return None
                                       (caller MUST refuse — no env fallback)

Cache invalidation: callers that mutate user_mappings (admin endpoint, GDPR
flow) MUST call `await get_tenant_resolver().invalidate(phone)` afterwards.
TTL of 1h bounds the staleness window for any path we miss.

Usage:
    # production webhook
    from services.tenant_resolver import resolve_tenant_for_phone
    tenant_id = await resolve_tenant_for_phone(sender)
    if not tenant_id:
        # refuse — never default to env

    # tests
    resolver = TenantResolver(
        session_factory=test_session_factory,   # in-memory aiosqlite
        redis_client=fake_redis,                # or None to skip cache
    )
    tid = await resolver.resolve_tenant_for_phone("+385955087196")
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Awaitable, Callable, Optional

from sqlalchemy import text

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Phone normalization (unchanged contract — covered by test_normalize_phone)
# ---------------------------------------------------------------------------

_NON_DIGIT = re.compile(r"\D+")


def _normalize_phone(raw: str, default_cc: str = "385") -> str:
    """Normalize an MSISDN to leading-`+` E.164.

    Rules (deterministic, no I/O):
      - strip all non-digits
      - "00<cc>..."        → "+<cc>..."
      - "0<8-9 digits>"    → "+<default_cc><rest>"  (Croatia by default)
      - already starts with country code → just prepend "+"
    """
    if not raw:
        return ""
    digits = _NON_DIGIT.sub("", raw)
    if not digits:
        return ""
    if digits.startswith("00"):
        digits = digits[2:]
    elif digits.startswith("0") and len(digits) <= 10:
        digits = default_cc + digits[1:]
    return "+" + digits


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------

CACHE_KEY_PREFIX = "tenant_phone:"
# 5 minutes — multi-tenant isolation rule must self-heal even if an admin
# forgets to call invalidate() after re-mapping a phone to a tenant. The
# extra DB hits at low QPS are negligible vs. the security risk of stale
# tenant routing.
DEFAULT_CACHE_TTL_SECONDS = 300


SessionFactory = Callable[[], Any]               # returns async context manager
RedisClient = Any                                # duck-typed: get/set/delete
RedisFactory = Callable[[], Awaitable[RedisClient]]


class TenantResolver:
    """
    Resolves WhatsApp phone → MobilityOne TenantId.

    Read-through cache: Redis first, Postgres on miss, populate Redis.
    Stateless aside from the injected dependencies — safe for concurrent calls.
    """

    def __init__(
        self,
        *,
        session_factory: Optional[SessionFactory] = None,
        redis_client: Optional[RedisClient] = None,
        redis_factory: Optional[RedisFactory] = None,
        cache_ttl: int = DEFAULT_CACHE_TTL_SECONDS,
    ) -> None:
        """
        Args:
            session_factory: zero-arg callable returning an async context
                manager that yields an AsyncSession. Defaults to lazy-loading
                ``database.AsyncSessionLocal``.
            redis_client: a pre-built async Redis client (overrides factory).
            redis_factory: zero-arg async callable returning the client
                (lazy production wiring). Defaults to ``webhook_simple.get_redis``.
            cache_ttl: seconds to keep a hit in Redis. None disables write-through.
        """
        self._session_factory = session_factory
        self._redis_client = redis_client
        self._redis_factory = redis_factory
        self._cache_ttl = cache_ttl
        self._lock = asyncio.Lock()

    # ── lazy dependency wiring ───────────────────────────────────────────

    def _get_session_factory(self) -> SessionFactory:
        if self._session_factory is not None:
            return self._session_factory
        # Lazy import — avoids loading SA engine at module import time.
        from database import AsyncSessionLocal
        self._session_factory = AsyncSessionLocal
        return self._session_factory

    async def _get_redis(self) -> Optional[RedisClient]:
        if self._redis_client is not None:
            return self._redis_client
        if self._redis_factory is not None:
            self._redis_client = await self._redis_factory()
            return self._redis_client
        # Production default: reuse the webhook's connection pool.
        try:
            from webhook_simple import get_redis  # local import: no circular
            self._redis_client = await get_redis()
            return self._redis_client
        except Exception as e:
            logger.warning("TenantResolver: no Redis available (%s)", e)
            return None

    # ── public API ───────────────────────────────────────────────────────

    async def resolve_tenant_for_phone(self, phone: str) -> Optional[str]:
        """
        Return the active TenantId for `phone`, or None if unknown.

        None means "refuse this request" — the caller MUST NOT default to
        env (V6.2 strict isolation).
        """
        normalized = _normalize_phone(phone)
        if not normalized:
            return None

        cache_key = f"{CACHE_KEY_PREFIX}{normalized}"

        # 1) Cache
        redis = await self._get_redis()
        if redis is not None:
            try:
                hit = await redis.get(cache_key)
                if hit:
                    return hit.decode() if isinstance(hit, (bytes, bytearray)) else str(hit)
            except Exception as e:
                logger.warning("Tenant cache read failed for ***%s: %s", normalized[-4:], e)

        # 2) Database (source of truth)
        tenant_id = await self._lookup_in_db(normalized)
        if tenant_id is None:
            return None

        # 3) Write-through cache (best effort)
        if redis is not None and self._cache_ttl:
            try:
                await redis.set(cache_key, tenant_id, ex=self._cache_ttl)
            except Exception as e:
                logger.warning("Tenant cache write failed for ***%s: %s", normalized[-4:], e)

        return tenant_id

    async def invalidate(self, phone: str) -> None:
        """
        Drop the cached entry for `phone`. Call after admin/GDPR mutations
        on `user_mappings` so subsequent lookups re-read from Postgres.
        """
        normalized = _normalize_phone(phone)
        if not normalized:
            return
        redis = await self._get_redis()
        if redis is None:
            return
        try:
            await redis.delete(f"{CACHE_KEY_PREFIX}{normalized}")
        except Exception as e:
            logger.warning("Tenant cache invalidate failed for ***%s: %s", normalized[-4:], e)

    async def upsert_user_mapping(
        self,
        phone: str,
        *,
        tenant_id: str,
        person_id: Optional[str] = None,
        display_name: Optional[str] = None,
    ) -> bool:
        """Lazy-onboard: insert (or refresh) user_mappings row after a
        successful MobilityOne /Persons lookup. Idempotent via phone unique
        index. Future messages from same phone skip the API roundtrip.

        Best-effort: returns False (no raise) on DB failure so the caller
        can still reply to the user — onboarding is a cache optimization,
        not a hard dependency. Cache is invalidated so the next
        resolve_tenant_for_phone() reads the fresh tenant_id.
        """
        normalized = _normalize_phone(phone)
        if not normalized or not tenant_id:
            return False
        factory = self._get_session_factory()
        try:
            async with factory() as session:
                # Postgres ON CONFLICT — keeps existing row's id/created_at,
                # refreshes tenant binding (in case user moved tenants).
                await session.execute(
                    text(
                        "INSERT INTO user_mappings "
                        "(phone_number, api_identity, display_name, tenant_id, is_active, created_at, updated_at) "
                        "VALUES (:phone, :pid, :name, :tid, TRUE, NOW(), NOW()) "
                        "ON CONFLICT (phone_number) DO UPDATE SET "
                        "  api_identity = EXCLUDED.api_identity, "
                        "  display_name = EXCLUDED.display_name, "
                        "  tenant_id    = EXCLUDED.tenant_id, "
                        "  is_active    = TRUE, "
                        "  updated_at   = NOW()"
                    ),
                    {
                        "phone": normalized,
                        "pid": person_id,
                        "name": display_name,
                        "tid": tenant_id,
                    },
                )
                await session.commit()
        except Exception as e:
            logger.warning(
                "user_mappings upsert failed for ***%s: %s — onboarding skipped",
                normalized[-4:], e,
            )
            return False
        # Invalidate cache so next resolve_tenant_for_phone reads the fresh row.
        await self.invalidate(normalized)
        return True

    async def purge_user_mapping(self, phone: str) -> int:
        """
        GDPR right-to-erasure: hard-DELETE the user_mappings row for `phone`
        and drop the tenant cache entry. Returns the number of rows deleted
        (0 if the phone was never registered, 1 on success).

        This is irreversible — the legal record of the deletion lives in
        ``gdpr:requests:{tenant_id}`` (see services/v2/gdpr_audit.py).
        """
        normalized = _normalize_phone(phone)
        if not normalized:
            return 0
        factory = self._get_session_factory()
        deleted = 0
        try:
            async with factory() as session:
                result = await session.execute(
                    text(
                        "DELETE FROM user_mappings WHERE phone_number = :phone"
                    ),
                    {"phone": normalized},
                )
                await session.commit()
                deleted = int(result.rowcount or 0)
        except Exception as e:
            logger.error(
                "user_mappings purge failed for ***%s: %s",
                normalized[-4:], e,
            )
            raise
        # Best-effort cache drop — even if it fails, the next resolve()
        # will hit DB (now empty) and return None.
        await self.invalidate(normalized)
        logger.warning(
            "GDPR purge: deleted %d row(s) from user_mappings for ***%s",
            deleted, normalized[-4:],
        )
        return deleted

    # ── internal ─────────────────────────────────────────────────────────

    async def _lookup_in_db(self, normalized_phone: str) -> Optional[str]:
        """Read the active mapping. Raw SQL keeps us dialect-agnostic and
        avoids importing the ORM model (no SA model coupling here)."""
        factory = self._get_session_factory()
        try:
            async with factory() as session:
                result = await session.execute(
                    text(
                        "SELECT tenant_id FROM user_mappings "
                        "WHERE phone_number = :phone AND is_active = :active "
                        "LIMIT 1"
                    ),
                    {"phone": normalized_phone, "active": True},
                )
                row = result.first()
        except Exception as e:
            # DB outage MUST NOT silently fall back to env (V6.2). Log loud,
            # return None so caller refuses with GATEWAY_MISSING_TENANT_CONTEXT.
            logger.error(
                "Tenant DB lookup failed for ***%s: %s — refusing call",
                normalized_phone[-4:], e,
            )
            return None

        if not row:
            return None
        tenant_id = row[0]
        if not tenant_id:
            # Row exists but tenant_id is NULL: treat as unmapped (refuse).
            return None
        return str(tenant_id)


# ---------------------------------------------------------------------------
# Module-level singleton (production callers use this 99% of the time)
# ---------------------------------------------------------------------------

_singleton: Optional[TenantResolver] = None
_singleton_lock = asyncio.Lock()


async def get_tenant_resolver() -> TenantResolver:
    global _singleton
    if _singleton is not None:
        return _singleton
    async with _singleton_lock:
        if _singleton is None:
            _singleton = TenantResolver()
    return _singleton


async def resolve_tenant_for_phone(phone: str) -> Optional[str]:
    """Convenience wrapper around the module singleton."""
    resolver = await get_tenant_resolver()
    return await resolver.resolve_tenant_for_phone(phone)
