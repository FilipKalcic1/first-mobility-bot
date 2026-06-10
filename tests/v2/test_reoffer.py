"""Tests for the 'nije točno' reoffer flow (Filip 2026-06-05).

We test `V2Engine._handle_reoffer` in isolation with a minimal hand-built
engine: fake pending_clarify_store, fake executor.method_of, a small
tkb_intents dict. We do NOT spin up the full LLM router — the handler
only consumes pre-cached cosine top-50 + shown ids from the saved
PendingClarify state.
"""
from __future__ import annotations

import pytest

from services.v2 import clarify_ui
from services.v2.engine import V2Engine
from services.v2.identity import IdentitySnapshot
from services.v2.pending_clarify import (
    PendingClarify, PendingClarifyStore, STAGE_TOOL,
)


# ---------- helpers ----------

class _FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}

    async def setex(self, key, ttl, value):
        self.store[key] = value

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, key):
        self.store.pop(key, None)


class _FakeExecutor:
    """Minimal executor — _handle_reoffer only calls method_of for label
    rendering."""

    @staticmethod
    def method_of(tool_id: str) -> str:
        if tool_id.startswith("delete_"):
            return "DELETE"
        if tool_id.startswith("post_"):
            return "POST"
        if tool_id.startswith("put_") or tool_id.startswith("patch_"):
            return "PUT"
        return "GET"


def _build_engine(redis) -> V2Engine:
    """Construct a stripped-down engine where only the reoffer surface
    needs to work. All other deps are None — _handle_reoffer doesn't
    touch them."""
    eng = V2Engine.__new__(V2Engine)
    eng.pending_clarify_store = PendingClarifyStore(redis)
    eng.executor = _FakeExecutor()
    eng.tkb_intents = {
        "get_VehicleCalendar": "Pokaži rezervacije vozila",
        "get_Vehicles": "Pokaži sva vozila",
        "delete_VehicleCalendar_id": "Otkaži rezervaciju po ID-u",
        "get_Persons": "Pokaži osobe",
        "get_Expenses": "Pokaži troškove",
        "get_LatestVehicleCalendar": "Pokaži aktualne rezervacije",
    }
    return eng


def _identity() -> IdentitySnapshot:
    return IdentitySnapshot(
        phone="385912345678",
        person_id="p-1", tenant_id="t-1",
        first_name="Filip", full_name="Filip K",
        vehicle_id="v-1", vehicle_name="DA053F VW Passat",
    )


# ---------- TESTS ----------

@pytest.mark.asyncio
async def test_reoffer_offers_next_three_from_cosine_top50():
    """User did 'pokaži rezervacije' → bot picked get_VehicleCalendar →
    user says 'nije točno'. Bot should offer next 3 cosine candidates
    (positions 4-6 from cached top-50) and NOT re-show get_VehicleCalendar."""
    redis = _FakeRedis()
    eng = _build_engine(redis)
    phone = "385912345678"

    # Simulate state saved after first execute
    await eng.pending_clarify_store.save(
        phone, candidates=[], original_query="moje rezervacije",
        stage=STAGE_TOOL,
        all_candidate_ids=[
            "get_VehicleCalendar",          # shown card #1 (also executed)
            "delete_VehicleCalendar_id",     # shown card #2
            "get_LatestVehicleCalendar",     # shown card #3
            "get_Vehicles",                  # position 4
            "get_Persons",                   # position 5
            "get_Expenses",                  # position 6
        ],
        shown_tool_ids=[
            "get_VehicleCalendar",
            "delete_VehicleCalendar_id",
            "get_LatestVehicleCalendar",
        ],
        last_executed_tool="get_VehicleCalendar",
        can_reoffer=True,
    )

    pc = await eng.pending_clarify_store.load(phone)
    reply = await eng._handle_reoffer(phone, pc, _identity())

    # Reply is the new picker text — should contain the "U redu, evo drugih
    # opcija:" header and 3 numbered cards.
    assert "U redu, evo drugih opcija:" in reply
    assert "1️⃣" in reply
    assert "2️⃣" in reply
    assert "3️⃣" in reply

    # State must be updated with EXACTLY the next 3 candidates (positions
    # 4-6 from all_candidate_ids = ["get_Vehicles", "get_Persons", "get_Expenses"]).
    updated = await eng.pending_clarify_store.load(phone)
    assert updated is not None
    assert "get_Vehicles" in updated.shown_tool_ids
    assert "get_Persons" in updated.shown_tool_ids
    assert "get_Expenses" in updated.shown_tool_ids
    # Originally shown still in list (cumulative — no re-offering)
    assert "get_VehicleCalendar" in updated.shown_tool_ids
    assert "delete_VehicleCalendar_id" in updated.shown_tool_ids
    assert "get_LatestVehicleCalendar" in updated.shown_tool_ids
    # 3 original + 3 new + 0 duplicates = 6
    assert len(updated.shown_tool_ids) == 6
    # Awaiting user pick + execute → reoffer disabled for this round
    assert updated.can_reoffer is False
    # Candidates list now contains the 3 new tool_ids for the picker resolver
    candidate_tool_ids = {c.get("tool_id") for c in updated.candidates}
    assert candidate_tool_ids == {"get_Vehicles", "get_Persons", "get_Expenses"}


