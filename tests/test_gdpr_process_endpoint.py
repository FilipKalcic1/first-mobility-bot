"""Tests for the POST /admin/gdpr-process endpoint + TenantResolver.purge_user_mapping.

Covers the bot-side cleanup half of the GDPR right-to-erasure flow:
operator hits this endpoint after seeing a request via /admin/gdpr-requests,
and the bot wipes Redis state + Postgres user_mappings row. The MobilityOne
Person + cascade deletion stays manual per the readiness plan (Option B).
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
except Exception:
    pytest.skip("fastapi.testclient not available", allow_module_level=True)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

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


class FakeRedis:
    """In-memory Redis stub covering the keys the endpoint touches."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str):
        return self.store.get(key)

    async def set(self, key: str, value: Any, ex: int | None = None) -> bool:
        self.store[key] = str(value)
        return True

    async def setex(self, key: str, ttl: int, value: Any) -> bool:
        self.store[key] = str(value)
        return True

    async def delete(self, *keys: str) -> int:
        n = 0
        for k in keys:
            if k in self.store:
                del self.store[k]
                n += 1
        return n


@pytest.fixture
def admin_token_env(monkeypatch):
    for k in list(os.environ):
        if k.startswith("ADMIN_TOKEN"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ADMIN_TOKEN_1", "secret_filip")
    monkeypatch.setenv("ADMIN_TOKEN_1_USER", "filip.kalcic")
    import services.admin_auth as mod
    importlib.reload(mod)
    yield mod


@pytest.fixture
def client(admin_token_env, session_factory):
    fake_redis = FakeRedis()
    # Pre-populate Redis with state for the target phone, simulating an
    # active user. The endpoint should wipe these.
    target_phone = "+385955087196"
    for key_tmpl in (
        "v2:identity:{}",
        "v2:pending_mut:{}",
        "v2:pending_mut:exec:{}",
        "v2_pending_clarify:{}",
        "v2_conv_history:{}",
        "v2:rl:m:{}",
        "v2:rl:h:{}",
        "tenant_phone:{}",
    ):
        fake_redis.store[key_tmpl.format(target_phone)] = "stub_value"

    # Wire up an in-memory tenant resolver pointed at the test SQLite DB.
    from services.tenant_resolver import TenantResolver
    resolver = TenantResolver(
        session_factory=session_factory,
        redis_client=fake_redis,
    )

    # Reset the module-level singleton so our test instance is used.
    import services.tenant_resolver as tr_mod
    tr_mod._singleton = resolver

    mock_settings = MagicMock()
    mock_settings.VERIFY_WHATSAPP_SIGNATURE = False
    mock_settings.REDIS_URL = "redis://localhost:6379/0"
    mock_settings.WHATSAPP_VERIFY_TOKEN = None

    with patch("webhook_simple.settings", mock_settings):
        with patch("webhook_simple.get_redis", AsyncMock(return_value=fake_redis)):
            from webhook_simple import router
            app = FastAPI()
            app.include_router(router)
            yield TestClient(app), fake_redis, session_factory, target_phone

    # Cleanup module singleton
    tr_mod._singleton = None


async def _seed_user_mapping(factory, phone: str, tenant: str, row_id: str = "r1"):
    async with factory() as session:
        await session.execute(
            text(
                "INSERT INTO user_mappings (id, phone_number, tenant_id, is_active) "
                "VALUES (:id, :phone, :tenant, 1)"
            ),
            {"id": row_id, "phone": phone, "tenant": tenant},
        )
        await session.commit()


async def _count_mappings(factory, phone: str) -> int:
    async with factory() as session:
        result = await session.execute(
            text("SELECT COUNT(*) FROM user_mappings WHERE phone_number = :phone"),
            {"phone": phone},
        )
        return int((result.scalar() or 0))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_no_token_returns_404(client):
    api, *_ = client
    resp = api.post("/admin/gdpr-process?tenant=t1&phone=+385955087196")
    assert resp.status_code == 404


def test_wrong_token_returns_404(client):
    api, *_ = client
    resp = api.post(
        "/admin/gdpr-process?token=WRONG&tenant=t1&phone=+385955087196"
    )
    assert resp.status_code == 404


def test_missing_tenant_param_returns_400(client):
    api, *_ = client
    resp = api.post(
        "/admin/gdpr-process?token=secret_filip&phone=+385955087196"
    )
    assert resp.status_code == 400


def test_missing_phone_param_returns_400(client):
    api, *_ = client
    resp = api.post("/admin/gdpr-process?token=secret_filip&tenant=t1")
    assert resp.status_code == 400


def test_unmappable_phone_returns_400(client):
    api, *_ = client
    resp = api.post(
        "/admin/gdpr-process?token=secret_filip&tenant=t1&phone=garbage"
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_dry_run_deletes_nothing_but_previews_keys(client):
    api, fake_redis, factory, phone = client
    await _seed_user_mapping(factory, "+385955087196", "tenant-A")

    resp = api.post(
        "/admin/gdpr-process"
        "?token=secret_filip&tenant=tenant-A&phone=+385955087196&dry_run=true"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["dry_run"] is True
    assert body["binding"] == "mapped_to_tenant"
    assert body["would_delete_user_mappings"] is True
    assert len(body["would_delete_redis_keys"]) == 8  # all 8 templates
    # DB row still present
    assert await _count_mappings(factory, "+385955087196") == 1
    # Redis keys still present
    assert "v2:identity:+385955087196" in fake_redis.store


@pytest.mark.asyncio
async def test_real_run_wipes_redis_and_postgres(client):
    api, fake_redis, factory, phone = client
    await _seed_user_mapping(factory, "+385955087196", "tenant-A")
    assert await _count_mappings(factory, "+385955087196") == 1

    resp = api.post(
        "/admin/gdpr-process"
        "?token=secret_filip&tenant=tenant-A&phone=+385955087196"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["dry_run"] is False
    assert body["user_mappings_rows_deleted"] == 1
    assert body["redis_keys_deleted_count"] >= 7  # at least the 7 stub keys
    assert body["user_mappings_error"] is None
    assert "MobilityOne" in body["reminder"]

    # Postgres row gone
    assert await _count_mappings(factory, "+385955087196") == 0
    # All seeded Redis keys gone
    for key_tmpl in (
        "v2:identity:{}", "v2:pending_mut:{}", "v2_pending_clarify:{}",
        "v2_conv_history:{}", "v2:rl:m:{}", "v2:rl:h:{}", "tenant_phone:{}",
    ):
        assert key_tmpl.format("+385955087196") not in fake_redis.store


@pytest.mark.asyncio
async def test_tenant_mismatch_refuses_with_409(client):
    api, fake_redis, factory, phone = client
    # User is in tenant-A, but operator from tenant-B tries to delete them.
    await _seed_user_mapping(factory, "+385955087196", "tenant-A")

    resp = api.post(
        "/admin/gdpr-process"
        "?token=secret_filip&tenant=tenant-B&phone=+385955087196"
    )
    assert resp.status_code == 409
    # Nothing should be deleted
    assert await _count_mappings(factory, "+385955087196") == 1
    assert "v2:identity:+385955087196" in fake_redis.store


@pytest.mark.asyncio
async def test_already_unmapped_phone_still_wipes_redis(client):
    """Operator may re-run cleanup after a failure. Stale Redis keys must
    still be removable even when user_mappings row is already gone."""
    api, fake_redis, factory, phone = client
    # No seed in user_mappings — phone is "unmapped_or_already_deleted"

    resp = api.post(
        "/admin/gdpr-process"
        "?token=secret_filip&tenant=tenant-A&phone=+385955087196"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["binding_before"] == "unmapped_or_already_deleted"
    assert body["user_mappings_rows_deleted"] == 0  # nothing to delete
    assert body["redis_keys_deleted_count"] >= 7  # stale keys cleaned


# Direct unit tests for TenantResolver.purge_user_mapping live in
# tests/test_tenant_resolver_purge.py (no FastAPI dependency).
