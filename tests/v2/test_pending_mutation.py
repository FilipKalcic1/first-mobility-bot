"""Tests for pending_mutation store + reply parser."""
from __future__ import annotations

import pytest

from services.v2.pending_mutation import (
    PendingMutation, PendingMutationStore,
    STAGE_DOUBLE_FIRST, STAGE_DOUBLE_SECOND, STAGE_SINGLE,
    parse_reply,
)


# ---- FakeRedis (matches contract used by store) -----------------------


class FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}
        self.fail = False

    async def get(self, key):
        if self.fail:
            raise ConnectionError("down")
        return self.store.get(key)

    async def setex(self, key, ttl, value):
        if self.fail:
            raise ConnectionError("down")
        self.store[key] = value

    async def delete(self, key):
        if self.fail:
            raise ConnectionError("down")
        self.store.pop(key, None)


# ---- Store roundtrip ---------------------------------------------------


@pytest.mark.asyncio
async def test_save_and_load_roundtrip():
    s = PendingMutationStore(FakeRedis())
    await s.save("385955087196", "delete_X", {"id": "x"}, STAGE_SINGLE)
    p = await s.load("385955087196")
    assert p is not None
    assert p.tool_id == "delete_X"
    assert p.params == {"id": "x"}
    assert p.stage == STAGE_SINGLE


@pytest.mark.asyncio
async def test_load_missing_returns_none():
    s = PendingMutationStore(FakeRedis())
    assert await s.load("385955087196") is None


@pytest.mark.asyncio
async def test_clear_removes_state():
    redis = FakeRedis()
    s = PendingMutationStore(redis)
    await s.save("385955087196", "x", {}, STAGE_SINGLE)
    assert await s.load("385955087196") is not None
    await s.clear("385955087196")
    assert await s.load("385955087196") is None


@pytest.mark.asyncio
async def test_corrupt_payload_returns_none():
    redis = FakeRedis()
    redis.store["v2:pending_mut:385955087196"] = "{ not json"
    s = PendingMutationStore(redis)
    assert await s.load("385955087196") is None


@pytest.mark.asyncio
async def test_redis_failure_load_returns_none_no_crash():
    redis = FakeRedis()
    redis.fail = True
    s = PendingMutationStore(redis)
    # All ops fault-tolerant
    assert await s.load("x") is None
    await s.save("x", "t", {}, STAGE_SINGLE)  # no raise
    await s.clear("x")  # no raise


def test_pending_mutation_json_is_self_describing():
    p = PendingMutation(
        tool_id="post_AddMileage",
        params={"Value": 100},
        stage=STAGE_SINGLE,
        created_at=1714000000.0,
    )
    rt = PendingMutation.from_json(p.to_json())
    assert rt.tool_id == p.tool_id
    assert rt.params == p.params
    assert rt.stage == p.stage


# ---- Reply parsing -----------------------------------------------------


def test_parse_da_in_single_stage_executes():
    assert parse_reply("Da", STAGE_SINGLE) == "execute"
    assert parse_reply("DA", STAGE_SINGLE) == "execute"
    assert parse_reply("ok", STAGE_SINGLE) == "execute"
    assert parse_reply("može", STAGE_SINGLE) == "execute"


def test_parse_ne_cancels():
    assert parse_reply("ne", STAGE_SINGLE) == "cancel"
    assert parse_reply("Ne", STAGE_SINGLE) == "cancel"
    assert parse_reply("odustani", STAGE_SINGLE) == "cancel"
    assert parse_reply("prekini", STAGE_DOUBLE_FIRST) == "cancel"


def test_parse_da_in_double_first_advances():
    assert parse_reply("Da", STAGE_DOUBLE_FIRST) == "advance"
    assert parse_reply("ok", STAGE_DOUBLE_FIRST) == "advance"


def test_parse_trajno_in_double_second_executes():
    assert parse_reply("TRAJNO", STAGE_DOUBLE_SECOND) == "execute"
    assert parse_reply("trajno", STAGE_DOUBLE_SECOND) == "execute"
    assert parse_reply("Da, trajno", STAGE_DOUBLE_SECOND) == "execute"


def test_parse_da_alone_in_double_second_is_ambiguous():
    # User must type the literal token TRAJNO at stage 2 — bare "Da" not enough
    assert parse_reply("Da", STAGE_DOUBLE_SECOND) == "ambiguous"


def test_parse_empty_is_ambiguous():
    assert parse_reply("", STAGE_SINGLE) == "ambiguous"
    assert parse_reply("   ", STAGE_SINGLE) == "ambiguous"


def test_parse_random_text_is_ambiguous():
    assert parse_reply("što ovo znači?", STAGE_SINGLE) == "ambiguous"
    assert parse_reply("kolika mi je km", STAGE_SINGLE) == "ambiguous"


def test_parse_negative_beats_affirmative_in_mixed_text():
    """If user writes 'Da, ali ne' the cancel signal wins to be safe."""
    assert parse_reply("ne, odustani", STAGE_SINGLE) == "cancel"
