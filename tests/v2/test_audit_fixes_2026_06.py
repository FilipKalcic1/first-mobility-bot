"""Engine-level fixes from the 2026-06-11 full-system audit.

Covers:
  AUD-1: digit replies to a pending mutation confirm. The old "Odgovori
         brojem 1, 2 ili 3" menu was advertised but NO code path parsed
         digits → every advertised input looped back to "Nisam siguran".
         Now: 2/3 cancel safely; 1 re-prompts for an explicit 'Da'
         (digits never blind-execute — same asymmetry as parse_reply).
  AUD-2: _render_type_question announces hidden options ("i još N") when
         a *TypeId pick-list exceeds the 20 shown names.

Engine objects are built via object.__new__ (skip the heavy factory),
mirroring tests/v2/test_engine_orch_fixes.py.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Optional

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from services.v2.engine import V2Engine  # noqa: E402
from services.v2.identity import IdentitySnapshot  # noqa: E402
from services.v2.pending_mutation import PendingMutation, STAGE_SINGLE  # noqa: E402


class _MutStore:
    def __init__(self):
        self.clear_count = 0
        self.saved = None

    async def clear(self, phone):
        self.clear_count += 1

    async def save(self, phone, *, tool_id, params, stage):
        self.saved = (tool_id, params, stage)

    async def try_acquire_execution(self, phone, *a, **k):
        return True

    async def release_execution(self, phone):
        pass


class _NeverExecutor:
    """Digit replies must NEVER reach execute()."""
    def __init__(self):
        self.call_count = 0

    async def execute(self, **kwargs):
        self.call_count += 1
        raise AssertionError("execute() must not be called for digit replies")


def _mut_engine(store: Optional[_MutStore] = None, executor=None):
    eng = object.__new__(V2Engine)
    eng.pending_mut_store = store or _MutStore()
    eng.executor = executor or _NeverExecutor()
    eng.api_error_translator = None
    eng.tkb_intents = {}
    eng.tool_parameters = {}
    return eng


def _pending(age_s: float = 0.0) -> PendingMutation:
    return PendingMutation(
        tool_id="delete_VehicleCalendar_id",
        params={"id": "x1"},
        stage=STAGE_SINGLE,
        created_at=time.time() - age_s,
    )


def _identity():
    return IdentitySnapshot(
        phone="385999", person_id="p1",
        tenant_id="t1", vehicle_id="v1", vehicle_name="VW Golf",
    )


# ---------------------------------------------------------------------------
# AUD-1 — digit replies to pending confirm
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("digit", ["2", "3", "2.", "3!"])
async def test_digit_2_or_3_cancels_pending_confirm(digit):
    store = _MutStore()
    executor = _NeverExecutor()
    eng = _mut_engine(store, executor)

    reply = await eng._continue_pending_mutation(
        "385999", _pending(), digit, _identity(),
    )

    assert store.clear_count == 1, "digit 2/3 must clear the pending confirm"
    assert "otkaz" in reply.lower()
    assert executor.call_count == 0


@pytest.mark.asyncio
async def test_digit_1_reprompts_for_explicit_da_never_executes():
    """'1' commonly means yes in menus, but a blind execute on a digit is the
    one unrecoverable mistake — re-prompt for an explicit 'Da' instead."""
    store = _MutStore()
    executor = _NeverExecutor()
    eng = _mut_engine(store, executor)

    reply = await eng._continue_pending_mutation(
        "385999", _pending(), "1", _identity(),
    )

    assert store.clear_count == 0, "pending must survive the '1' re-prompt"
    assert "'Da'" in reply
    assert executor.call_count == 0


@pytest.mark.asyncio
async def test_new_query_guard_message_only_advertises_parseable_inputs():
    """The multi-pending guard message must only offer inputs that the
    parser actually understands (Da / Ne) — the old 1/2/3 menu was a lie."""
    eng = _mut_engine()

    reply = await eng._continue_pending_mutation(
        "385999", _pending(), "obriši moju rezervaciju sutra", _identity(),
    )

    assert "Da" in reply and "Ne" in reply
    assert "brojem 1" not in reply


# ---------------------------------------------------------------------------
# AUD-2 — *TypeId pick-list announces hidden options
# ---------------------------------------------------------------------------


def _type_engine():
    eng = object.__new__(V2Engine)
    eng.tool_parameters = {"post_AddExpense": {"ExpenseTypeId": {
        "param_type": "string", "dependency_source": "user_input",
    }}}
    eng.param_labeler = None
    return eng


@pytest.mark.asyncio
async def test_type_question_announces_hidden_options_beyond_20():
    eng = _type_engine()
    pairs = [(str(i), f"Tip{i}") for i in range(25)]

    q = await eng._render_type_question("post_AddExpense", "ExpenseTypeId", pairs)

    assert "Tip0" in q and "Tip19" in q
    assert "Tip20" not in q  # cut at 20 shown
    assert "i još 5" in q    # but the cut is announced


@pytest.mark.asyncio
async def test_type_question_short_list_has_no_more_suffix():
    eng = _type_engine()
    pairs = [("1", "Gorivo"), ("2", "Servis")]

    q = await eng._render_type_question("post_AddExpense", "ExpenseTypeId", pairs)

    assert "Gorivo" in q and "Servis" in q
    assert "i još" not in q
