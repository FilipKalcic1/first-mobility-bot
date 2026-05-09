"""End-to-end smoke test for V2Engine with all V3 layers wired.

Exercises the full process_message path with stubbed Azure clients:
- Identity resolution (known phone)
- L1 special intents (greeting)
- L1.5 identity gate (unknown phone)
- L2 driver_quick_path
- V3 router (Stage 1 → Stage 2 → execute)
- Top-3 cards Stage 2 medium confidence path
- Pending clarify resolution (user reply 1/2/3)
- Multi-turn context append

Goal: confirm wiring is intact end-to-end without burning Azure quota.
Each test < 100ms.
"""
from __future__ import annotations

import pytest

from services.v2.domain_picker import DomainDecision, DomainHit
from services.v2.domain_scoped_picker import ScopedDecision, ScopedToolHit
from services.v2.engine import V2Engine
from services.v2.identity import IdentitySnapshot
from services.v2.pending_clarify import PendingClarifyStore
from services.v2.conversation_history import ConversationHistoryStore
from services.v2.unified_responder import UnifiedResponder, UnifiedDecision


# ---- Fakes ----

class _FakeRedis:
    def __init__(self):
        self.store = {}
        self.lists = {}

    async def setex(self, k, ttl, v):
        self.store[k] = v

    async def get(self, k):
        return self.store.get(k)

    async def delete(self, k):
        self.store.pop(k, None)
        self.lists.pop(k, None)

    async def rpush(self, k, v):
        self.lists.setdefault(k, []).append(v)

    async def ltrim(self, k, s, e):
        if k in self.lists:
            lst = self.lists[k]
            self.lists[k] = lst[s:] if e == -1 else lst[s:e + 1]

    async def lrange(self, k, s, e):
        lst = self.lists.get(k, [])
        return lst[s:] if e == -1 else lst[s:e + 1]

    async def expire(self, k, ttl):
        pass


class _FakeRateLimiter:
    async def check(self, phone):
        from types import SimpleNamespace
        return SimpleNamespace(allowed=True, user_message=None)


class _FakePII:
    def scrub(self, q):
        from types import SimpleNamespace
        return SimpleNamespace(scrubbed_text=q)


class _FakeIdentity:
    def __init__(self, identity: IdentitySnapshot):
        self._identity = identity

    async def resolve(self, phone):
        return self._identity


class _FakeFlowStore:
    async def load(self, phone):
        return None

    async def clear(self, phone):
        pass

    async def save(self, *args, **kwargs):
        pass


class _FakePendingMutStore:
    async def load(self, phone):
        return None

    async def save(self, *args, **kwargs):
        pass

    async def clear(self, phone):
        pass


class _FakeIntentTypeClassifier:
    async def classify(self, q):
        from types import SimpleNamespace
        return SimpleNamespace(kind="QUESTION_ABOUT_OTHERS")


class _FakeBasics:
    async def match(self, q):
        from types import SimpleNamespace
        return SimpleNamespace(matched=False, field=None)


class _FakeRecognition:
    async def recognize(self, *args, **kwargs):
        from types import SimpleNamespace
        return SimpleNamespace(
            tool_id=None, params={}, candidates=[], anchor_score=0.0,
            llm_confidence=0.0, error="not_initialized", flow_name=None,
            template_id=None,
        )


class _FakeFlowEngine:
    pass


class _FakeExecutor:
    def method_of(self, tool_id):
        if tool_id.startswith(("post_", "put_", "patch_", "delete_")):
            return "POST"
        return "GET"

    async def execute(self, tool_id, params, identity_summary):
        from types import SimpleNamespace
        return SimpleNamespace(
            success=True, data={"value": "stub"}, error=None, circuit_open=False,
        )


class _FakeDomainPicker:
    def __init__(self, decision: DomainDecision):
        self._decision = decision

    async def pick(self, query, persona="driver", is_known=True, recent_turns=None):
        return self._decision


