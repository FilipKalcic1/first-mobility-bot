"""Tests for ConversationHistoryStore (Redis-backed multi-turn context)."""
from __future__ import annotations

import pytest

from services.v2.conversation_history import (
    ConversationHistoryStore, ConversationTurn, REDIS_KEY_PREFIX,
)


class _FakeRedis:
    """In-memory async stub for redis.asyncio with list ops we use."""

    def __init__(self):
        self.lists: dict[str, list[str]] = {}
        self.ttls: dict[str, int] = {}

    async def rpush(self, key, value):
        self.lists.setdefault(key, []).append(value)

    async def ltrim(self, key, start, end):
        if key not in self.lists:
            return
        lst = self.lists[key]
        # Mirror redis semantics: keep elements from start to end inclusive
        if end == -1:
            self.lists[key] = lst[start:]
        else:
            self.lists[key] = lst[start:end + 1]

    async def expire(self, key, ttl):
        self.ttls[key] = ttl

    async def lrange(self, key, start, stop):
        lst = self.lists.get(key, [])
        if stop == -1:
            return lst[start:]
        return lst[start:stop + 1]

    async def delete(self, key):
        self.lists.pop(key, None)
        self.ttls.pop(key, None)


@pytest.mark.asyncio
async def test_append_and_load_returns_turns_in_order():
    store = ConversationHistoryStore(_FakeRedis())
    phone = "385912345678"
    await store.append(phone, ConversationTurn(user="kolika km", bot="34521 km"))
    await store.append(phone, ConversationTurn(user="prošli mjesec", bot="..."))

    turns = await store.load(phone)
    assert len(turns) == 2
    assert turns[0]["user"] == "kolika km"
    assert turns[1]["user"] == "prošli mjesec"


@pytest.mark.asyncio
async def test_load_empty_returns_empty_list():
    store = ConversationHistoryStore(_FakeRedis())
    turns = await store.load("385999999999")
    assert turns == []


@pytest.mark.asyncio
async def test_max_turns_trim():
    store = ConversationHistoryStore(_FakeRedis(), max_turns=2)
    phone = "385912345678"
    for i in range(5):
        await store.append(phone, ConversationTurn(user=f"q{i}"))
    turns = await store.load(phone)
    # Most recent 2 should remain
    assert len(turns) == 2
    assert turns[0]["user"] == "q3"
    assert turns[1]["user"] == "q4"


@pytest.mark.asyncio
async def test_clear_wipes_state():
    store = ConversationHistoryStore(_FakeRedis())
    phone = "385912345678"
    await store.append(phone, ConversationTurn(user="hello"))
    await store.clear(phone)
    assert await store.load(phone) == []


@pytest.mark.asyncio
async def test_redis_none_is_safe_noop():
    store = ConversationHistoryStore(redis_client=None)
    await store.append("385912345678", ConversationTurn(user="x"))
    assert await store.load("385912345678") == []


@pytest.mark.asyncio
async def test_redis_failure_doesnt_raise():
    class _Broken:
        async def rpush(self, key, value):
            raise ConnectionError("boom")

        async def lrange(self, key, start, stop):
            raise ConnectionError("boom")

        async def expire(self, key, ttl):
            return None

        async def ltrim(self, key, s, e):
            return None

        async def delete(self, key):
            return None

    store = ConversationHistoryStore(_Broken())
    # Must not raise
    await store.append("385912345678", ConversationTurn(user="x"))
    out = await store.load("385912345678")
    assert out == []


def test_key_prefix_constant_is_namespaced():
    assert REDIS_KEY_PREFIX.startswith("v2_")


def test_turn_serialization_roundtrip():
    t = ConversationTurn(user="kolika km", bot="34521", bot_action="get_MasterData")
    d = t.to_dict()
    assert d["user"] == "kolika km"
    assert d["bot"] == "34521"
    assert d["bot_action"] == "get_MasterData"


@pytest.mark.asyncio
async def test_ttl_is_set_on_append():
    redis = _FakeRedis()
    store = ConversationHistoryStore(redis, ttl_seconds=900)
    await store.append("385912345678", ConversationTurn(user="x"))
    assert any(ttl == 900 for ttl in redis.ttls.values())
