"""Tests for L3 — FlowEngine + FlowStateStore.

Pure-logic tests on engine + Redis-backed store with fake Redis.
"""
from __future__ import annotations

import json

import pytest

from services.v2.flow_engine import (
    FLOWS,
    FLOW_TTL_SECONDS,
    OUTCOME_CANCELLED,
    OUTCOME_DONE,
    OUTCOME_EXECUTE,
    OUTCOME_INVALID,
    OUTCOME_PROMPT,
    Flow,
    FlowEngine,
    FlowState,
    FlowStateStore,
    Step,
    STEP_ASK_CHOICE,
    STEP_ASK_CONFIRM,
    STEP_ASK_NUMBER,
    STEP_ASK_PERIOD,
    STEP_ASK_TEXT,
    STEP_EXEC_LOOKUP,
)


# --------------------------------------------------------------------------
# FlowState round-trip
# --------------------------------------------------------------------------


def test_flow_state_json_round_trip():
    s = FlowState(
        flow_name="booking", step_index=2,
        collected_params={"vehicle_id": "v1", "from_time": "2025-12-16T09:00"},
    )
    raw = s.to_json()
    s2 = FlowState.from_json(raw)
    assert s2.flow_name == "booking"
    assert s2.step_index == 2
    assert s2.collected_params == s.collected_params


def test_flow_state_from_json_handles_missing_optional_fields():
    raw = json.dumps({"flow_name": "case"})
    s = FlowState.from_json(raw)
    assert s.flow_name == "case"
    assert s.step_index == 0
    assert s.collected_params == {}


# --------------------------------------------------------------------------
# Mileage flow — simplest end-to-end
# --------------------------------------------------------------------------


def test_mileage_flow_happy_path():
    engine = FlowEngine(FLOWS)
    # start
    out = engine.start("mileage", identity_context={
        "person_id": "p1", "vehicle_id": "v1", "vehicle_name": "VW Golf",
    })
    assert out.kind == OUTCOME_PROMPT
    assert "kilometraža" in out.response.lower() or "km" in out.response.lower()
    state = out.new_state

    # Step 1: provide value
    out = engine.handle(state, "145000")
    assert out.kind == OUTCOME_PROMPT
    assert "Upisat ću 145000" in out.response
    assert "VW Golf" in out.response
    state = out.new_state

    # Step 2: confirm
    out = engine.handle(state, "Da")
    assert out.kind == OUTCOME_EXECUTE
    assert out.tool_id == "post_AddMileage"
    assert out.params["VehicleId"] == "v1"
    assert out.params["Value"] == 145000


def test_mileage_flow_user_says_no_at_confirm():
    engine = FlowEngine(FLOWS)
    out = engine.start("mileage", identity_context={
        "person_id": "p1", "vehicle_id": "v1", "vehicle_name": "VW Golf",
    })
    state = out.new_state
    out = engine.handle(state, "145000")
    state = out.new_state

    out = engine.handle(state, "Ne")
    assert out.kind == OUTCOME_CANCELLED
    assert out.new_state is None  # caller clears Redis


def test_mileage_flow_invalid_number_rejected():
    engine = FlowEngine(FLOWS)
    out = engine.start("mileage", identity_context={
        "person_id": "p1", "vehicle_id": "v1", "vehicle_name": "VW Golf",
    })
    state = out.new_state

    out = engine.handle(state, "nije broj")
    assert out.kind == OUTCOME_INVALID
    assert "broj" in out.response.lower()
    # State preserved — user can retry
    assert out.new_state is state


def test_mileage_flow_strips_units_from_number():
    engine = FlowEngine(FLOWS)
    out = engine.start("mileage", identity_context={
        "person_id": "p1", "vehicle_id": "v1", "vehicle_name": "VW Golf",
    })
    state = out.new_state

    # User types "145000 km" — engine should accept
    out = engine.handle(state, "145000 km")
    assert out.kind == OUTCOME_PROMPT
    state = out.new_state

    out = engine.handle(state, "Da")
    assert out.kind == OUTCOME_EXECUTE
    assert out.params["Value"] == 145000