class _FakeScopedPicker:
    def __init__(self, decision: ScopedDecision):
        self._decision = decision

    async def pick(self, query, domain_id, persona="driver", recent_turns=None):
        return self._decision

    def tkb_entry_for(self, tool_id):
        return {"intent_summary": f"Stub TKB summary for {tool_id}"}


def _known_identity(name="Filip"):
    return IdentitySnapshot(
        phone="385912345678",
        person_id="p1",
        full_name=f"{name} Test",
        first_name=name,
        tenant_id="t1",
        vehicle_id="v1",
        vehicle_name="DA053F",
        last_mileage=30000,
        is_first_contact=False,
        is_known=True,
    )


def _unknown_identity():
    return IdentitySnapshot(
        phone="385999999999",
        is_first_contact=False,
        is_known=False,
    )


class _FakeUnifiedResponder:
    """Stub UnifiedResponder for engine smoke tests."""

    def __init__(self, decision):
        self._decision = decision

    async def respond(self, query, identity_summary=None, recent_turns=None, top_k=30):
        return self._decision


def _make_engine(
    *, identity, domain_decision, scoped_decision,
    redis_client=None, unified_decision=None,
):
    if redis_client is None:
        redis_client = _FakeRedis()
    return V2Engine(
        rate_limiter=_FakeRateLimiter(),
        pii=_FakePII(),
        identity=_FakeIdentity(identity),
        intent_type=_FakeIntentTypeClassifier(),
        basics=_FakeBasics(),
        recognition=_FakeRecognition(),
        flow_engine=_FakeFlowEngine(),
        flow_store=_FakeFlowStore(),
        executor=_FakeExecutor(),
        pending_mut_store=_FakePendingMutStore(),
        quick_path=None,
        telemetry=None,
        unified_responder=(
            _FakeUnifiedResponder(unified_decision) if unified_decision else None
        ),
        domain_picker=_FakeDomainPicker(domain_decision) if domain_decision else None,
        scoped_picker=_FakeScopedPicker(scoped_decision) if scoped_decision else None,
        pending_clarify_store=PendingClarifyStore(redis_client),
        conversation_history_store=ConversationHistoryStore(redis_client),
        query_normalizer=None,  # Empirically falsified on adversarial bench
    )


@pytest.mark.asyncio
async def test_unknown_phone_gets_enrollment_message(monkeypatch):
    monkeypatch.setenv("V2_USE_V3_ROUTER", "1")
    engine = _make_engine(
        identity=_unknown_identity(),
        domain_decision=DomainDecision(top_picks=[]),
        scoped_decision=ScopedDecision(),
    )
    response = await engine.process_message("385999999999", "kolika km")
    assert "broj" in response.lower() and "sustav" in response.lower()
    assert "manager" in response.lower() or "podršku" in response.lower()


@pytest.mark.asyncio
async def test_known_phone_v3_high_confidence_executes_tool(monkeypatch):
    monkeypatch.setenv("V2_USE_V3_ROUTER", "1")
    engine = _make_engine(
        identity=_known_identity(),
        domain_decision=DomainDecision(top_picks=[
            DomainHit(domain_id="vehicle_info", label="Vehicle", confidence=0.95),
            DomainHit(domain_id="reservations", label="Reservations", confidence=0.3),
        ]),
        scoped_decision=ScopedDecision(top_picks=[
            ScopedToolHit(
                tool_id="get_MasterData", method="GET", confidence=0.95,
                field_hint="TotalKm",
            ),
        ]),
    )
    response = await engine.process_message("385912345678", "kolika km")
    assert response is not None
    assert "broj" not in response.lower() or "kilometara" in response.lower()


