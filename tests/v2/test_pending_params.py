"""Tests for PendingParamsStore (Redis-backed param-collection state)."""
from __future__ import annotations

import pytest

from services.v2.pending_params import (
    PendingParams,
    PendingParamsStore,
    REDIS_KEY_PREFIX,
)


class _FakeRedis:
    """In-memory async stub mimicking the redis.asyncio interface."""

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


def test_serializes_and_deserializes_roundtrip():
    pp = PendingParams(
        phone="385912345678",
        tool_id="delete_VehicleCalendar_id",
        collected={"x": "y"},
        required_remaining=["id"],
        optional_remaining=["Note"],
        optional_offered=False,
        original_query="obriši rezervaciju",
    )
    pp2 = PendingParams.from_json(pp.to_json())
    assert pp2.phone == pp.phone
    assert pp2.tool_id == pp.tool_id
    assert pp2.collected == pp.collected
    assert pp2.required_remaining == pp.required_remaining
    assert pp2.optional_remaining == pp.optional_remaining
    assert pp2.optional_offered is False
    assert pp2.original_query == pp.original_query


@pytest.mark.asyncio
async def test_save_and_load():
    redis = _FakeRedis()
    store = PendingParamsStore(redis)
    state = PendingParams(
        phone="385912345678",
        tool_id="post_AddMileage",
        required_remaining=["Mileage"],
    )
    await store.save("385912345678", state)
    loaded = await store.load("385912345678")
    assert loaded is not None
    assert loaded.tool_id == "post_AddMileage"
    assert loaded.required_remaining == ["Mileage"]


@pytest.mark.asyncio
async def test_load_returns_none_for_unknown_phone():
    store = PendingParamsStore(_FakeRedis())
    assert await store.load("385999999999") is None


@pytest.mark.asyncio
async def test_clear_removes_state():
    redis = _FakeRedis()
    store = PendingParamsStore(redis)
    state = PendingParams(phone="p", tool_id="x")
    await store.save("p", state)
    assert await store.load("p") is not None
    await store.clear("p")
    assert await store.load("p") is None


@pytest.mark.asyncio
async def test_save_uses_setex_ttl():
    redis = _FakeRedis()
    store = PendingParamsStore(redis, ttl_seconds=120)
    state = PendingParams(phone="p", tool_id="x")
    await store.save("p", state)
    assert redis.ttls[REDIS_KEY_PREFIX + "p"] == 120


@pytest.mark.asyncio
async def test_save_handles_redis_failure_gracefully():
    class _BrokenRedis:
        async def setex(self, *a, **kw):
            raise RuntimeError("redis down")

    store = PendingParamsStore(_BrokenRedis())
    # Must not propagate — telemetry-style failure swallowing
    await store.save("p", PendingParams(phone="p", tool_id="x"))


@pytest.mark.asyncio
async def test_load_handles_redis_failure_gracefully():
    class _BrokenRedis:
        async def get(self, key):
            raise RuntimeError("redis down")

    store = PendingParamsStore(_BrokenRedis())
    assert await store.load("p") is None


@pytest.mark.asyncio
async def test_works_with_none_redis_client():
    """None redis_client (dev without Redis) → no-op save/load/clear."""
    store = PendingParamsStore(None)
    await store.save("p", PendingParams(phone="p", tool_id="x"))
    assert await store.load("p") is None
    await store.clear("p")  # must not raise


@pytest.mark.asyncio
async def test_bytes_response_decoded():
    """Some redis clients return bytes; store handles both."""
    class _BytesRedis:
        async def setex(self, *a, **kw):
            self.value = a[2] if len(a) > 2 else None

        async def get(self, key):
            v = self.value if hasattr(self, "value") else None
            return v.encode("utf-8") if v else None

    redis = _BytesRedis()
    store = PendingParamsStore(redis)
    await store.save("p", PendingParams(phone="p", tool_id="t"))
    loaded = await store.load("p")
    assert loaded is not None
    assert loaded.tool_id == "t"
