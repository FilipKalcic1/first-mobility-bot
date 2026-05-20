"""Tests for pending_mutation store + reply parser."""
from __future__ import annotations

import pytest

from services.v2.pending_mutation import (
    PendingMutation, PendingMutationStore,
    STAGE_SINGLE,
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
    assert parse_reply("prekini", STAGE_SINGLE) == "cancel"


def test_parse_unknown_stage_returns_ambiguous():
    """Single-confirm policy: only STAGE_SINGLE is recognised. Corrupt or
    legacy stage strings get treated as ambiguous (re-prompt, not execute)."""
    assert parse_reply("Da", "double_first") == "ambiguous"
    assert parse_reply("TRAJNO", "double_second") == "ambiguous"


def test_parse_empty_is_ambiguous():
    assert parse_reply("", STAGE_SINGLE) == "ambiguous"
    assert parse_reply("   ", STAGE_SINGLE) == "ambiguous"


def test_parse_random_text_is_ambiguous():
    assert parse_reply("što ovo znači?", STAGE_SINGLE) == "ambiguous"
    assert parse_reply("kolika mi je km", STAGE_SINGLE) == "ambiguous"


def test_parse_negative_beats_affirmative_in_mixed_text():
    """If user writes 'Da, ali ne' the cancel signal wins to be safe."""
    assert parse_reply("ne, odustani", STAGE_SINGLE) == "cancel"


# ---- Faza 1 (Filip 2026-05-17): strict execute whitelist --------------


def test_parse_moze_biti_is_ambiguous_not_execute():
    """Bug #4: 'može biti' used to parse as execute because 'može' was
    matched by word-split. STRICT whitelist now requires exact match."""
    assert parse_reply("može biti", STAGE_SINGLE) == "ambiguous"
    assert parse_reply("može možda", STAGE_SINGLE) == "ambiguous"
    assert parse_reply("možda", STAGE_SINGLE) == "ambiguous"


def test_parse_da_with_other_words_does_not_execute():
    """'da možda', 'da ali...' must NOT execute (strict whitelist).
    Note: 'da, ne znam' falls under cancel because cancel set is lenient
    (word-split fallback) — safer to over-cancel than over-execute."""
    assert parse_reply("da možda", STAGE_SINGLE) == "ambiguous"
    assert parse_reply("da ali samo ako", STAGE_SINGLE) == "ambiguous"
    assert parse_reply("da, ne znam", STAGE_SINGLE) == "cancel"  # 'ne' wins


def test_parse_strips_trailing_punctuation():
    """'Da.' / 'Da!' / 'Da?' should still execute (common phone keyboards)."""
    assert parse_reply("Da.", STAGE_SINGLE) == "execute"
    assert parse_reply("DA!", STAGE_SINGLE) == "execute"
    assert parse_reply("ok!", STAGE_SINGLE) == "execute"
    assert parse_reply("može.", STAGE_SINGLE) == "execute"


def test_parse_standalone_moze_still_executes():
    """Standalone 'može' (no trailing words) IS a confirm — keep working."""
    assert parse_reply("može", STAGE_SINGLE) == "execute"
    assert parse_reply("Može", STAGE_SINGLE) == "execute"
    assert parse_reply("moze", STAGE_SINGLE) == "execute"


def test_parse_ne_hvala_still_cancels():
    """Cancel set stays lenient — 'ne hvala' / 'ne, ne radi to' should
    still cancel (false negatives only mean re-ask, safer)."""
    assert parse_reply("ne hvala", STAGE_SINGLE) == "cancel"
    assert parse_reply("Ne, otkaži", STAGE_SINGLE) == "cancel"