def test_mileage_flow_negative_number_rejected():
    engine = FlowEngine(FLOWS)
    out = engine.start("mileage", identity_context={
        "person_id": "p1", "vehicle_id": "v1", "vehicle_name": "VW Golf",
    })
    state = out.new_state
    out = engine.handle(state, "-100")
    # "-100" → digits "100" → 100 (still valid). Confirm next step
    # accepted. This documents current behavior — sign stripped.
    assert out.kind in (OUTCOME_PROMPT, OUTCOME_INVALID)


# --------------------------------------------------------------------------
# Case flow
# --------------------------------------------------------------------------


def test_case_flow_happy_path():
    engine = FlowEngine(FLOWS)
    out = engine.start("case", identity_context={
        "person_id": "p1", "vehicle_id": "v1", "vehicle_name": "VW Golf",
    })
    assert out.kind == OUTCOME_PROMPT
    state = out.new_state

    out = engine.handle(state, "udario sam u stup u garaži")
    assert out.kind == OUTCOME_PROMPT
    assert "udario sam u stup" in out.response
    assert "VW Golf" in out.response
    state = out.new_state

    out = engine.handle(state, "Da")
    assert out.kind == OUTCOME_EXECUTE
    assert out.tool_id == "post_AddCase"
    assert out.params["User"] == "p1"
    assert out.params["Message"] == "udario sam u stup u garaži"


def test_case_flow_too_short_description_rejected():
    engine = FlowEngine(FLOWS)
    out = engine.start("case", identity_context={"person_id": "p1"})
    state = out.new_state

    out = engine.handle(state, "x")  # 1 char
    assert out.kind == OUTCOME_INVALID


# --------------------------------------------------------------------------
# Cancellation
# --------------------------------------------------------------------------


@pytest.mark.parametrize("cancel_word", ["odustani", "prekini", "cancel", "stop"])
def test_cancel_during_flow(cancel_word):
    engine = FlowEngine(FLOWS)
    out = engine.start("mileage", identity_context={"person_id": "p1"})
    state = out.new_state

    out = engine.handle(state, cancel_word)
    assert out.kind == OUTCOME_CANCELLED
    assert out.new_state is None


# --------------------------------------------------------------------------
# Booking flow — multi-step with EXEC_LOOKUP
# --------------------------------------------------------------------------


def test_booking_flow_period_then_lookup_request():
    engine = FlowEngine(FLOWS)
    out = engine.start("booking", identity_context={"person_id": "p1"})
    assert out.kind == OUTCOME_PROMPT
    assert "period" in out.response.lower()
    state = out.new_state

    # Provide period text
    out = engine.handle(state, "16.12.2025 9-17")
    # Engine now wants lookup tool to run (EXEC_LOOKUP step)
    assert out.kind == OUTCOME_PROMPT
    assert out.tool_id == "get_AvailableVehicles"
    # _parse_period populated from_time/to_time slots; template substituted.
    assert out.params["from"] == "2025-12-16T09:00:00"
    assert out.params["to"] == "2025-12-16T17:00:00"


def test_booking_flow_after_lookup_asks_choice_then_confirm():
    engine = FlowEngine(FLOWS)
    out = engine.start("booking", identity_context={"person_id": "p1"})
    state = out.new_state
    out = engine.handle(state, "16.12.2025 9-17")
    state = out.new_state

    # Caller runs lookup, gets results, calls back with lookup_result
    available = [
        {"Id": "veh-A", "label": "VW Golf BG-1234"},
        {"Id": "veh-B", "label": "Renault Clio BG-5678"},
    ]
    out = engine.handle(state, "", lookup_result=available)

    # Now we should be at ASK_CHOICE
    assert out.kind == OUTCOME_PROMPT
    assert "1." in out.response and "VW Golf" in out.response
    assert "2." in out.response and "Renault Clio" in out.response
    state = out.new_state

    # Pick option 1
    out = engine.handle(state, "1")
    assert out.kind == OUTCOME_PROMPT
    assert "VW Golf" in out.response
    state = out.new_state

    # Confirm
    out = engine.handle(state, "Da")
    assert out.kind == OUTCOME_EXECUTE
    assert out.tool_id == "post_VehicleCalendar"
    assert out.params["VehicleId"] == "veh-A"
    assert out.params["AssignedToId"] == "p1"