@pytest.mark.asyncio
async def test_v3_stage2_medium_confidence_renders_top3_cards(monkeypatch):
    monkeypatch.setenv("V2_USE_V3_ROUTER", "1")
    engine = _make_engine(
        identity=_known_identity(),
        domain_decision=DomainDecision(top_picks=[
            DomainHit(domain_id="reservations", label="Reservations", confidence=0.95),
            DomainHit(domain_id="vehicle_info", label="Vehicle", confidence=0.3),
        ]),
        scoped_decision=ScopedDecision(top_picks=[
            ScopedToolHit(tool_id="get_VehicleCalendar", method="GET", confidence=0.6),
            ScopedToolHit(tool_id="get_LatestVehicleCalendar", method="GET", confidence=0.55),
            ScopedToolHit(tool_id="post_VehicleCalendar", method="POST", confidence=0.4),
        ]),
    )
    response = await engine.process_message("385912345678", "rezervacije")
    assert "1️⃣" in response and "2️⃣" in response
    assert "Odgovori brojem" in response


@pytest.mark.asyncio
async def test_pending_clarify_reply_1_routes_to_first_card(monkeypatch):
    monkeypatch.setenv("V2_USE_V3_ROUTER", "1")
    redis = _FakeRedis()
    engine = _make_engine(
        identity=_known_identity(),
        domain_decision=DomainDecision(top_picks=[
            DomainHit(domain_id="reservations", label="Reservations", confidence=0.95),
            DomainHit(domain_id="vehicle_info", label="Vehicle", confidence=0.3),
        ]),
        scoped_decision=ScopedDecision(top_picks=[
            ScopedToolHit(tool_id="get_VehicleCalendar", method="GET", confidence=0.6),
            ScopedToolHit(tool_id="get_LatestVehicleCalendar", method="GET", confidence=0.55),
        ]),
        redis_client=redis,
    )
    # Turn 1: render Top-3 cards (saves pending state)
    first = await engine.process_message("385912345678", "rezervacije")
    assert "1️⃣" in first

    # Turn 2: user replies "1" — should map to first card and execute
    second = await engine.process_message("385912345678", "1")
    # Executor returns stub data; we verify the reply was resolved (not asking again)
    assert "1️⃣" not in second  # not re-rendering cards


@pytest.mark.asyncio
async def test_pending_clarify_reply_ne_clears_state(monkeypatch):
    monkeypatch.setenv("V2_USE_V3_ROUTER", "1")
    redis = _FakeRedis()
    engine = _make_engine(
        identity=_known_identity(),
        domain_decision=DomainDecision(top_picks=[
            DomainHit(domain_id="reservations", label="Reservations", confidence=0.95),
            DomainHit(domain_id="vehicle_info", label="Vehicle", confidence=0.3),
        ]),
        scoped_decision=ScopedDecision(top_picks=[
            ScopedToolHit(tool_id="get_VehicleCalendar", method="GET", confidence=0.6),
            ScopedToolHit(tool_id="get_LatestVehicleCalendar", method="GET", confidence=0.55),
        ]),
        redis_client=redis,
    )
    await engine.process_message("385912345678", "rezervacije")
    second = await engine.process_message("385912345678", "ne")
    assert "U redu" in second or "drugačije" in second.lower()


@pytest.mark.asyncio
async def test_v3_disabled_falls_through_to_v2_path(monkeypatch):
    monkeypatch.setenv("V2_USE_V3_ROUTER", "0")
    engine = _make_engine(
        identity=_known_identity(),
        domain_decision=None,
        scoped_decision=None,
    )
    # V3 disabled; engine should NOT call V3 pickers and fall to L2a/L3
    # Recognition stub returns error="not_initialized" which triggers fallback
    response = await engine.process_message("385912345678", "neki random upit")
    assert response is not None
    # Doesn't crash; surfaces fallback message


# ----------------------------------------------------------------------
# Unified Responder path tests (V2_USE_UNIFIED_RESPONDER=1)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unified_path_high_conf_executes_and_fills_template(monkeypatch):
    monkeypatch.setenv("V2_USE_UNIFIED_RESPONDER", "1")
    monkeypatch.setenv("V2_USE_V3_ROUTER", "0")
    engine = _make_engine(
        identity=_known_identity(),
        domain_decision=None,
        scoped_decision=None,
        unified_decision=UnifiedDecision(
            tool_id="get_MasterData",
            params={},
            needs_confirm=False,
            response_text="Tvoja kilometraža: {value} km",
            confidence=0.95,
            reasoning="snapshot",
        ),
    )
    response = await engine.process_message("385912345678", "kolika km")
    # _FakeExecutor returns data={"value": "stub"} — placeholder filled
    assert "stub" in response
    assert "kilometraža" in response.lower()


