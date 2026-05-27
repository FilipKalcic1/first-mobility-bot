"""ORCHESTRATOR-layer fixes (Filip 2026-05-20 — audit of Dio 2).

Covers the verified bugs from the 3-agent ORCHESTRATOR audit:
  ORCH-1: _continue_flow passed identity_summary={} → executor refused
          tenant-scoped tools with missing_tenant_id → ALL flows broke.
  ORCH-4: flow state was cleared BEFORE execute → on failure the user lost
          all progress and couldn't retry the confirm.

Tests drive _continue_flow directly with a fake flow_engine returning
OUTCOME_EXECUTE + a capturing executor, built via object.__new__ to skip
the heavy production factory.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from services.v2.engine import V2Engine  # noqa: E402
from services.v2.flow_engine import (  # noqa: E402
    FlowOutcome, OUTCOME_EXECUTE,
)
from services.v2.identity import IdentitySnapshot  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class FakeExecResult:
    success: bool
    data: object = None
    error: str = ""
    error_body: object = None
    status_code: Optional[int] = None


class CapturingExecutor:
    """Captures the identity_summary passed to execute()."""
    def __init__(self, result: FakeExecResult):
        self._result = result
        self.captured_identity: Optional[dict] = None
        self.call_count = 0

    async def execute(self, *, tool_id, params, identity_summary):
        self.call_count += 1
        self.captured_identity = identity_summary
        return self._result


class FakeFlowEngine:
    """handle() always returns a scripted OUTCOME_EXECUTE outcome."""
    def __init__(self, tool_id="post_AddMileage", params=None):
        self._outcome = FlowOutcome(
            kind=OUTCOME_EXECUTE,
            tool_id=tool_id,
            params=params or {"Value": 145000},
        )

    def handle(self, state, user_input):
        return self._outcome


class FakeFlowStore:
    def __init__(self):
        self.cleared = False
        self.clear_count = 0

    async def clear(self, phone):
        self.cleared = True
        self.clear_count += 1

    async def save(self, phone, state):
        pass


def _make_engine(executor, flow_store) -> V2Engine:
    """Construct V2Engine without the production factory."""
    eng = object.__new__(V2Engine)
    eng.flow_engine = FakeFlowEngine()
    eng.flow_store = flow_store
    eng.executor = executor
    # _render_execution_failure path needs api_error_translator + tkb_intents;
    # set to None so it falls back to the generic message (no LLM in tests).
    eng.api_error_translator = None
    eng.tkb_intents = {}
    # _continue_flow now coerces params before execute (same as [C] path) —
    # _coerce_llm_params reads self.tool_parameters; empty = no-op here.
    eng.tool_parameters = {}
    return eng


def _identity(tenant="tenant-123", vehicle="veh-9"):
    return IdentitySnapshot(
        phone="385999",
        person_id="p1",
        tenant_id=tenant,
        vehicle_id=vehicle,
        vehicle_name="VW Golf",
    )


# ---------------------------------------------------------------------------
# ORCH-5: pending_params cleared only AFTER finalize succeeds
# ---------------------------------------------------------------------------


class _ParamStore:
    """Tracks clear() calls + holds saved state."""
    def __init__(self):
        self.clear_count = 0
        self.saved = None

    async def clear(self, phone):
        self.clear_count += 1

    async def save(self, phone, pending):
        self.saved = pending


def _make_param_engine(param_store, finalize_raises: bool):
    """Engine stub for _resolve_pending_params 3c path (required drained,
    no optionals → finalize)."""
    eng = object.__new__(V2Engine)
    eng.pending_params_store = param_store
    eng.tool_parameters = {"post_AddMileage": {
        "Value": {"required": True, "dependency_source": "user_input",
                  "param_type": "integer"},
    }}
    eng.optional_extractor = None

    async def _finalize(phone, pending, identity):
        if finalize_raises:
            raise RuntimeError("simulated finalize failure (e.g. Redis save)")
        return "Potvrđuješ unos? Da/Ne"

    eng._finalize_after_params = _finalize

    async def _label_for(tool_id, name, pdef):
        return name
    eng._label_for = _label_for
    return eng


@pytest.mark.asyncio
async def test_orch5_params_NOT_cleared_when_finalize_raises():
    """ORCH-5: if finalize raises (e.g. Redis save of pending_mutation fails),
    pending_params must NOT be cleared — user keeps collected params + retries.
    The bug found while writing this test: 3 clear-before-finalize sites
    existed, only 1 was fixed in the first ORCH-5 pass."""
    from services.v2.pending_params import PendingParams

    store = _ParamStore()
    eng = _make_param_engine(store, finalize_raises=True)
    # Last required param; user provides value → required drained → 3c finalize.
    pending = PendingParams(
        phone="385999", tool_id="post_AddMileage", collected={},
        required_remaining=["Value"], optional_remaining=[],
    )

    with pytest.raises(RuntimeError):
        await eng._resolve_pending_params(
            "385999", pending, "145000", _identity(),
        )

    assert store.clear_count == 0, (
        "pending_params must NOT be cleared when finalize raises"
    )


@pytest.mark.asyncio
async def test_orch5_params_cleared_when_finalize_succeeds():
    """Happy path: successful finalize → pending_params cleared exactly once."""
    from services.v2.pending_params import PendingParams

    store = _ParamStore()
    eng = _make_param_engine(store, finalize_raises=False)
    pending = PendingParams(
        phone="385999", tool_id="post_AddMileage", collected={},
        required_remaining=["Value"], optional_remaining=[],
    )

    reply = await eng._resolve_pending_params(
        "385999", pending, "145000", _identity(),
    )

    assert store.clear_count == 1
    assert "Potvr" in reply


# ---------------------------------------------------------------------------
# ORCH-1: flow execute carries real tenant identity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orch1_flow_execute_passes_tenant_identity():
    """The HIGH bug: _continue_flow used to pass identity_summary={}, so the
    executor refused with missing_tenant_id. Now it must pass tenant_id."""
    executor = CapturingExecutor(FakeExecResult(success=True, data={"ok": 1}))
    store = FakeFlowStore()
    eng = _make_engine(executor, store)

    await eng._continue_flow("385999", state=object(), user_input="da",
                             identity=_identity(tenant="damir-tenant"))

    assert executor.captured_identity is not None
    assert executor.captured_identity.get("tenant_id") == "damir-tenant", (
        f"flow execute must carry tenant_id, got {executor.captured_identity}"
    )


@pytest.mark.asyncio
async def test_orch1_no_identity_falls_back_to_empty():
    """Defensive: if identity is None (shouldn't happen in prod), don't crash —
    pass {} (executor will then refuse tenant-scoped tools, which is correct)."""
    executor = CapturingExecutor(FakeExecResult(success=True))
    store = FakeFlowStore()
    eng = _make_engine(executor, store)

    await eng._continue_flow("385999", state=object(), user_input="da",
                             identity=None)

    assert executor.captured_identity == {}


# ---------------------------------------------------------------------------
# ORCH-4: flow state cleared only on success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orch4_flow_state_NOT_cleared_on_execute_failure():
    """If the API call fails, flow state must survive so the user can retry
    the confirm — previously it was cleared before execute, killing the flow."""
    executor = CapturingExecutor(
        FakeExecResult(success=False, error="api_500", status_code=500)
    )
    store = FakeFlowStore()
    eng = _make_engine(executor, store)

    reply = await eng._continue_flow("385999", state=object(), user_input="da",
                                     identity=_identity())

    assert store.cleared is False, "flow state must NOT be cleared on failure"
    assert "nije uspjela" in reply.lower() or "tehni" in reply.lower()


@pytest.mark.asyncio
async def test_orch4_flow_state_cleared_on_execute_success():
    """Happy path: successful execute clears the flow state exactly once."""
    executor = CapturingExecutor(FakeExecResult(success=True, data={"Id": 5}))
    store = FakeFlowStore()
    eng = _make_engine(executor, store)

    await eng._continue_flow("385999", state=object(), user_input="da",
                             identity=_identity())

    assert store.cleared is True
    assert store.clear_count == 1