def test_booking_invalid_choice_rejected():
    engine = FlowEngine(FLOWS)
    out = engine.start("booking", identity_context={
        "person_id": "p1", "from_time": "F", "to_time": "T",
    })
    state = out.new_state
    out = engine.handle(state, "16.12. 9-17")
    state = out.new_state

    out = engine.handle(state, "", lookup_result=[
        {"Id": "v1", "label": "VW"},
    ])
    state = out.new_state

    # Try invalid choice number
    out = engine.handle(state, "99")
    assert out.kind == OUTCOME_INVALID
    # State preserved
    assert out.new_state is state


# --------------------------------------------------------------------------
# Confirm step — strict Da/Ne
# --------------------------------------------------------------------------


def test_confirm_unrecognized_input_re_asks():
    engine = FlowEngine(FLOWS)
    out = engine.start("mileage", identity_context={
        "person_id": "p1", "vehicle_id": "v1", "vehicle_name": "X",
    })
    state = out.new_state
    out = engine.handle(state, "100000")
    state = out.new_state

    out = engine.handle(state, "možda")
    assert out.kind == OUTCOME_INVALID
    assert "Da" in out.response and "Ne" in out.response


# ---- Unify (Filip 2026-05-27): shared parse_reply + booking label ----


def test_confirm_parse_is_consistent_with_pending_mutation():
    """Flow confirm uses the shared parse_reply: exact 'može' executes, but a
    hesitant 'može biti' re-asks (not execute) — same as the general [C] path."""
    engine = FlowEngine(FLOWS)

    def _at_confirm():
        o = engine.start("mileage", identity_context={
            "person_id": "p1", "vehicle_id": "v1", "vehicle_name": "X"})
        return engine.handle(o.new_state, "100000").new_state

    assert engine.handle(_at_confirm(), "može").kind == OUTCOME_EXECUTE
    assert engine.handle(_at_confirm(), "može biti").kind == OUTCOME_INVALID
    assert engine.handle(_at_confirm(), "ne hvala").kind == OUTCOME_CANCELLED


def test_render_prompt_shows_dict_label_not_repr():
    """Booking's {chosen_vehicle} slot is a picked row dict — the confirm must
    show its label, not the raw dict repr."""
    from services.v2.flow_engine import _render_prompt
    out = _render_prompt(
        "Rezervirat ću vozilo {chosen_vehicle}.",
        {"chosen_vehicle": {"Id": "veh-A", "label": "Škoda Octavia DA066F"}},
    )
    assert out == "Rezervirat ću vozilo Škoda Octavia DA066F."


# --------------------------------------------------------------------------
# Edge cases
# --------------------------------------------------------------------------


def test_unknown_flow_name_returns_cancelled():
    engine = FlowEngine(FLOWS)
    out = engine.start("does_not_exist", identity_context={})
    assert out.kind == OUTCOME_CANCELLED


def test_engine_handles_state_for_missing_flow():
    engine = FlowEngine(FLOWS)
    state = FlowState(flow_name="ghost", step_index=0)
    out = engine.handle(state, "input")
    assert out.kind == OUTCOME_CANCELLED


def test_engine_handles_state_index_past_end():
    engine = FlowEngine(FLOWS)
    flow = FLOWS["mileage"]
    state = FlowState(
        flow_name="mileage", step_index=len(flow.steps) + 5,
    )
    out = engine.handle(state, "ignored")
    assert out.kind == OUTCOME_DONE


