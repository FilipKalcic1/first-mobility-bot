"""Tests for PendingClarifyStore (Redis-backed clarify state)."""
from __future__ import annotations

import pytest

from services.v2.pending_clarify import (
    PendingClarify,
    PendingClarifyStore,
    REDIS_KEY_PREFIX,
    STAGE_ACTION,
    STAGE_ACTION_GLOBAL,
    STAGE_TOOL,
)
from services.v2 import clarify_ui


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


# --------------------------------------------------------------------------
# 2-step clarify (Filip direktiva 2026-05-17): stage field + action picker
# --------------------------------------------------------------------------

def test_default_stage_is_tool_for_backward_compat():
    pc = PendingClarify(phone="p", candidates=[])
    assert pc.stage == STAGE_TOOL


def test_legacy_json_without_stage_field_loads_as_tool():
    """Pre-2026-05-17 entries lack `stage` — must default to STAGE_TOOL."""
    import json
    legacy = json.dumps({
        "phone": "p",
        "candidates": [{"tool_id": "get_X"}],
        "original_query": "x",
    })
    pc = PendingClarify.from_json(legacy)
    assert pc.stage == STAGE_TOOL


@pytest.mark.asyncio
async def test_save_with_stage_action_persists_stage():
    redis = _FakeRedis()
    store = PendingClarifyStore(redis)
    await store.save(
        "p",
        candidates=[
            {"tool_id": "get_X", "method": "GET"},
            {"tool_id": "post_Y", "method": "POST"},
        ],
        stage=STAGE_ACTION,
    )
    loaded = await store.load("p")
    assert loaded is not None
    assert loaded.stage == STAGE_ACTION
    assert len(loaded.candidates) == 2


def test_action_picker_groups_by_http_method():
    """Step 1: mixed-method candidates produce one card per action label.
    PUT+PATCH merge into single IZMIJENITI card."""
    candidates = [
        {"tool_id": "get_X",        "method": "GET"},
        {"tool_id": "get_Y",        "method": "GET"},      # dup label, dedupe
        {"tool_id": "post_Z",       "method": "POST"},
        {"tool_id": "put_A",        "method": "PUT"},
        {"tool_id": "patch_B",      "method": "PATCH"},    # shares IZMIJENITI
        {"tool_id": "delete_C",     "method": "DELETE"},
    ]
    options = clarify_ui.build_action_picker(candidates)
    labels = [c.short_label for c in options.cards]
    # Canonical order: POGLEDATI, UNIJETI, IZMIJENITI, IZBRISATI
    assert labels == ["POGLEDATI", "UNIJETI / KREIRATI", "IZMIJENITI", "IZBRISATI"]


def test_action_picker_single_method_yields_single_card():
    """All candidates GET → only 1 action card (POGLEDATI). Engine should
    detect this and skip Step 1 entirely, going straight to Step 2."""
    candidates = [
        {"tool_id": "get_A", "method": "GET"},
        {"tool_id": "get_B", "method": "GET"},
        {"tool_id": "get_C", "method": "GET"},
    ]
    options = clarify_ui.build_action_picker(candidates)
    assert len(options.cards) == 1
    assert options.cards[0].short_label == "POGLEDATI"


def test_action_picker_handles_empty_or_unknown_method():
    """Defensive: candidates with empty/unrecognized method are skipped."""
    candidates = [
        {"tool_id": "get_OK", "method": "GET"},
        {"tool_id": "weird",  "method": ""},
        {"tool_id": "alien",  "method": "OPTIONS"},
    ]
    options = clarify_ui.build_action_picker(candidates)
    assert len(options.cards) == 1
    assert options.cards[0].short_label == "POGLEDATI"


def test_methods_for_action_label_reverse_lookup():
    """Step 2 filter: given a Step 1 selection label, return the methods to
    filter candidates by. IZMIJENITI must yield both PUT and PATCH."""
    assert clarify_ui.methods_for_action_label("POGLEDATI") == {"GET"}
    assert clarify_ui.methods_for_action_label("UNIJETI / KREIRATI") == {"POST"}
    assert clarify_ui.methods_for_action_label("IZMIJENITI") == {"PUT", "PATCH"}
    assert clarify_ui.methods_for_action_label("IZBRISATI") == {"DELETE"}
    assert clarify_ui.methods_for_action_label("nepostojeci") == set()


