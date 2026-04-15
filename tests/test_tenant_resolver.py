"""
TenantResolver lab tests — DB-backed (Postgres in prod, in-memory SQLite here).

Covers:
  - Phone normalization (E.164, 00<cc>, national 0-prefix, junk)
  - Cache hit (Redis stub) returns without touching DB
  - Cache miss → DB lookup → write-through cache
  - Inactive mapping is treated as unknown (refuse, not fall back)
  - NULL tenant_id is treated as unknown
  - DB outage returns None (V6.2: refuse, never default to env)
  - invalidate() drops the cached entry

Run:
    pytest tests/test_tenant_resolver.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, Optional

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from services.tenant_resolver import (  # noqa: E402
    CACHE_KEY_PREFIX,
    TenantResolver,
    _normalize_phone,
)


# ---------------------------------------------------------------------------
# Fixtures: in-memory aiosqlite, fake redis
# ---------------------------------------------------------------------------

CREATE_USER_MAPPINGS_SQL = """
CREATE TABLE user_mappings (
    id           TEXT PRIMARY KEY,
    phone_number TEXT NOT NULL UNIQUE,
    tenant_id    TEXT,
    is_active    INTEGER NOT NULL DEFAULT 1
)
"""

INSERT_USER_MAPPING_SQL = (
    "INSERT INTO user_mappings (id, phone_number, tenant_id, is_active) "
    "VALUES (:id, :phone, :tenant, :active)"
)


@pytest.fixture
async def session_factory():
    """Fresh in-memory aiosqlite database per test, schema bootstrapped."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.execute(text(CREATE_USER_MAPPINGS_SQL))
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _insert_mapping(
    factory,
    *,
    phone: str,
    tenant: Optional[str],
    active: bool = True,
    row_id: str = "row-1",
) -> None:
    async with factory() as session:
        await session.execute(
            text(INSERT_USER_MAPPING_SQL),
            {"id": row_id, "phone": phone, "tenant": tenant, "active": 1 if active else 0},
        )
        await session.commit()


class FakeRedis:
    """Minimal async Redis stub: get / set(ex=...) / delete."""

    def __init__(self) -> None:
        self.store: Dict[str, str] = {}
        self.set_calls: list = []  # (key, value, ttl)
        self.get_calls: int = 0

    async def get(self, key: str) -> Optional[str]:
        self.get_calls += 1
        return self.store.get(key)

    async def set(self, key: str, value: Any, ex: Optional[int] = None) -> bool:
        self.store[key] = str(value)
        self.set_calls.append((key, str(value), ex))
        return True

    async def delete(self, *keys: str) -> int:
        n = 0
        for k in keys:
            if k in self.store:
                del self.store[k]
                n += 1
        return n


# ---------------------------------------------------------------------------
# Normalization (deterministic, no I/O — same coverage as before)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("+385955087196", "+385955087196"),
    ("385955087196", "+385955087196"),
    ("00385955087196", "+385955087196"),
    ("0955087196", "+385955087196"),
    ("  +385 (95) 508-7196  ", "+385955087196"),
    ("", ""),
    ("---", ""),
])
def test_normalize_phone(raw, expected):
    assert _normalize_phone(raw) == expected


# ---------------------------------------------------------------------------
# DB-backed lookup
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cache_miss_then_db_hit_writes_through(session_factory):
    redis = FakeRedis()
    await _insert_mapping(session_factory, phone="+385955087196", tenant="tenant-A")

    resolver = TenantResolver(session_factory=session_factory, redis_client=redis)

    tid = await resolver.resolve_tenant_for_phone("+385955087196")
    assert tid == "tenant-A"

    # Write-through: the same key is now cached with TTL == 3600
    cached = redis.store.get(f"{CACHE_KEY_PREFIX}+385955087196")
    assert cached == "tenant-A"
    assert redis.set_calls and redis.set_calls[-1][2] == 3600


@pytest.mark.asyncio
async def test_cache_hit_skips_db(session_factory):
    """If cache has the value, DB is never touched."""
    redis = FakeRedis()
    redis.store[f"{CACHE_KEY_PREFIX}+385955087196"] = "tenant-CACHED"

    # Note: DB is empty — if resolver hits it, lookup would return None.
    resolver = TenantResolver(session_factory=session_factory, redis_client=redis)

    tid = await resolver.resolve_tenant_for_phone("+385955087196")
    assert tid == "tenant-CACHED"


@pytest.mark.asyncio
async def test_normalization_applied_before_lookup(session_factory):
    redis = FakeRedis()
    await _insert_mapping(session_factory, phone="+385955087196", tenant="tenant-A")

    resolver = TenantResolver(session_factory=session_factory, redis_client=redis)

    # National format with leading 0 → normalizes to the stored E.164 row.
    tid = await resolver.resolve_tenant_for_phone("0955087196")
    assert tid == "tenant-A"


@pytest.mark.asyncio
async def test_inactive_mapping_treated_as_unknown(session_factory):
    """is_active=False → refuse (V6.2)."""
    redis = FakeRedis()
    await _insert_mapping(
        session_factory, phone="+385955087196", tenant="tenant-A", active=False
    )
    resolver = TenantResolver(session_factory=session_factory, redis_client=redis)

    assert await resolver.resolve_tenant_for_phone("+385955087196") is None


@pytest.mark.asyncio
async def test_null_tenant_treated_as_unknown(session_factory):
    """Row exists but tenant_id IS NULL → refuse (V6.2)."""
    redis = FakeRedis()
    await _insert_mapping(session_factory, phone="+385955087196", tenant=None)
    resolver = TenantResolver(session_factory=session_factory, redis_client=redis)

    assert await resolver.resolve_tenant_for_phone("+385955087196") is None


@pytest.mark.asyncio
async def test_unknown_phone_returns_none_not_env(session_factory):
    """V6.2: misses MUST return None. Caller refuses; never defaults to env."""
    redis = FakeRedis()
    resolver = TenantResolver(session_factory=session_factory, redis_client=redis)

    assert await resolver.resolve_tenant_for_phone("+15555550000") is None
    assert await resolver.resolve_tenant_for_phone("") is None
    assert await resolver.resolve_tenant_for_phone("garbage") is None


@pytest.mark.asyncio
async def test_db_outage_returns_none_not_env(session_factory):
    """If DB raises, resolver MUST refuse (return None). No env fallback."""
    redis = FakeRedis()

    class _BoomFactory:
        def __call__(self):
            class _Ctx:
                async def __aenter__(self_inner):
                    raise RuntimeError("simulated DB outage")

                async def __aexit__(self_inner, *a):
                    return False
            return _Ctx()

    resolver = TenantResolver(session_factory=_BoomFactory(), redis_client=redis)
    assert await resolver.resolve_tenant_for_phone("+385955087196") is None


@pytest.mark.asyncio
async def test_invalidate_drops_cache(session_factory):
    redis = FakeRedis()
    await _insert_mapping(session_factory, phone="+385955087196", tenant="tenant-A")
    resolver = TenantResolver(session_factory=session_factory, redis_client=redis)

    # populate cache
    assert await resolver.resolve_tenant_for_phone("+385955087196") == "tenant-A"
    assert f"{CACHE_KEY_PREFIX}+385955087196" in redis.store

    await resolver.invalidate("+385955087196")
    assert f"{CACHE_KEY_PREFIX}+385955087196" not in redis.store