@pytest.mark.asyncio
async def test_reoffer_two_rounds_no_duplicate_cards():
    """User says 'nije točno' twice in a row. Round 2 must offer 3 NEW
    candidates (positions 7-9), excluding everything previously shown
    including the round-1 offering."""
    redis = _FakeRedis()
    eng = _build_engine(redis)
    phone = "385912345678"

    # State after round 1 reoffer (positions 4-6 shown, none executed yet)
    await eng.pending_clarify_store.save(
        phone, candidates=[], original_query="q",
        stage=STAGE_TOOL,
        all_candidate_ids=[f"t{i}" for i in range(1, 11)],  # t1..t10
        shown_tool_ids=["t1", "t2", "t3", "t4", "t5", "t6"],
        last_executed_tool="t1",  # only t1 was actually executed
        can_reoffer=True,
    )

    pc = await eng.pending_clarify_store.load(phone)
    await eng._handle_reoffer(phone, pc, _identity())

    updated = await eng.pending_clarify_store.load(phone)
    # Should now have t7, t8, t9 added
    assert "t7" in updated.shown_tool_ids
    assert "t8" in updated.shown_tool_ids
    assert "t9" in updated.shown_tool_ids
    # No duplicates added
    assert len(updated.shown_tool_ids) == 9


@pytest.mark.asyncio
async def test_reoffer_zero_remaining_returns_graceful_message():
    """When all 50 candidates have been shown, bot says 'no more options'
    and clears state — no infinite loop."""
    redis = _FakeRedis()
    eng = _build_engine(redis)
    phone = "385912345678"

    await eng.pending_clarify_store.save(
        phone, candidates=[], original_query="q",
        stage=STAGE_TOOL,
        all_candidate_ids=["t1", "t2", "t3"],   # only 3 ever existed
        shown_tool_ids=["t1", "t2", "t3"],       # all shown
        last_executed_tool="t1",
        can_reoffer=True,
    )

    pc = await eng.pending_clarify_store.load(phone)
    reply = await eng._handle_reoffer(phone, pc, _identity())

    assert "Nemam više" in reply or "Opiši ponovo" in reply

    # State cleared
    after = await eng.pending_clarify_store.load(phone)
    assert after is None


@pytest.mark.asyncio
async def test_reoffer_l2b_sentinel_falls_through_to_action_picker():
    """When the executed tool was L2B driver-basics shortcut, reoffer
    re-routes through the full L3 cascade by saving an ACTION_GLOBAL
    pending state (lets user pick POGLEDATI/UNIJETI/IZMIJENITI/IZBRISATI)."""
    redis = _FakeRedis()
    eng = _build_engine(redis)
    phone = "385912345678"

    await eng.pending_clarify_store.save(
        phone, candidates=[], original_query="kolika km",
        stage=STAGE_TOOL,
        all_candidate_ids=[],   # L2B never computed cosine top-50
        shown_tool_ids=["__L2B_DRIVER_BASICS__"],
        last_executed_tool="__L2B_DRIVER_BASICS__",
        can_reoffer=True,
    )

    pc = await eng.pending_clarify_store.load(phone)
    reply = await eng._handle_reoffer(phone, pc, _identity())

    # Reply contains the universal action picker labels
    assert "POGLEDATI" in reply
    assert "UNIJETI" in reply or "KREIRATI" in reply

    # State now in ACTION_GLOBAL stage with original_query preserved
    after = await eng.pending_clarify_store.load(phone)
    assert after is not None
    assert after.stage == "action_global"
    assert after.original_query == "kolika km"


@pytest.mark.asyncio
async def test_reoffer_excludes_already_executed_even_if_not_in_shown():
    """Defensive: if shown_tool_ids missed the executed tool (race or bug),
    last_executed_tool still gets excluded from the next offer."""
    redis = _FakeRedis()
    eng = _build_engine(redis)
    phone = "385912345678"

    await eng.pending_clarify_store.save(
        phone, candidates=[], original_query="q",
        stage=STAGE_TOOL,
        all_candidate_ids=["t1", "t2", "t3", "t4"],
        shown_tool_ids=["t1"],   # incomplete — only t1 listed
        last_executed_tool="t2",  # but t2 was executed
        can_reoffer=True,
    )

    pc = await eng.pending_clarify_store.load(phone)
    await eng._handle_reoffer(phone, pc, _identity())

    updated = await eng.pending_clarify_store.load(phone)
    # Both t1 and t2 should be excluded → t3, t4 offered
    assert "t3" in updated.shown_tool_ids
    assert "t4" in updated.shown_tool_ids