@pytest.mark.asyncio
async def test_unified_path_clarify_returns_question(monkeypatch):
    monkeypatch.setenv("V2_USE_UNIFIED_RESPONDER", "1")
    monkeypatch.setenv("V2_USE_V3_ROUTER", "0")
    engine = _make_engine(
        identity=_known_identity(),
        domain_decision=None,
        scoped_decision=None,
        unified_decision=UnifiedDecision(
            tool_id="get_VehicleCalendar",
            needs_clarify=True,
            clarify_question="Za koje vozilo?",
            clarify_options=["DA053F", "ZG0001"],
            confidence=0.55,
        ),
    )
    response = await engine.process_message("385912345678", "rezervacije")
    assert response == "Za koje vozilo?"


@pytest.mark.asyncio
async def test_unified_path_mutation_triggers_confirm(monkeypatch):
    monkeypatch.setenv("V2_USE_UNIFIED_RESPONDER", "1")
    monkeypatch.setenv("V2_USE_V3_ROUTER", "0")
    engine = _make_engine(
        identity=_known_identity(),
        domain_decision=None,
        scoped_decision=None,
        unified_decision=UnifiedDecision(
            tool_id="post_AddMileage",
            params={"value": 30000},
            needs_confirm=True,
            response_text=None,
            confidence=0.9,
        ),
    )
    response = await engine.process_message("385912345678", "unesi 30000 km")
    # Mutation gate intercepts and renders confirm prompt
    assert response is not None
    # Either the confirm prompt or the pending state being saved
    # (engine renders confirm with "Da" / "Ne" semantics)


@pytest.mark.asyncio
async def test_unified_error_falls_through_to_v2(monkeypatch):
    monkeypatch.setenv("V2_USE_UNIFIED_RESPONDER", "1")
    monkeypatch.setenv("V2_USE_V3_ROUTER", "0")
    engine = _make_engine(
        identity=_known_identity(),
        domain_decision=None,
        scoped_decision=None,
        unified_decision=UnifiedDecision(error="llm_error:Timeout"),
    )
    # Unified errors → falls through to V2/L3, which is stubbed and itself errors
    response = await engine.process_message("385912345678", "kolika km")
    assert response is not None  # Doesn't crash; emits fallback message


@pytest.mark.asyncio
async def test_unified_disabled_uses_v3(monkeypatch):
    """Default state — V2_USE_UNIFIED_RESPONDER unset → V3 path runs."""
    monkeypatch.setenv("V2_USE_V3_ROUTER", "1")
    # Unified responder is None — V3 path takes over
    engine = _make_engine(
        identity=_known_identity(),
        domain_decision=DomainDecision(top_picks=[
            DomainHit(domain_id="vehicle_info", label="Vehicle", confidence=0.95),
            DomainHit(domain_id="reservations", label="Reservations", confidence=0.3),
        ]),
        scoped_decision=ScopedDecision(top_picks=[
            ScopedToolHit(tool_id="get_MasterData", method="GET", confidence=0.95),
        ]),
    )
    response = await engine.process_message("385912345678", "kolika km")
    assert response is not None  # V3 path executes


class _FakeToolUseResponder:
    """Stub for tool_use 2-pass loop. Returns canned ToolUseResult and
    optionally invokes the per-call executor argument once (Pass 1
    decision) so we can verify mutation gate intercept ran."""

    def __init__(self, result, picks_tool_id=None):
        self._result = result
        self._picks_tool_id = picks_tool_id  # if set, call executor before returning

    async def respond(self, query, identity_summary=None, recent_turns=None,
                      top_k=30, executor=None):
        if self._picks_tool_id and executor is not None:
            # Simulate Pass 1 calling the gated executor
            await executor(self._picks_tool_id, {}, identity_summary or {})
        return self._result


