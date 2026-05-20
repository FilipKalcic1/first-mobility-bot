"""Tests for TenantResolver.purge_user_mapping — GDPR right-to-erasure.

These tests run in any env (no FastAPI dependency). Endpoint-level tests
for the HTTP route live in tests/test_gdpr_process_endpoint.py (skipped
when fastapi is not installed).
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from services.tenant_resolver import TenantResolver  # noqa: E402


CREATE_USER_MAPPINGS_SQL = """
CREATE TABLE user_mappings (
    id           TEXT PRIMARY KEY,
    phone_number TEXT NOT NULL UNIQUE,
    tenant_id    TEXT,
    is_active    INTEGER NOT NULL DEFAULT 1
)
"""


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.execute(text(CREATE_USER_MAPPINGS_SQL))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str):
        return self.store.get(key)

    async def set(self, key: str, value: Any, ex: int | None = None) -> bool:
        self.store[key] = str(value)
        return True

    async def delete(self, *keys: str) -> int:
        n = 0
        for k in keys:
            if k in self.store:
                del self.store[k]
                n += 1
        return n


async def _seed(factory, phone: str, tenant: str, row_id: str = "r1"):
    async with factory() as session:
        await session.execute(
            text(
                "INSERT INTO user_mappings (id, phone_number, tenant_id, is_active) "
                "VALUES (:id, :phone, :tenant, 1)"
            ),
            {"id": row_id, "phone": phone, "tenant": tenant},
        )
        await session.commit()


async def _count(factory, phone: str) -> int:
    async with factory() as session:
        r = await session.execute(
            text("SELECT COUNT(*) FROM user_mappings WHERE phone_number = :p"),
            {"p": phone},
        )
        return int(r.scalar() or 0)


@pytest.mark.asyncio
async def test_purge_deletes_row_and_drops_cache(session_factory):
    redis = _FakeRedis()
    resolver = TenantResolver(session_factory=session_factory, redis_client=redis)
    await _seed(session_factory, "+385955087196", "tenant-A")
    # Prime the cache so we can prove invalidate fires
    redis.store["tenant_phone:+385955087196"] = "tenant-A"

    deleted = await resolver.purge_user_mapping("+385955087196")
    assert deleted == 1
    assert await _count(session_factory, "+385955087196") == 0
    assert "tenant_phone:+385955087196" not in redis.store


@pytest.mark.asyncio
async def test_purge_normalizes_phone(session_factory):
    """National-format phone with leading 0 must map to the same E.164 row."""
    redis = _FakeRedis()
    resolver = TenantResolver(session_factory=session_factory, redis_client=redis)
    await _seed(session_factory, "+385955087196", "tenant-A")

    deleted = await resolver.purge_user_mapping("0955087196")  # national format
    assert deleted == 1
    assert await _count(session_factory, "+385955087196") == 0


@pytest.mark.asyncio
async def test_purge_unknown_phone_returns_zero(session_factory):
    resolver = TenantResolver(
        session_factory=session_factory, redis_client=_FakeRedis(),
    )
    assert await resolver.purge_user_mapping("+15555550000") == 0


@pytest.mark.asyncio
async def test_purge_empty_or_garbage_phone_returns_zero(session_factory):
    resolver = TenantResolver(
        session_factory=session_factory, redis_client=_FakeRedis(),
    )
    assert await resolver.purge_user_mapping("") == 0
    assert await resolver.purge_user_mapping("---") == 0


@pytest.mark.asyncio
async def test_purge_db_outage_raises(session_factory):
    """If the DB is unreachable, purge must surface the error — operator
    should NOT believe GDPR deletion succeeded when the row is still there."""
    class _BoomFactory:
        def __call__(self):
            class _Ctx:
                async def __aenter__(self_inner):
                    raise RuntimeError("simulated DB outage")
                async def __aexit__(self_inner, *a):
                    return False
            return _Ctx()

    resolver = TenantResolver(
        session_factory=_BoomFactory(), redis_client=_FakeRedis(),
    )
    with pytest.raises(RuntimeError, match="simulated DB outage"):
        await resolver.purge_user_mapping("+385955087196")


@pytest.mark.asyncio
async def test_subsequent_resolve_returns_none_after_purge(session_factory):
    """End-to-end: after purge, the user is treated as unknown by resolve()."""
    redis = _FakeRedis()
    resolver = TenantResolver(session_factory=session_factory, redis_client=redis)
    await _seed(session_factory, "+385955087196", "tenant-A")

    # Verify resolves OK before purge
    assert await resolver.resolve_tenant_for_phone("+385955087196") == "tenant-A"
    # Purge
    await resolver.purge_user_mapping("+385955087196")
    # Now resolves to None (cache was also dropped)
    assert await resolver.resolve_tenant_for_phone("+385955087196") is None