# ---------- CORRECTION LABELS (Filip 2026-06-10, measure-first) ----------

@pytest.mark.asyncio
async def test_reoffer_carries_origin_tool_forward():
    """L3 reoffer must carry the rejected tool into the re-offered pending
    so the eventual pick can be logged as a (wrong, correct) golden label."""
    redis = _FakeRedis()
    eng = _build_engine(redis)
    phone = "385912345678"

    await eng.pending_clarify_store.save(
        phone, candidates=[], original_query="moje rezervacije",
        stage=STAGE_TOOL,
        all_candidate_ids=["get_VehicleCalendar", "get_Vehicles",
                           "get_Persons", "get_Expenses"],
        shown_tool_ids=["get_VehicleCalendar"],
        last_executed_tool="get_VehicleCalendar",  # the rejected tool
        can_reoffer=True,
    )

    pc = await eng.pending_clarify_store.load(phone)
    await eng._handle_reoffer(phone, pc, _identity())

    updated = await eng.pending_clarify_store.load(phone)
    assert updated.reoffer_origin_tool == "get_VehicleCalendar"


@pytest.mark.asyncio
async def test_reoffer_pick_emits_correction_label():
    """When the user resolves a 'nije točno' reoffer by picking a DIFFERENT
    tool, _resolve_pending_clarify logs a routing_correction telemetry event
    with {wrong_tool, correct_tool} — the free golden-set label."""
    redis = _FakeRedis()
    eng = _build_engine(redis)
    phone = "385912345678"

    # Capture telemetry + stub the heavy tail (param-ask, execute).
    logged: list[dict] = []

    async def _rec(**kw):
        logged.append(kw)

    async def _no_param(*a, **kw):
        return None

    async def _exec(*a, **kw):
        return "EXECUTED_OK"

    eng._log_telemetry = _rec
    eng._maybe_start_param_collection = _no_param
    eng._run_gate_and_execute = _exec

    # Re-offered pending: user already rejected delete_VehicleCalendar_id,
    # now offered get_VehicleCalendar as card #1.
    pending = PendingClarify(
        phone=phone,
        candidates=[{"tool_id": "get_VehicleCalendar", "params": {},
                     "field_hint": None}],
        original_query="moje rezervacije",
        stage=STAGE_TOOL,
        all_candidate_ids=["delete_VehicleCalendar_id", "get_VehicleCalendar"],
        shown_tool_ids=["delete_VehicleCalendar_id", "get_VehicleCalendar"],
        last_executed_tool=None,
        can_reoffer=False,
        reoffer_origin_tool="delete_VehicleCalendar_id",  # the rejected tool
    )

    reply = await eng._resolve_pending_clarify(phone, pending, "1", _identity())
    assert reply == "EXECUTED_OK"

    corrections = [k for k in logged if k.get("correction")]
    assert len(corrections) == 1, f"expected 1 correction event, got {logged}"
    c = corrections[0]["correction"]
    assert c == {"wrong_tool": "delete_VehicleCalendar_id",
                 "correct_tool": "get_VehicleCalendar"}
    assert corrections[0]["tool_picked"] == "get_VehicleCalendar"
    assert corrections[0]["query"] == "moje rezervacije"


@pytest.mark.asyncio
async def test_normal_pick_emits_no_correction_label():
    """A plain clarify pick (no reoffer origin) must NOT log a correction."""
    redis = _FakeRedis()
    eng = _build_engine(redis)
    phone = "385912345678"

    logged: list[dict] = []

    async def _rec(**kw):
        logged.append(kw)

    async def _no_param(*a, **kw):
        return None

    async def _exec(*a, **kw):
        return "EXECUTED_OK"

    eng._log_telemetry = _rec
    eng._maybe_start_param_collection = _no_param
    eng._run_gate_and_execute = _exec

    pending = PendingClarify(
        phone=phone,
        candidates=[{"tool_id": "get_VehicleCalendar", "params": {},
                     "field_hint": None}],
        original_query="moje rezervacije",
        stage=STAGE_TOOL,
        reoffer_origin_tool=None,  # ordinary pick, not a correction
    )

    await eng._resolve_pending_clarify(phone, pending, "1", _identity())
    assert not [k for k in logged if k.get("correction")]