# --------------------------------------------------------------------------
# Model A (Filip 2026-05-17): STAGE_ACTION_GLOBAL = universal action picker
# saved on every Turn-1 query. Candidates list is EMPTY at save time —
# Turn 2 fills it after the user picks an action and the scoped router runs.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_with_stage_action_global_persists_empty_candidates_and_query():
    redis = _FakeRedis()
    store = PendingClarifyStore(redis)
    await store.save(
        "p",
        candidates=[],  # Model A: filled on Turn 2
        original_query="nešto s kalendarom",
        stage=STAGE_ACTION_GLOBAL,
    )
    loaded = await store.load("p")
    assert loaded is not None
    assert loaded.stage == STAGE_ACTION_GLOBAL
    assert loaded.candidates == []
    assert loaded.original_query == "nešto s kalendarom"


def test_action_picker_global_returns_four_canonical_actions():
    """Model A universal picker — always 4 options regardless of router
    state. Order matters: POGLEDATI / UNIJETI / IZMIJENITI / IZBRISATI."""
    options = clarify_ui.build_action_picker_global()
    labels = [c.short_label for c in options.cards]
    assert labels == [
        "POGLEDATI", "UNIJETI / KREIRATI", "IZMIJENITI", "IZBRISATI",
    ]


# ---------- REOFFER (Filip 2026-06-05) ----------

def test_pending_clarify_reoffer_fields_default_to_empty():
    """Backward compat: legacy entries (pre-2026-06-05) must still load
    even though they lack the new reoffer fields."""
    legacy_json = (
        '{"phone": "385912345678", "candidates": [], '
        '"original_query": "x", "stage": "tool"}'
    )
    pc = PendingClarify.from_json(legacy_json)
    assert pc.all_candidate_ids == []
    assert pc.shown_tool_ids == []
    assert pc.last_executed_tool is None
    assert pc.can_reoffer is False


def test_pending_clarify_with_reoffer_roundtrip():
    pc = PendingClarify(
        phone="385912345678",
        candidates=[{"tool_id": "x"}],
        original_query="moje rezervacije",
        stage=STAGE_TOOL,
        all_candidate_ids=["t1", "t2", "t3", "t4", "t5"],
        shown_tool_ids=["t1", "t2", "t3"],
        last_executed_tool="t1",
        can_reoffer=True,
    )
    pc2 = PendingClarify.from_json(pc.to_json())
    assert pc2.all_candidate_ids == ["t1", "t2", "t3", "t4", "t5"]
    assert pc2.shown_tool_ids == ["t1", "t2", "t3"]
    assert pc2.last_executed_tool == "t1"
    assert pc2.can_reoffer is True


@pytest.mark.asyncio
async def test_store_save_persists_reoffer_fields():
    redis = _FakeRedis()
    store = PendingClarifyStore(redis)
    await store.save(
        "p", candidates=[], original_query="q", stage=STAGE_TOOL,
        all_candidate_ids=["a", "b", "c"],
        shown_tool_ids=["a"],
        last_executed_tool="a",
        can_reoffer=True,
    )
    loaded = await store.load("p")
    assert loaded is not None
    assert loaded.all_candidate_ids == ["a", "b", "c"]
    assert loaded.shown_tool_ids == ["a"]
    assert loaded.last_executed_tool == "a"
    assert loaded.can_reoffer is True


@pytest.mark.asyncio
async def test_store_save_defaults_reoffer_to_disabled():
    """Existing callers that don't pass reoffer args still work — fields
    default to empty/False so 'nije točno' handler stays silent."""
    redis = _FakeRedis()
    store = PendingClarifyStore(redis)
    await store.save("p", candidates=[{"tool_id": "x"}], original_query="q")
    loaded = await store.load("p")
    assert loaded is not None
    assert loaded.all_candidate_ids == []
    assert loaded.shown_tool_ids == []
    assert loaded.last_executed_tool is None
    assert loaded.can_reoffer is False


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