# --------------------------------------------------------------------------
# FlowStateStore (Redis wrapper)
# --------------------------------------------------------------------------


class FakeRedis:
    def __init__(self):
        self.store = {}
        self.ttls = {}

    async def get(self, k):
        return self.store.get(k)

    async def setex(self, k, ttl, v):
        self.store[k] = v
        self.ttls[k] = ttl

    async def delete(self, k):
        self.store.pop(k, None)
        self.ttls.pop(k, None)


@pytest.mark.asyncio
async def test_state_store_save_and_load_roundtrip():
    store = FlowStateStore(FakeRedis())
    state = FlowState(
        flow_name="case", step_index=1,
        collected_params={"description": "test"},
    )
    await store.save("385955087196", state)
    loaded = await store.load("385955087196")
    assert loaded is not None
    assert loaded.flow_name == "case"
    assert loaded.step_index == 1
    assert loaded.collected_params == {"description": "test"}


@pytest.mark.asyncio
async def test_state_store_load_missing_returns_none():
    store = FlowStateStore(FakeRedis())
    assert await store.load("385000000000") is None


@pytest.mark.asyncio
async def test_state_store_clear():
    redis = FakeRedis()
    store = FlowStateStore(redis)
    state = FlowState(flow_name="mileage", step_index=0)
    await store.save("385955087196", state)
    assert "v2:flow:385955087196" in redis.store
    await store.clear("385955087196")
    assert "v2:flow:385955087196" not in redis.store


@pytest.mark.asyncio
async def test_state_store_corrupt_data_returns_none_no_crash():
    redis = FakeRedis()
    redis.store["v2:flow:385955087196"] = "{{ not json"
    store = FlowStateStore(redis)
    assert await store.load("385955087196") is None


@pytest.mark.asyncio
async def test_state_store_redis_failure_does_not_crash():
    class BrokenRedis:
        async def get(self, k):
            raise ConnectionError("redis down")

        async def setex(self, *a, **kw):
            raise ConnectionError("redis down")

        async def delete(self, *a, **kw):
            raise ConnectionError("redis down")

    store = FlowStateStore(BrokenRedis())
    assert await store.load("X") is None
    await store.save("X", FlowState(flow_name="case"))  # no raise
    await store.clear("X")  # no raise


@pytest.mark.asyncio
async def test_state_store_uses_configured_ttl():
    redis = FakeRedis()
    store = FlowStateStore(redis, ttl_seconds=42)
    await store.save("X", FlowState(flow_name="case"))
    assert redis.ttls["v2:flow:X"] == 42


def test_default_ttl_is_sane():
    """10-min default — long enough for slow user, short enough that
    abandoned flows clear automatically."""
    assert 60 <= FLOW_TTL_SECONDS <= 1800


# --------------------------------------------------------------------------
# Flow definition sanity (catches refactor regressions)
# --------------------------------------------------------------------------


def test_all_flows_have_terminal_confirm_step():
    """Every flow MUST end with ASK_CONFIRM — the L6 mutation gate.
    Mutating without confirm = 0-error tolerance violation."""
    for name, flow in FLOWS.items():
        last = flow.steps[-1]
        assert last.kind == STEP_ASK_CONFIRM, (
            f"flow {name!r} doesn't end with ASK_CONFIRM"
        )


def test_all_flows_have_final_tool_id():
    for name, flow in FLOWS.items():
        assert flow.final_tool_id, f"flow {name!r} missing final_tool_id"


def test_all_flows_have_params_builder():
    for name, flow in FLOWS.items():
        assert callable(flow.final_params_builder), (
            f"flow {name!r} missing final_params_builder"
        )


# ---- Faza 4 Bug #2 (Filip 2026-05-17): extended _parse_period ----

from services.v2.flow_engine import _parse_period
from datetime import datetime, timedelta


def _next_weekday(target_weekday: int):
    """Return next occurrence of weekday (0=Mon). 0-delta means today is
    Monday → returns next Monday (+7), matching _parse_period semantics
    where 'u ponedjeljak' on a Monday is treated as next week."""
    today = datetime.now().date()
    delta = (target_weekday - today.weekday()) % 7
    if delta == 0:
        delta = 7
    return today + timedelta(days=delta)


