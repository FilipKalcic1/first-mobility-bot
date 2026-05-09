"""Tests for PendingClarifyStore (Redis-backed clarify state)."""
from __future__ import annotations

import pytest

from services.v2.pending_clarify import (
    PendingClarify,
    PendingClarifyStore,
    REDIS_KEY_PREFIX,
)


class _FakeRedis:
    """In-memory async stub mimicking the redis.asyncio interface we use."""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    async def setex(self, key, ttl, value):
        self.store[key] = value
        self.ttls[key] = ttl

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, key):
        self.store.pop(key, None)
        self.ttls.pop(key, None)


def test_pending_clarify_serializes_and_deserializes():
    pc = PendingClarify(
        phone="385912345678",
        candidates=[
            {"tool_id": "get_VehicleCalendar", "label": "Pokaži rezervacije"},
            {"tool_id": "delete_VehicleCalendar_id", "label": "Otkaži rezervaciju"},
        ],
        original_query="moje rezervacije",
    )
    s = pc.to_json()
    pc2 = PendingClarify.from_json(s)
    assert pc2.phone == pc.phone
    assert pc2.candidates == pc.candidates
    assert pc2.original_query == pc.original_query


@pytest.mark.asyncio
async def test_save_and_load_roundtrip():
    redis = _FakeRedis()
    store = PendingClarifyStore(redis)
    cands = [{"tool_id": "get_X", "label": "X"}]
    await store.save("385912345678", cands, original_query="test")
    loaded = await store.load("385912345678")
    assert loaded is not None
    assert loaded.phone == "385912345678"
    assert loaded.candidates == cands
    assert loaded.original_query == "test"


@pytest.mark.asyncio
async def test_load_returns_none_for_unknown_phone():
    store = PendingClarifyStore(_FakeRedis())
    assert await store.load("385999999999") is None


@pytest.mark.asyncio
async def test_clear_removes_state():
    redis = _FakeRedis()
    store = PendingClarifyStore(redis)
    await store.save("p", [{"tool_id": "x"}])
    assert await store.load("p") is not None
    await store.clear("p")
    assert await store.load("p") is None


@pytest.mark.asyncio
async def test_save_uses_setex_ttl():
    redis = _FakeRedis()
    store = PendingClarifyStore(redis, ttl_seconds=120)
    await store.save("p", [{"tool_id": "x"}])
    assert redis.ttls[REDIS_KEY_PREFIX + "p"] == 120


@pytest.mark.asyncio
async def test_save_handles_redis_failure_gracefully():
    class _BrokenRedis:
        async def setex(self, *a, **kw):
            raise RuntimeError("redis down")

    store = PendingClarifyStore(_BrokenRedis())
    # Must not propagate — telemetry-style failure swallowing
    await store.save("p", [{"tool_id": "x"}])


@pytest.mark.asyncio
async def test_load_handles_redis_failure_gracefully():
    class _BrokenRedis:
        async def get(self, key):
            raise RuntimeError("redis down")

    store = PendingClarifyStore(_BrokenRedis())
    assert await store.load("p") is None


@pytest.mark.asyncio
async def test_works_with_none_redis_client():
    """When redis_client is None (e.g., dev without Redis), store is a no-op."""
    store = PendingClarifyStore(None)
    await store.save("p", [{"tool_id": "x"}])
    assert await store.load("p") is None
    await store.clear("p")  # must not raise


@pytest.mark.asyncio
async def test_byte_response_decoded():
    """Some redis clients return bytes; store handles both."""
    class _BytesRedis:
        async def setex(self, *a, **kw):
            self.value = a[2] if len(a) > 2 else None

        async def get(self, key):
            v = self.value if hasattr(self, "value") else None
            return v.encode("utf-8") if v else None

    redis = _BytesRedis()
    store = PendingClarifyStore(redis)
    await store.save("p", [{"tool_id": "x"}])
    loaded = await store.load("p")
    assert loaded is not None
    assert loaded.candidates == [{"tool_id": "x"}]
