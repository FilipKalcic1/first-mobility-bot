"""Tests for V2Engine._v3_route() — V3 hierarchical routing wire-up.

V3 is opt-in via V2_USE_V3_ROUTER=1 env flag AND domain_picker/scoped_picker
must be wired. When OFF (default), engine behavior is unchanged — these
tests verify the gate, the special_intents inline responses, and graceful
fall-through to V2 path on uncertainty.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pytest

from services.v2.domain_picker import DomainDecision, DomainHit, DomainPicker
from services.v2.domain_scoped_picker import (
    DomainScopedToolPicker, ScopedDecision, ScopedToolHit,
)
from services.v2.engine import V2Engine
from services.v2.identity import IdentitySnapshot


# ---- Fakes ----

class _FakePicker:
    """Stub for DomainPicker that returns a pre-set decision."""

    def __init__(self, decision: DomainDecision):
        self._decision = decision

    async def pick(self, query: str, persona: str = "driver", is_known: bool = True, recent_turns=None):
        return self._decision


class _FakeScopedPicker:
    def __init__(self, decision: ScopedDecision):
        self._decision = decision

    async def pick(self, query: str, domain_id: str, persona: str = "driver", recent_turns=None):
        return self._decision

    def tkb_entry_for(self, tool_id: str):
        return None


def _make_engine(domain_decision=None, scoped_decision=None) -> V2Engine:
    """Helper: build a V2Engine with stubbed V3 pickers, no other deps."""
    return V2Engine(
        rate_limiter=None,
        pii=None,
        identity=None,
        intent_type=None,
        basics=None,
        recognition=None,
        flow_engine=None,
        flow_store=None,
        executor=None,
        pending_mut_store=None,
        domain_picker=_FakePicker(domain_decision) if domain_decision else None,
        scoped_picker=_FakeScopedPicker(scoped_decision) if scoped_decision else None,
    )


def _make_identity(known: bool = True) -> IdentitySnapshot:
    return IdentitySnapshot(
        person_id="x" if known else None,
        full_name="Test User" if known else None,
        first_name="Test" if known else None,
        tenant_id="tenant_1",
        vehicle_name="VW Passat",
        licence_plate="DA053F",
        vin="...",
        last_mileage=30000,
        leasing_company="Porsche Leasing",
        co2_emission=110,
        registration_expiry="2026-05-13",
        phone="385912345678",
        is_first_contact=False,
    )


# ---- Tests ----


def test_engine_default_v3_pickers_none():
    """V3 path is opt-in. Default V2Engine has no V3 pickers — V3 path skipped."""
    engine = _make_engine(domain_decision=None, scoped_decision=None)
    assert engine.domain_picker is None
    assert engine.scoped_picker is None


@pytest.mark.asyncio
async def test_v3_route_special_intents_greeting():
    decision = DomainDecision(
        top_picks=[DomainHit(domain_id="special_intents", label="x", confidence=0.95)],
    )
    engine = _make_engine(domain_decision=decision)
    result = await engine._v3_route("385912345678", "Bok", _make_identity())
    assert result is not None
    assert "Bok" in result
    assert "pomoći" in result.lower() or "kako" in result.lower()


@pytest.mark.asyncio
async def test_v3_route_special_intents_english():
    decision = DomainDecision(
        top_picks=[DomainHit(domain_id="special_intents", label="x", confidence=0.99)],
    )
    engine = _make_engine(domain_decision=decision)
    result = await engine._v3_route(
        "385912345678", "hello can i get my car", _make_identity(),
    )
    assert result is not None
    assert "hrvatski" in result.lower()


@pytest.mark.asyncio
async def test_v3_route_special_intents_personal_info():
    decision = DomainDecision(
        top_picks=[DomainHit(domain_id="special_intents", label="x", confidence=0.99)],
    )
    engine = _make_engine(domain_decision=decision)
    result = await engine._v3_route(
        "385912345678", "kako se ja zovem", _make_identity(),
    )
    assert result is not None
    assert "privatn" in result.lower() or "ne mogu" in result.lower()


@pytest.mark.asyncio
async def test_v3_route_special_intents_destructive_profile():
    decision = DomainDecision(
        top_picks=[DomainHit(domain_id="special_intents", label="x", confidence=0.99)],
    )
    engine = _make_engine(domain_decision=decision)
    result = await engine._v3_route(
        "385912345678", "obriši moj profil", _make_identity(),
    )
    assert result is not None
    assert "administrator" in result.lower() or "nije dostupno" in result.lower()


@pytest.mark.asyncio
async def test_v3_route_renders_top3_cards_on_low_confidence():
    """When Stage-1 not confident BUT has 2+ picks, render Top-3 cards UX
    (smart user load shifting — never silent, never wrong)."""
    decision = DomainDecision(
        top_picks=[
            DomainHit(domain_id="vehicle_info", label="Info o vozilu", confidence=0.7),
            DomainHit(domain_id="reservations", label="Rezervacije vozila", confidence=0.65),
            DomainHit(domain_id="cases", label="Prijave šteta", confidence=0.4),
        ],
    )
    engine = _make_engine(domain_decision=decision)
    result = await engine._v3_route("385912345678", "auto", _make_identity())
    assert result is not None
    assert "1️⃣" in result and "2️⃣" in result
    assert "Info o vozilu" in result
    assert "Rezervacije" in result
    assert "❌" in result  # opt-out option


@pytest.mark.asyncio
async def test_v3_route_returns_none_on_low_confidence_with_single_pick():
    """Low confidence + only 1 pick → defer to V2 (no useful Top-3 to render)."""
    decision = DomainDecision(
        top_picks=[
            DomainHit(domain_id="vehicle_info", label="x", confidence=0.4),
        ],
    )
    engine = _make_engine(domain_decision=decision)
    result = await engine._v3_route("385912345678", "test", _make_identity())
    assert result is None  # fall through to V2


@pytest.mark.asyncio
async def test_v3_route_returns_none_on_stage1_error():
    decision = DomainDecision(error="json_parse_failed")
    engine = _make_engine(domain_decision=decision)
    result = await engine._v3_route("385912345678", "test", _make_identity())
    assert result is None


@pytest.mark.asyncio
async def test_v3_route_returns_clarify_question_when_stage2_asks():
    s1 = DomainDecision(
        top_picks=[DomainHit(domain_id="reservations", label="x", confidence=0.95)],
    )
    s2 = ScopedDecision(
        top_picks=[ScopedToolHit(tool_id="post_VehicleCalendar", method="POST", confidence=0.9)],
        is_mutating=True,
        needs_clarify=True,
        clarify_question="Za koji period trebaš auto?",
        missing_info=["dates"],
    )
    engine = _make_engine(domain_decision=s1, scoped_decision=s2)
    result = await engine._v3_route("385912345678", "trebam auto", _make_identity())
    assert result == "Za koji period trebaš auto?"


@pytest.mark.asyncio
async def test_v3_route_returns_none_when_stage2_low_confidence():
    """Stage-2 below 0.85 → fall through to V2 path."""
    s1 = DomainDecision(
        top_picks=[DomainHit(domain_id="vehicle_info", label="x", confidence=0.95)],
    )
    s2 = ScopedDecision(
        top_picks=[ScopedToolHit(tool_id="get_MasterData", method="GET", confidence=0.6)],
    )
    engine = _make_engine(domain_decision=s1, scoped_decision=s2)
    result = await engine._v3_route("385912345678", "test", _make_identity())
    assert result is None


@pytest.mark.asyncio
async def test_v3_route_dispatches_get_to_executor_directly():
    """High-confidence Stage-2 GET → V3 runs mutation_gate (AUTO for GET) +
    executor + formatter, returning final response WITHOUT V2 fall-through.
    """
    s1 = DomainDecision(
        top_picks=[DomainHit(domain_id="vehicle_info", label="x", confidence=0.95)],
    )
    s2 = ScopedDecision(
        top_picks=[ScopedToolHit(tool_id="get_MasterData", method="GET", confidence=0.95)],
    )

    # Stub executor + formatter
    class _Result:
        success = True
        circuit_open = False
        data = {"LastMileage": 30000, "RegistrationPlate": "DA053F"}
        error = None

    class _Exec:
        def method_of(self, tid):
            return "GET"
        async def execute(self, **kwargs):
            return _Result()

    engine = _make_engine(domain_decision=s1, scoped_decision=s2)
    engine.executor = _Exec()
    # Telemetry stays None — _log_telemetry is a no-op then.
    result = await engine._v3_route("385912345678", "kilometraža", _make_identity())
    # Now V3 returns formatted response directly — no None fall-through.
    assert result is not None
    assert isinstance(result, str)


@pytest.mark.asyncio
async def test_v3_route_falls_through_when_executor_fails():
    """Executor failure → V3 returns None so V2 path can retry."""
    s1 = DomainDecision(
        top_picks=[DomainHit(domain_id="vehicle_info", label="x", confidence=0.95)],
    )
    s2 = ScopedDecision(
        top_picks=[ScopedToolHit(tool_id="get_MasterData", method="GET", confidence=0.95)],
    )

    class _FailResult:
        success = False
        circuit_open = False
        data = None
        error = "http_500"

    class _Exec:
        def method_of(self, tid):
            return "GET"
        async def execute(self, **kwargs):
            return _FailResult()

    engine = _make_engine(domain_decision=s1, scoped_decision=s2)
    engine.executor = _Exec()
    result = await engine._v3_route("385912345678", "test", _make_identity())
    # Failure → None (let V2 try)
    assert result is None


@pytest.mark.asyncio
async def test_v3_route_runs_mutation_gate_for_post():
    """High-conf Stage-2 POST → mutation_gate triggers confirm dialog."""
    s1 = DomainDecision(
        top_picks=[DomainHit(domain_id="mileage_reports", label="x", confidence=0.95)],
    )
    s2 = ScopedDecision(
        top_picks=[ScopedToolHit(tool_id="post_AddMileage", method="POST", confidence=0.95)],
        is_mutating=True,
    )

    class _Exec:
        def method_of(self, tid):
            return "POST"
        async def execute(self, **kwargs):
            raise AssertionError("Executor should NOT be called for unconfirmed mutation")

    class _PendingStore:
        async def save(self, *args, **kwargs):
            self.saved = kwargs

    engine = _make_engine(domain_decision=s1, scoped_decision=s2)
    engine.executor = _Exec()
    engine.pending_mut_store = _PendingStore()
    result = await engine._v3_route("385912345678", "unesi 30000 km", _make_identity())
    # Should return confirm dialog text — never silently execute mutation
    assert result is not None
    assert isinstance(result, str)


@pytest.mark.asyncio
async def test_v3_route_engine_field_optional_default_none():
    """When engine is built without V3 pickers, .domain_picker is None.
    The process_message gate checks this and skips V3 path entirely."""
    engine = _make_engine()
    assert engine.domain_picker is None
    assert engine.scoped_picker is None