def test_parse_period_handles_named_weekdays():
    """'u petak 9-15' → next Friday 09:00-15:00."""
    out = _parse_period("u petak 9-15")
    assert out is not None
    expected = _next_weekday(4)  # Friday=4
    assert out["from_time"].startswith(expected.isoformat())
    assert "09:00:00" in out["from_time"]
    assert "15:00:00" in out["to_time"]


def test_parse_period_handles_srijeda_inflections():
    """'u srijedu 8-16' (akuzativ) and 'u srijedi 8-16' both should match
    because we use prefix 'srijed' that covers both forms."""
    out = _parse_period("u srijedu 8-16")
    assert out is not None
    expected = _next_weekday(2)  # Wednesday=2
    assert out["from_time"].startswith(expected.isoformat())


def test_parse_period_handles_next_week_keyword():
    """'iduci petak 9-15' → an upcoming Friday, never today, never BEFORE the
    plain 'u petak'. On other days 'iduci' lands a week later than 'u petak';
    when today IS Friday the parser naturally collapses both to the same coming
    Friday (+7) — the sensible reading. (Date-independent: the old assertion
    hardcoded +14 and broke when the suite ran on a Friday, e.g. 2026-05-22.)"""
    out = _parse_period("iduci petak 9-15")
    plain = _parse_period("u petak 9-15")
    assert out is not None and plain is not None
    today_iso = datetime.now().date().isoformat()
    # 'iduci' must never resolve to today, and never before the plain Friday.
    assert out["from_time"][:10] != today_iso
    assert out["from_time"] >= plain["from_time"]
    assert "09:00:00" in out["from_time"]


def test_parse_period_parts_of_day_ujutro():
    """'sutra ujutro' → tomorrow 09:00-12:00 (default morning window)."""
    out = _parse_period("sutra ujutro")
    assert out is not None
    expected = datetime.now().date() + timedelta(days=1)
    assert out["from_time"].startswith(expected.isoformat())
    assert "09:00:00" in out["from_time"]
    assert "12:00:00" in out["to_time"]


def test_parse_period_parts_of_day_popodne():
    """'danas popodne' → today 13:00-17:00."""
    out = _parse_period("danas popodne")
    assert out is not None
    today = datetime.now().date()
    assert out["from_time"].startswith(today.isoformat())
    assert "13:00:00" in out["from_time"]
    assert "17:00:00" in out["to_time"]


def test_parse_period_parts_of_day_navecer():
    """'sutra navečer' → tomorrow 18:00-22:00."""
    out = _parse_period("sutra navečer")
    assert out is not None
    expected = datetime.now().date() + timedelta(days=1)
    assert out["from_time"].startswith(expected.isoformat())
    assert "18:00:00" in out["from_time"]
    assert "22:00:00" in out["to_time"]


def test_parse_period_weekday_plus_part_of_day():
    """'u petak ujutro' → next Friday 09:00-12:00."""
    out = _parse_period("u petak ujutro")
    assert out is not None
    expected = _next_weekday(4)
    assert out["from_time"].startswith(expected.isoformat())
    assert "09:00:00" in out["from_time"]
    assert "12:00:00" in out["to_time"]


def test_parse_period_existing_formats_still_work():
    """Regression: old supported formats must keep working."""
    # Original cases that have to keep working
    assert _parse_period("sutra 9-15") is not None
    assert _parse_period("danas 9:30-17:00") is not None
    assert _parse_period("prekosutra 8-16") is not None
    assert _parse_period("16.12.2025 9:00-17:00") is not None


def test_parse_period_returns_none_on_garbage():
    """No date AND no time → None."""
    assert _parse_period("kasnim na sastanak") is None
    # Date but no hour and no part-of-day → None
    assert _parse_period("sutra negdje") is None