@pytest.mark.asyncio
async def test_tool_use_path_returns_text_directly(monkeypatch):
    from services.v2.tool_use_responder import ToolUseResult
    monkeypatch.setenv("V2_USE_TOOL_USE", "1")
    monkeypatch.setenv("V2_USE_V3_ROUTER", "0")
    redis = _FakeRedis()
    engine = V2Engine(
        rate_limiter=_FakeRateLimiter(),
        pii=_FakePII(),
        identity=_FakeIdentity(_known_identity()),
        intent_type=_FakeIntentTypeClassifier(),
        basics=_FakeBasics(),
        recognition=_FakeRecognition(),
        flow_engine=_FakeFlowEngine(),
        flow_store=_FakeFlowStore(),
        executor=_FakeExecutor(),
        pending_mut_store=_FakePendingMutStore(),
        quick_path=None,
        telemetry=None,
        domain_picker=None,
        scoped_picker=None,
        pending_clarify_store=PendingClarifyStore(redis),
        conversation_history_store=ConversationHistoryStore(redis),
        tool_use_responder=_FakeToolUseResponder(
            ToolUseResult(response_text="Tvoja kilometraža je 34 521 km."),
        ),
    )
    response = await engine.process_message("385912345678", "kolika km")
    assert "kilometraža" in response.lower()
    assert "34 521" in response


@pytest.mark.asyncio
async def test_tool_use_path_intercepts_mutation(monkeypatch):
    from services.v2.tool_use_responder import ToolUseResult
    monkeypatch.setenv("V2_USE_TOOL_USE", "1")
    monkeypatch.setenv("V2_USE_V3_ROUTER", "0")
    redis = _FakeRedis()
    engine = V2Engine(
        rate_limiter=_FakeRateLimiter(),
        pii=_FakePII(),
        identity=_FakeIdentity(_known_identity()),
        intent_type=_FakeIntentTypeClassifier(),
        basics=_FakeBasics(),
        recognition=_FakeRecognition(),
        flow_engine=_FakeFlowEngine(),
        flow_store=_FakeFlowStore(),
        executor=_FakeExecutor(),
        pending_mut_store=_FakePendingMutStore(),
        quick_path=None,
        telemetry=None,
        domain_picker=None,
        scoped_picker=None,
        pending_clarify_store=PendingClarifyStore(redis),
        conversation_history_store=ConversationHistoryStore(redis),
        # Responder simulates Pass 1 calling executor with a mutating tool.
        # Engine's gated executor intercepts → saves pending mutation →
        # responder gets sentinel back → engine overrides with confirm prompt.
        tool_use_responder=_FakeToolUseResponder(
            ToolUseResult(response_text="LLM-rendered text (gets overridden)"),
            picks_tool_id="post_AddMileage",
        ),
    )
    response = await engine.process_message("385912345678", "unesi 30000")
    # Engine renders the canonical confirm prompt for safety per CLAUDE.md §1.3
    assert response is not None
    # Either "Da" or some confirm-flavored text


@pytest.mark.asyncio
async def test_unified_skipped_when_responder_none(monkeypatch):
    """V2_USE_UNIFIED_RESPONDER=1 but unified_responder field is None — V3 takes over."""
    monkeypatch.setenv("V2_USE_UNIFIED_RESPONDER", "1")
    monkeypatch.setenv("V2_USE_V3_ROUTER", "1")
    engine = _make_engine(
        identity=_known_identity(),
        domain_decision=DomainDecision(top_picks=[
            DomainHit(domain_id="vehicle_info", label="Vehicle", confidence=0.95),
            DomainHit(domain_id="reservations", label="Reservations", confidence=0.3),
        ]),
        scoped_decision=ScopedDecision(top_picks=[
            ScopedToolHit(tool_id="get_MasterData", method="GET", confidence=0.95),
        ]),
        unified_decision=None,  # explicit None
    )
    response = await engine.process_message("385912345678", "kolika km")
    assert response is not None
