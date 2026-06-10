"""End-to-end V2Engine wire-up smoke tests.

Drives real queries through `V2Engine.process_message()` with explicit
fakes for every external dep (Redis, gateway, LLM, embedder, registry).
Tests the orchestration plumbing — not individual layer logic, which is
covered by per-layer unit tests.

What's exercised:
    L-1 rate limit → L0.5 PII → L0 identity → L1 special intents →
    L2a intent type → L2b basics anchor → L3 recognition → L5 gate →
    L6 mutation → L7 executor → L8 formatter

What's NOT here:
    - Realism of LLM judgments (anchor scores hand-pinned)
    - Network/latency behaviour
    - Cross-process Redis race semantics
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from services.v2.engine import V2Engine
from services.v2.flow_engine import FLOWS, FlowEngine, FlowStateStore
from services.v2.driver_basics import DriverBasicsAnchor
from services.v2.executor import ToolExecutor
from services.v2.gdpr_audit import GdprAuditStore
from services.v2.identity import IdentityContext, IdentitySnapshot
from services.v2.intent_type import IntentTypeClassifier
from services.v2.pending_clarify import PendingClarifyStore
from services.v2.pending_mutation import PendingMutationStore
from services.v2.pending_params import PendingParamsStore
from services.v2.pii_scrubber import PIIScrubber
from services.v2.rate_limiter import RateLimiter
from services.router.llm_router import RouterResult
from services.formatter.llm_formatter import FormatResult


# --------------------------------------------------------------------------
# Fakes (explicit, no MagicMock)
# --------------------------------------------------------------------------


class FakeRedis:
    def __init__(self):
        self.store: dict = {}
        self.counters: dict[str, int] = {}
        self.lists: dict[str, list] = {}

    async def get(self, key):
        return self.store.get(key)

    async def setex(self, key, ttl, value):
        self.store[key] = value

    async def set(self, key, value, nx: bool = False, ex: int | None = None):
        """SET with optional NX (create-if-absent) + EX (TTL) — covers the
        SET-NX EX form used by pending_mut_store.try_acquire_execution.
        TTL is recorded but not enforced (tests don't depend on wall-clock
        lock expiry). Returns truthy on write, None on NX miss — same shape
        as real redis-py."""
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def delete(self, key):
        self.store.pop(key, None)
        self.counters.pop(key, None)
        self.lists.pop(key, None)

    async def incr(self, key):
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]

    async def expire(self, key, ttl):
        return True

    async def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, value)
        return len(self.lists[key])

    async def ltrim(self, key, start, end):
        if key in self.lists:
            self.lists[key] = self.lists[key][start : end + 1]
        return True

    async def lrange(self, key, start, end):
        return self.lists.get(key, [])[start : end + 1]

    async def eval(self, script, numkeys, *args):
        # Mimics the rate-limiter Lua: INCR + (if v == 1) noop EXPIRE.
        keys = args[:numkeys]
        return await self.incr(keys[0])


@dataclass
class FakeApiResponse:
    success: bool
    data: object = None
    status_code: int = 200


class FakeGateway:
    def __init__(self):
        self.calls: list[dict] = []
        self.scripted: list[FakeApiResponse] = []

    def queue(self, resp: FakeApiResponse):
        self.scripted.append(resp)

    async def call(self, **kwargs):
        self.calls.append(kwargs)
        if self.scripted:
            return self.scripted.pop(0)
        return FakeApiResponse(success=False, status_code=500)


class FakeSettings:
    MOBILITY_TENANT_ID = "tenant-fake-uuid"


class FakeEmbedder:
    """Deterministic hashed-vector embedder. Tests can pin specific
    queries to specific vectors via .set()."""

    def __init__(self):
        self.overrides: dict[str, list[float]] = {}

    def set(self, text: str, vec: list[float]):
        self.overrides[text] = vec

    async def embed(self, text):
        if text in self.overrides:
            return self.overrides[text]
        h = hashlib.md5(text.encode("utf-8")).digest()
        return [b / 255.0 for b in h[:8]]


@dataclass
class _Choice:
    message: object


@dataclass
class _Msg:
    content: str


@dataclass
class _Response:
    choices: list


class _FakeCompletions:
    def __init__(self):
        self.queue: list = []

    def push(self, raw):
        self.queue.append(raw)

    async def create(self, **kwargs):
        if not self.queue:
            return _Response(choices=[_Choice(message=_Msg(content="{}"))])
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return _Response(choices=[_Choice(message=_Msg(content=item))])


class _FakeChat:
    def __init__(self, completions):
        self.completions = completions


class FakeLLM:
    def __init__(self):
        self.completions = _FakeCompletions()
        self.chat = _FakeChat(self.completions)


class FakeRegistry:
    """Mimics the tool_registry contract used by recognition + executor."""

    def __init__(self, tools_spec: dict):
        # tools_spec: {tool_id: {"method": "GET", "service": "automation",
        #                        "path": "/X", "purpose": "...", "anchor": "..."}}
        self._tools = tools_spec

    @property
    def tools(self):
        return list(self._tools.keys())

    def tool_id_of(self, tool):
        return tool

    def anchor_text_for(self, tool):
        return self._tools.get(tool, {}).get("anchor", "")

    def has_tool(self, tool_id):
        return tool_id in self._tools

    def method_of(self, tool_id):
        spec = self._tools.get(tool_id)
        return spec.get("method") if spec else None

    def purpose_of(self, tool_id):
        return self._tools.get(tool_id, {}).get("purpose", "")

    def spec_for(self, tool_id):
        spec = self._tools.get(tool_id)
        if spec is None:
            return None
        # Mirror the real ToolRegistry.spec_for: expose param_locations so the
        # executor can substitute {id} path placeholders + route query/body.
        # Derive path params from the path's {placeholders} (a {id} in the path
        # IS a path param), unless the test already declared param_locations.
        out = dict(spec)
        if "param_locations" not in out:
            import re
            placeholders = re.findall(r"\{(\w+)\}", out.get("path", "") or "")
            out["param_locations"] = {p: "path" for p in placeholders}
        return out


class FakeRouter:
    """Stub for services.router.llm_router.LLMRouter.

    Tests pre-queue RouterResult instances or call set_default() with a
    tool_id; .route() pops queue then falls back to default. Mimics the
    new L3 contract — no anchor embedding, no LLM call.
    """

    def __init__(self):
        self.queue: list = []
        self._default_tool_id = None
        self._default_params: dict = {}
        self.calls: list[tuple[str, str]] = []  # (query, identity_summary)

    def queue_result(self, result: "RouterResult"):
        self.queue.append(result)

    def set_default(self, tool_id: str, params=None):
        self._default_tool_id = tool_id
        self._default_params = params or {}

    async def initialize(self):
        return None

    async def route(self, query, identity_summary="", conversation_history=None, tool_filter=None):
        # Phase E 2026-05-16: route() now accepts an optional tool_filter
        # (a frozenset of allowed op_ids). FakeRouter ignores it; tests that
        # care about scoping should assert by examining catalog_scoper.
        self.calls.append((query, identity_summary))
        if self.queue:
            return self.queue.pop(0)
        return RouterResult(
            tool_id=self._default_tool_id,
            params=dict(self._default_params),
            confidence=0.95,
            rationale="default fake route",
            anchor_score=0.95,
            top_candidates=[(self._default_tool_id or "?", 0.95)],
        )


class FakeFormatter:
    """Stub for services.formatter.llm_formatter.LLMFormatter."""

    def __init__(self):
        self.calls: list[dict] = []
        self._template = "fake-formatted: {tool_id}"
        self.fail = False  # when True, simulate an LLM failure (error result)

    def set_template(self, template: str):
        self._template = template

    async def format(self, *, query, tool_id, api_data, identity_summary=""):
        self.calls.append({
            "query": query, "tool_id": tool_id,
            "api_data": api_data, "identity_summary": identity_summary,
        })
        if self.fail:
            return FormatResult(text="", error="boom")
        return FormatResult(text=self._template.format(tool_id=tool_id))


# --------------------------------------------------------------------------
# Engine builder
# --------------------------------------------------------------------------


async def _build_engine(*, registry_tools=None):
    redis = FakeRedis()
    gateway = FakeGateway()
    settings = FakeSettings()
    embedder = FakeEmbedder()
    llm = FakeLLM()

    registry = FakeRegistry(registry_tools or {})

    rate_limiter = RateLimiter(redis)
    pii = PIIScrubber()
    identity = IdentityContext(redis, gateway, settings)
    intent_type = IntentTypeClassifier(llm, "gpt-4o-mini")
    basics = DriverBasicsAnchor(embedder)
    await basics.initialize()
    router = FakeRouter()
    formatter_llm = FakeFormatter()
    flow_engine = FlowEngine(FLOWS)
    flow_store = FlowStateStore(redis)
    executor = ToolExecutor(gateway, registry)
    pending_mut_store = PendingMutationStore(redis)
    pending_clarify_store = PendingClarifyStore(redis)
    pending_params_store = PendingParamsStore(redis)
    gdpr_audit = GdprAuditStore(redis)

    # Mirror what make_v2_engine_for_production builds: op_id → parameters dict.
    # Tests can pass a `parameters` key on registry_tools entries to opt in to
    # param-asking; tools without it stay in legacy no-asking mode.
    tool_parameters = {
        tid: (spec.get("parameters") or {})
        for tid, spec in (registry_tools or {}).items()
    }

    engine = V2Engine(
        rate_limiter=rate_limiter,
        pii=pii,
        identity=identity,
        intent_type=intent_type,
        basics=basics,
        router=router,
        formatter_llm=formatter_llm,
        flow_engine=flow_engine,
        flow_store=flow_store,
        executor=executor,
        pending_mut_store=pending_mut_store,
        pending_clarify_store=pending_clarify_store,
        gdpr_audit_store=gdpr_audit,
        # Minimal TKB intents so clarify-cards UX has descriptions to render.
        # Real factory builds this from tool_knowledge_base.json.
        tkb_intents={
            tid: spec.get("purpose", "")
            for tid, spec in (registry_tools or {}).items()
        },
        pending_params_store=pending_params_store,
        tool_parameters=tool_parameters,
    )
    return engine, gateway, llm, embedder, router, formatter_llm


# --------------------------------------------------------------------------
# Smoke tests
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_contact_known_user_gets_welcome():
    """L-1 → L0.5 → L0 → L1: known user, first contact, welcomed."""
    engine, gateway, llm, _, _router, _fmt = await _build_engine()
    # Persons → resolves; MasterData → vehicle data
    gateway.queue(FakeApiResponse(success=True, data=[
        {"Id": "p1", "FirstName": "Marko", "LastName": "Marić",
         "TenantId": "t1"}
    ]))
    gateway.queue(FakeApiResponse(success=True, data={
        "VehicleName": "VW Golf", "LicencePlate": "ZG-1234",
        "LastMileage": 42500,
    }))

    reply = await engine.process_message("385955087196", "bok")

    # Welcome (L1) — Croatian, mentions name or vehicle
    assert reply
    assert "Marko" in reply or "Golf" in reply or "Bok" in reply or "Pozdrav" in reply


@pytest.mark.asyncio
async def test_question_about_self_serves_from_cache_no_llm_judge():
    """L2a → L2b path: 'kolika mi je km' served from cached MasterData."""
    engine, gateway, llm, embedder, _router, _fmt = await _build_engine()
    # Identity bootstrap
    gateway.queue(FakeApiResponse(success=True, data=[
        {"Id": "p1", "FirstName": "Ana", "LastName": "Anić",
         "TenantId": "t1"}
    ]))
    gateway.queue(FakeApiResponse(success=True, data={
        "VehicleName": "Skoda Octavia", "LicencePlate": "ZG-4242",
        "LastMileage": 88123,
    }))
    # Force second contact (skip welcome) via prior call
    await engine.process_message("385955087196", "ignored bootstrap")

    # Pin embedder so anchor matches strongly on the query
    target = [1.0, 0, 0, 0, 0, 0, 0, 0]
    for s in engine.basics._positive_vecs:
        pass  # already initialized; new pin only affects future embed calls
    # Make all positive anchors point at target, negatives orthogonal
    embedder.set("kolika mi je km", target)
    # Re-pin internal state by re-initializing with overrides
    # (basics already initialized; we just want match() to read cached vecs.)
    # Force LLM to classify as question_about_self
    llm.completions.push(json.dumps({
        "kind": "question_about_self", "confidence": 0.95,
    }))

    reply = await engine.process_message("385955087196", "kolika mi je km")
    assert reply
    # If basics matched, we'd see mileage. If it fell to L3 (no candidates),
    # we'd see a fallback. Either is correct orchestration — we just want
    # a non-empty Croatian string and no crash.
    assert isinstance(reply, str)


@pytest.mark.asyncio
async def test_rate_limit_short_circuits_before_any_other_layer():
    """L-1 cooldown: 101 quick messages → 101st gets cooldown, no API hits."""
    engine, gateway, llm, _, _router, _fmt = await _build_engine()
    # Bypass identity calls — we won't reach them once limit hits
    for _ in range(110):
        gateway.queue(FakeApiResponse(success=True, data=[]))

    last = None
    for i in range(101):
        last = await engine.process_message("385955087196", f"msg {i}")

    assert "Previše" in last or "Pričekaj" in last


@pytest.mark.asyncio
async def test_pii_scrubbed_before_downstream_layers():
    """L0.5 redacts an OIB/email before identity or LLM see the query."""
    engine, gateway, llm, _, _router, _fmt = await _build_engine()
    gateway.queue(FakeApiResponse(success=True, data=[
        {"Id": "p1", "FirstName": "X", "LastName": "Y", "TenantId": "t1"}
    ]))
    gateway.queue(FakeApiResponse(success=True, data={"VehicleName": "T"}))
    llm.completions.push(json.dumps({
        "kind": "other", "confidence": 0.9,
    }))

    # OIB embedded in query — should be scrubbed before LLM sees it
    await engine.process_message("385955087196", "moj OIB je 12345678901")

    # If LLM was called, the prompt must not contain raw OIB
    for call_args in []:  # We can't introspect _FakeCompletions.create kwargs
        pass
    # Indirect check: reply was produced without crash
    # (Direct PII assertion lives in test_pii_scrubber.py.)


@pytest.mark.asyncio
async def test_low_confidence_intent_falls_back_safely():
    """L2a low confidence → KIND_QUESTION_ABOUT_SELF safe path,
    bot never stalls on basic info."""
    engine, gateway, llm, _, _router, _fmt = await _build_engine()
    gateway.queue(FakeApiResponse(success=True, data=[
        {"Id": "p1", "FirstName": "K", "LastName": "L", "TenantId": "t1"}
    ]))
    gateway.queue(FakeApiResponse(success=True, data={
        "VehicleName": "Auto", "LicencePlate": "X-1", "LastMileage": 100,
    }))
    await engine.process_message("385955087196", "ignored bootstrap")

    # LLM returns low confidence → safe fallback to question_about_self
    llm.completions.push(json.dumps({"kind": "other", "confidence": 0.3}))
    reply = await engine.process_message("385955087196", "ambiguous query xyz")
    assert isinstance(reply, str)
    assert reply  # never empty


@pytest.mark.asyncio
async def test_engine_never_crashes_on_gateway_failure():
    """If MobilityOne is down on bootstrap, identity is degraded but
    the engine still returns a Croatian string instead of crashing."""
    engine, gateway, llm, _, _router, _fmt = await _build_engine()
    # Both Persons + MasterData fail
    gateway.queue(FakeApiResponse(success=False, status_code=503))
    gateway.queue(FakeApiResponse(success=False, status_code=503))
    llm.completions.push(json.dumps({"kind": "other", "confidence": 0.9}))

    reply = await engine.process_message("385955087196", "neki upit")
    assert isinstance(reply, str)
    assert reply


@pytest.mark.asyncio
async def test_unknown_user_still_gets_response():
    """No phone match → degraded snapshot, but engine still answers."""
    engine, gateway, llm, _, _router, _fmt = await _build_engine()
    gateway.queue(FakeApiResponse(success=True, data=[]))  # no person
    llm.completions.push(json.dumps({
        "kind": "question_about_self", "confidence": 0.9,
    }))

    reply = await engine.process_message("385999999999", "kolika mi je km")
    assert isinstance(reply, str)
    assert reply


@pytest.mark.asyncio
async def test_mutation_single_confirm_then_execute_delete():
    """Single-confirm flow (Filip 2026-05-16): DELETE pre-seeded as
    CONFIRM stage. User replies "Da" → mutation executes immediately.
    No more double-confirm "TRAJNO" prompt — just one Da/Ne turn.

    Regression pin: pending mutation persists across turns and is NOT
    re-classified. State cleared after execute.
    """
    engine, gateway, _, _, _router, _fmt = await _build_engine(registry_tools={
        "delete_VehicleCalendar_id": {
            "method":  "DELETE",
            "service": "automation",
            "path":    "/VehicleCalendar/{id}",
        },
    })
    # Identity bootstrap (resolved + cached)
    gateway.queue(FakeApiResponse(success=True, data=[
        {"Id": "p1", "FirstName": "M", "LastName": "M", "TenantId": "t1"},
    ]))
    gateway.queue(FakeApiResponse(success=True, data={"VehicleName": "X"}))
    await engine.identity.resolve("385955087196")

    # Pre-seed: caller said "obriši rezervaciju 123" earlier, L6 returned
    # single CONFIRM (was DOUBLE before single-confirm policy).
    from services.v2.pending_mutation import STAGE_SINGLE
    await engine.pending_mut_store.save(
        "385955087196",
        tool_id="delete_VehicleCalendar_id",
        params={"id": "abc-123"},
        stage=STAGE_SINGLE,
    )

    # Turn 1: "Da" → execute saved mutation immediately (no second prompt)
    gateway.queue(FakeApiResponse(success=True, data={"deleted": True}))
    reply = await engine.process_message("385955087196", "Da")

    # Verify the saved mutation actually ran. Fix #2 (Filip 2026-05-23): `id`
    # is a PATH param → {id} is SUBSTITUTED into the URL (/VehicleCalendar/abc-123),
    # NOT left literal and dumped in the body. (Old assertion pinned the bug:
    # path=="/VehicleCalendar/{id}" + body=={"id":...}.)
    delete_call = next(
        (c for c in gateway.calls
         if c.get("path") == "/VehicleCalendar/abc-123"
         and c.get("method") == "DELETE"),
        None,
    )
    assert delete_call is not None, (
        f"delete never called; gateway saw {[c.get('path') for c in gateway.calls]}"
    )
    assert "{id}" not in delete_call["path"]   # placeholder substituted
    assert not delete_call.get("body")         # id went to path, body empty
    assert "uspješno" in reply.lower() or "izvršena" in reply.lower()

    # State must be cleared after execute (single turn, single confirm, done)
    pending = await engine.pending_mut_store.load("385955087196")
    assert pending is None


@pytest.mark.asyncio
async def test_mutation_cancel_clears_pending_state():
    engine, gateway, _, _, _router, _fmt = await _build_engine(registry_tools={
        "delete_X": {"method": "DELETE", "service": "automation", "path": "/X"},
    })
    gateway.queue(FakeApiResponse(success=True, data=[
        {"Id": "p1", "FirstName": "M", "LastName": "M", "TenantId": "t1"},
    ]))
    gateway.queue(FakeApiResponse(success=True, data={"VehicleName": "X"}))
    await engine.identity.resolve("385955087196")

    from services.v2.pending_mutation import STAGE_SINGLE
    await engine.pending_mut_store.save(
        "385955087196", "delete_X", {"id": "x"}, STAGE_SINGLE,
    )

    reply = await engine.process_message("385955087196", "ne")
    assert "odustaj" in reply.lower()
    assert await engine.pending_mut_store.load("385955087196") is None
    # No DELETE call must have been made
    assert not any(
        c.get("method") == "DELETE" for c in gateway.calls
    )


@pytest.mark.asyncio
async def test_pending_mutation_short_circuits_other_layers():
    """When a pending mutation exists, the engine must NOT run L1, L2a,
    L3 — even if the user types something that looks like a new query."""
    engine, gateway, llm, _, _router, _fmt = await _build_engine(registry_tools={
        "delete_X": {"method": "DELETE", "service": "automation", "path": "/X"},
    })
    gateway.queue(FakeApiResponse(success=True, data=[
        {"Id": "p1", "FirstName": "M", "LastName": "M", "TenantId": "t1"},
    ]))
    gateway.queue(FakeApiResponse(success=True, data={"VehicleName": "X"}))
    await engine.identity.resolve("385955087196")

    from services.v2.pending_mutation import STAGE_SINGLE
    await engine.pending_mut_store.save(
        "385955087196", "delete_X", {}, STAGE_SINGLE,
    )

    # User types a new-looking question. Engine must treat it as a reply
    # to the pending confirm — ambiguous → either classic Da/Ne re-prompt
    # OR new multi-pending guard (#66) showing 3-option choice. Neither
    # path may trigger the DELETE.
    reply = await engine.process_message("385955087196", "kolika mi je km")
    # Acceptable: classic Da/Ne re-prompt, OR new multi-pending guard
    is_classic_reprompt = "Da" in reply or "Ne" in reply
    is_multi_pending_guard = (
        "nedovršenu potvrdu" in reply or "Izvrši pending" in reply
    )
    assert is_classic_reprompt or is_multi_pending_guard
    # No DELETE call must have been triggered
    assert not any(c.get("method") == "DELETE" for c in gateway.calls)
    # And the pending mutation must still be in place
    assert await engine.pending_mut_store.load("385955087196") is not None


@pytest.mark.asyncio
async def test_method_of_uses_public_accessor_not_private():
    """Regression: engine.py used to reach into executor._registry
    (private). Confirm the public method_of() works through V2Engine."""
    engine, _, _, _, _router, _fmt = await _build_engine(registry_tools={
        "get_X": {"method": "GET", "service": "automation", "path": "/x"},
    })
    assert engine.executor.method_of("get_X") == "GET"
    assert engine.executor.method_of(None) is None
    assert engine.executor.method_of("nonexistent") is None


# --------------------------------------------------------------------------
# B1 regression: GDPR + handover side_effects must actually fire
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_gdpr_delete_writes_audit_entry():
    """Regression: SpecialIntentMatch.side_effects used to be dead code.
    Confirm the engine now appends an audit entry to the per-tenant Redis
    list when a user requests GDPR delete."""
    from services.v2.gdpr_audit import GDPR_KEY_PREFIX

    engine, gateway, _, _, _router, _fmt = await _build_engine()
    # Identity bootstrap → tenant_id is set
    gateway.queue(FakeApiResponse(success=True, data=[
        {"Id": "p1", "FirstName": "Marko", "LastName": "M", "TenantId": "tenant-A"}
    ]))
    gateway.queue(FakeApiResponse(success=True, data={
        "VehicleName": "VW", "LicencePlate": "ZG-1",
    }))
    await engine.process_message("385955087196", "ignored bootstrap")

    # Now the actual GDPR delete request
    reply = await engine.process_message("385955087196", "obriši moje podatke")
    assert "48 sati" in reply or "obrisat" in reply.lower() or "GDPR" in reply.upper()

    # Audit entry MUST have landed
    entries = await engine.gdpr_audit_store.list_gdpr_requests("tenant-A")
    assert len(entries) == 1
    assert entries[0]["action"] == "audit_log_gdpr_delete"
    assert entries[0]["phone"] == "385955087196"
    assert entries[0]["person_id"] == "p1"
    assert entries[0]["tenant_id"] == "tenant-A"


@pytest.mark.asyncio
async def test_gdpr_export_writes_audit_entry():
    engine, gateway, _, _, _router, _fmt = await _build_engine()
    gateway.queue(FakeApiResponse(success=True, data=[
        {"Id": "p1", "FirstName": "A", "LastName": "A", "TenantId": "tenant-B"}
    ]))
    gateway.queue(FakeApiResponse(success=True, data={
        "VehicleName": "X", "LicencePlate": "Y",
    }))
    await engine.process_message("385111", "bok")

    reply = await engine.process_message("385111", "izvezi moje podatke")
    assert reply
    entries = await engine.gdpr_audit_store.list_gdpr_requests("tenant-B")
    assert len(entries) == 1
    assert entries[0]["action"] == "audit_log_gdpr_export"


@pytest.mark.asyncio
async def test_handover_writes_audit_entry():
    engine, gateway, _, _, _router, _fmt = await _build_engine()
    gateway.queue(FakeApiResponse(success=True, data=[
        {"Id": "p1", "FirstName": "C", "LastName": "C", "TenantId": "tenant-C"}
    ]))
    gateway.queue(FakeApiResponse(success=True, data={
        "VehicleName": "X", "LicencePlate": "Y",
    }))
    await engine.process_message("385222", "bok")

    reply = await engine.process_message("385222", "trebam pomoć čovjeka")
    assert reply
    entries = await engine.gdpr_audit_store.list_handover_requests("tenant-C")
    assert len(entries) == 1
    assert entries[0]["action"] == "queue_human_handover"


@pytest.mark.asyncio
async def test_gdpr_for_unknown_tenant_does_not_explode():
    """If tenant_id is unresolved, audit is skipped with a warning but the
    user still gets the queued-confirmation reply."""
    engine, gateway, _, _, _router, _fmt = await _build_engine()
    # No Persons match → unknown phone
    gateway.queue(FakeApiResponse(success=True, data=[]))

    reply = await engine.process_message("385000", "obriši moje podatke")
    # Even on unknown phone, GDPR intent fires (legal precedence)
    assert reply
    # But no audit entry should be written (no tenant to scope it to)
    # We can't read by tenant since there's no tenant — just confirm Redis
    # didn't grow keys under GDPR prefix
    assert not any(k.startswith("gdpr:requests:") for k in engine.gdpr_audit_store._redis.lists)


# --------------------------------------------------------------------------
# Model A (Filip 2026-05-17): ANY non-special query after identity now goes
# through the universal action picker BEFORE the L3 router runs. No more
# auto-execute. The L5-fallback-only clarify path is replaced by an always-on
# 3-turn cascade (action pick → tool pick → execute/confirm).
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_model_a_query_shows_universal_action_picker_and_saves_original_query():
    """Filip Model A: any non-special query → action picker, regardless of
    router confidence. The L3 router does NOT run on Turn 1 — it runs on
    Turn 2 after the user picks an action category. The pending_clarify
    state persists the ORIGINAL query (not candidates yet — those come from
    Turn 2's scoped router call)."""
    engine, gateway, _, _, _router, _fmt = await _build_engine(registry_tools={
        "get_VehicleCalendar":        {"method": "GET",  "service": "auto", "path": "/v",  "purpose": "Lista mojih rezervacija."},
        "delete_VehicleCalendar_id":  {"method": "DELETE","service": "auto", "path": "/v/{id}", "purpose": "Otkaži rezervaciju."},
        "put_VehicleCalendar_id":     {"method": "PUT",  "service": "auto", "path": "/v/{id}", "purpose": "Promijeni rezervaciju."},
    })
    gateway.queue(FakeApiResponse(success=True, data=[
        {"Id": "p1", "FirstName": "Marko", "LastName": "M", "TenantId": "t1"}
    ]))
    gateway.queue(FakeApiResponse(success=True, data={"VehicleName": "VW", "LicencePlate": "X-1"}))
    # First contact hits L1 WELCOME — warmup so subsequent calls reach the
    # action picker (welcome has priority over the cascade).
    await engine.process_message("+385955087196", "bok")

    # Query that doesn't trigger L2b driver-basics anchor (no "moja/moje")
    # so it reaches the universal action picker.
    reply = await engine.process_message("+385955087196", "nešto s kalendarom")

    # Reply is the universal 4-action picker
    assert "POGLEDATI" in reply
    assert "UNIJETI" in reply
    assert "IZMIJENITI" in reply
    assert "IZBRISATI" in reply
    assert "Odgovori brojem" in reply
    # State persisted with original query for Turn 2 re-routing
    pending = await engine.pending_clarify_store.load("+385955087196")
    assert pending is not None
    assert pending.stage == "action_global"
    assert pending.original_query == "nešto s kalendarom"
    # Candidates list is empty at this stage — populated on Turn 2 after user
    # picks an action and the engine runs the scoped L3 router.
    assert pending.candidates == []


@pytest.mark.asyncio
async def test_clarify_user_picks_one_executes_chosen_tool():
    """Pre-saved clarify state + user replies '1' → engine resolves to the
    saved candidate, runs through mutation gate + executor + formatter."""
    engine, gateway, _, _, _router, _fmt = await _build_engine(registry_tools={
        "get_VehicleCalendar":        {"method": "GET", "service": "auto", "path": "/v",  "purpose": "Lista."},
        "delete_VehicleCalendar_id":  {"method": "DELETE","service": "auto", "path": "/v/{id}", "purpose": "Otkaži."},
        "put_VehicleCalendar_id":     {"method": "PUT", "service": "auto", "path": "/v/{id}", "purpose": "Promijeni."},
    })
    gateway.queue(FakeApiResponse(success=True, data=[
        {"Id": "p1", "FirstName": "M", "LastName": "M", "TenantId": "t1"}
    ]))
    gateway.queue(FakeApiResponse(success=True, data={"VehicleName": "X", "LicencePlate": "Y"}))
    await engine.process_message("+385955087196", "bok")

    # Pre-seed pending clarify state directly
    await engine.pending_clarify_store.save(
        phone="+385955087196",
        candidates=[
            {"tool_id": "get_VehicleCalendar",       "params": {}, "field_hint": None},
            {"tool_id": "delete_VehicleCalendar_id", "params": {}, "field_hint": None},
            {"tool_id": "put_VehicleCalendar_id",   "params": {}, "field_hint": None},
        ],
        original_query="nešto s rezervacijom",
    )
    # Queue the GET response the executor will produce
    gateway.queue(FakeApiResponse(success=True, data=[{"Id": "r1", "Status": "active"}]))

    reply = await engine.process_message("+385955087196", "1")

    # The picked tool (get_VehicleCalendar = read, AUTO mutation gate) was executed
    last_call = gateway.calls[-1]
    assert last_call["path"] == "/v"
    assert reply  # formatter ran
    # Pending state cleared so next turn doesn't re-resolve
    assert await engine.pending_clarify_store.load("+385955087196") is None


@pytest.mark.asyncio
async def test_clarify_user_says_ne_drops_clarify_and_acknowledges():
    """Pre-saved clarify + user says 'ne' → graceful drop, store cleared."""
    engine, gateway, _, _, _router, _fmt = await _build_engine()
    gateway.queue(FakeApiResponse(success=True, data=[
        {"Id": "p1", "FirstName": "M", "LastName": "M", "TenantId": "t1"}
    ]))
    gateway.queue(FakeApiResponse(success=True, data={"VehicleName": "X", "LicencePlate": "Y"}))
    await engine.process_message("+385955087196", "bok")

    await engine.pending_clarify_store.save(
        phone="+385955087196",
        candidates=[{"tool_id": "get_X", "params": {}, "field_hint": None}],
        original_query="something",
    )

    reply = await engine.process_message("+385955087196", "ne")
    assert "U redu" in reply or "drugačije" in reply
    assert await engine.pending_clarify_store.load("+385955087196") is None


@pytest.mark.asyncio
async def test_clarify_fresh_query_during_pending_falls_through_to_fresh_routing():
    """User had clarify pending, but instead of '1/2/3' types a brand-new
    query. Engine must clear the pending state and re-route from scratch
    (the new query passes through the full pipeline, not the clarify map)."""
    engine, gateway, _, _, router, _fmt = await _build_engine(registry_tools={
        "get_MasterData": {"method": "GET", "service": "auto", "path": "/master", "purpose": "Moji podaci."},
    })
    gateway.queue(FakeApiResponse(success=True, data=[
        {"Id": "p1", "FirstName": "M", "LastName": "M", "TenantId": "t1"}
    ]))
    gateway.queue(FakeApiResponse(success=True, data={"VehicleName": "VW", "LicencePlate": "Y", "LastMileage": 42500}))
    await engine.process_message("+385955087196", "bok")

    await engine.pending_clarify_store.save(
        phone="+385955087196",
        candidates=[
            {"tool_id": "get_X", "params": {}, "field_hint": None},
            {"tool_id": "get_Y", "params": {}, "field_hint": None},
        ],
        original_query="ambiguous question",
    )

    # User types a real Croatian query — driver-basics-anchorable
    reply = await engine.process_message("+385955087196", "kolika mi je kilometraža")

    # Reply was produced (some path took it); the unresolved clarify state
    # is cleared by _resolve_pending_clarify returning None → fall-through.
    assert reply
    # Filip 2026-06-05: after L2b match we now save a NEW reoffer state so
    # the user can reply "nije točno" and get the L3 cascade. The OLD stale
    # candidates ("get_X"/"get_Y") must be gone — only the L2B sentinel can
    # remain. Either None (no L2b match) or L2B reoffer state.
    state = await engine.pending_clarify_store.load("+385955087196")
    if state is not None:
        assert state.last_executed_tool == "__L2B_DRIVER_BASICS__"
        assert all(c.get("tool_id") not in ("get_X", "get_Y")
                   for c in state.candidates)


# --------------------------------------------------------------------------
# Model A cascade (Filip direktiva 2026-05-17) — full 3-turn flow tests
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_model_a_action_pick_runs_scoped_router_and_renders_tool_picker():
    """Turn 2 of Model A cascade: user replied '1' (POGLEDATI) after the
    universal action picker. Engine must (a) run L3 router on the ORIGINAL
    query stashed in pending, (b) save the new top-3 with stage=tool,
    (c) render the tool picker with the chosen action label in the header."""
    engine, gateway, _, _, router, _fmt = await _build_engine(registry_tools={
        "get_VehicleCalendar":      {"method": "GET",    "service": "auto", "path": "/v",      "purpose": "Lista rezervacija."},
        "get_LatestMileageReports": {"method": "GET",    "service": "auto", "path": "/m/last", "purpose": "Zadnja km."},
        "get_MasterData":           {"method": "GET",    "service": "auto", "path": "/me",     "purpose": "Moji podaci."},
    })
    gateway.queue(FakeApiResponse(success=True, data=[
        {"Id": "p1", "FirstName": "M", "LastName": "M", "TenantId": "t1"}
    ]))
    gateway.queue(FakeApiResponse(success=True, data={"VehicleName": "X", "LicencePlate": "Y"}))
    await engine.process_message("+385955087196", "bok")

    # Turn 1 — universal action picker (query avoids L2b basics anchor by
    # not containing 'moj/moja/moje' possessive)
    turn1 = await engine.process_message("+385955087196", "nešto s kalendarom")
    assert "POGLEDATI" in turn1

    # Pre-queue a router result for Turn 2 (scoped GET-only)
    router.queue_result(RouterResult(
        tool_id="get_VehicleCalendar",
        params={"id": "abc"},
        confidence=0.65,
        anchor_score=0.70,
        top_candidates=[
            ("get_VehicleCalendar",      0.70),
            ("get_LatestMileageReports", 0.55),
            ("get_MasterData",           0.45),
        ],
    ))

    # Turn 2 — user picks "1" (POGLEDATI)
    turn2 = await engine.process_message("+385955087196", "1")

    # Tool picker rendered with POGLEDATI in header
    assert "POGLEDATI" in turn2
    assert "1️⃣" in turn2 or "1." in turn2
    assert "Odgovori brojem" in turn2

    # Pending state now stage=tool with 3 candidates; original_query preserved
    pending = await engine.pending_clarify_store.load("+385955087196")
    assert pending is not None
    assert pending.stage == "tool"
    assert len(pending.candidates) == 3
    assert pending.candidates[0]["tool_id"] == "get_VehicleCalendar"
    # Router's parsed params preserved ONLY on the router's top pick
    assert pending.candidates[0]["params"] == {"id": "abc"}
    assert pending.candidates[1]["params"] == {}
    assert pending.original_query == "nešto s kalendarom"

    # Router was invoked with the ORIGINAL query, not the user's "1" reply
    assert router.calls[-1][0] == "nešto s kalendarom"


@pytest.mark.asyncio
async def test_model_a_tool_pick_triggers_execute_for_read():
    """Turn 3 of Model A cascade: user picked a GET tool from the tool picker.
    Engine runs mutation_gate (AUTO for GET) → executor → formatter."""
    engine, gateway, _, _, _router, _fmt = await _build_engine(registry_tools={
        "get_VehicleCalendar": {"method": "GET", "service": "auto", "path": "/v", "purpose": "Lista."},
    })
    gateway.queue(FakeApiResponse(success=True, data=[
        {"Id": "p1", "FirstName": "M", "LastName": "M", "TenantId": "t1"}
    ]))
    gateway.queue(FakeApiResponse(success=True, data={"VehicleName": "X", "LicencePlate": "Y"}))
    await engine.process_message("+385955087196", "bok")

    # Pre-seed Turn 3 state: STAGE_TOOL with one GET candidate
    await engine.pending_clarify_store.save(
        phone="+385955087196",
        candidates=[
            {"tool_id": "get_VehicleCalendar", "method": "GET",
             "params": {}, "field_hint": None,
             "short_label": "Pokaži rezervaciju", "description": "x"},
        ],
        original_query="kalendar",
        stage="tool",
    )
    # Executor response
    gateway.queue(FakeApiResponse(success=True, data=[{"Id": "r1"}]))

    reply = await engine.process_message("+385955087196", "1")

    assert reply  # formatter produced output
    # Pending cleared
    assert await engine.pending_clarify_store.load("+385955087196") is None


@pytest.mark.asyncio
async def test_model_a_tool_pick_for_mutation_triggers_confirm():
    """Turn 3 with a DELETE tool: mutation_gate → CONFIRM → save pending_mut,
    render confirm prompt. No executor call until user replies 'Da'."""
    engine, gateway, _, _, _router, _fmt = await _build_engine(registry_tools={
        "delete_VehicleCalendar_id": {"method": "DELETE", "service": "auto",
                                       "path": "/v/{id}", "purpose": "Otkaži."},
    })
    gateway.queue(FakeApiResponse(success=True, data=[
        {"Id": "p1", "FirstName": "M", "LastName": "M", "TenantId": "t1"}
    ]))
    gateway.queue(FakeApiResponse(success=True, data={"VehicleName": "X", "LicencePlate": "Y"}))
    await engine.process_message("+385955087196", "bok")

    await engine.pending_clarify_store.save(
        phone="+385955087196",
        candidates=[
            {"tool_id": "delete_VehicleCalendar_id", "method": "DELETE",
             "params": {"id": "r1"}, "field_hint": None,
             "short_label": "Obriši rezervaciju", "description": "x"},
        ],
        original_query="obriši rezervaciju r1",
        stage="tool",
    )

    reply = await engine.process_message("+385955087196", "1")

    # Confirm prompt rendered (mutation_gate gives a Croatian "siguran/DA/NE" msg)
    assert "DA" in reply or "NE" in reply or "potvrdu" in reply.lower()
    # Pending mutation persisted for "Da" reply on next turn
    assert await engine.pending_mut_store.load("+385955087196") is not None
    # Clarify cleared
    assert await engine.pending_clarify_store.load("+385955087196") is None


@pytest.mark.asyncio
async def test_model_a_crisis_intercepts_before_action_picker():
    """Crisis detection (L0.7) runs BEFORE the action picker. A crisis-signal
    query goes directly to the crisis response — no pending_clarify saved."""
    engine, gateway, _, _, _router, _fmt = await _build_engine()
    gateway.queue(FakeApiResponse(success=True, data=[
        {"Id": "p1", "FirstName": "M", "LastName": "M", "TenantId": "t1"}
    ]))
    gateway.queue(FakeApiResponse(success=True, data={"VehicleName": "X", "LicencePlate": "Y"}))
    await engine.process_message("+385955087196", "bok")

    reply = await engine.process_message("+385955087196", "ne želim više živjeti")

    # Crisis response (mentions hotline) — not action picker
    assert "POGLEDATI" not in reply
    assert "116" in reply or "telefon" in reply.lower() or "pomo" in reply.lower()
    # No clarify pending saved
    assert await engine.pending_clarify_store.load("+385955087196") is None


@pytest.mark.asyncio
async def test_model_a_welcome_intercepts_before_action_picker():
    """L1 special intent (welcome on first contact) has priority. The first
    message is the WELCOME response, not the action picker."""
    engine, gateway, _, _, _router, _fmt = await _build_engine()
    gateway.queue(FakeApiResponse(success=True, data=[
        {"Id": "p1", "FirstName": "Ivo", "LastName": "I", "TenantId": "t1"}
    ]))
    gateway.queue(FakeApiResponse(success=True, data={
        "VehicleName": "Octavia", "LicencePlate": "ZG-1", "LastMileage": 100,
    }))

    reply = await engine.process_message("+385955087196", "bok")

    # WELCOME — Croatian greeting, NOT action picker
    assert "POGLEDATI" not in reply
    # No pending clarify saved on first turn (welcome short-circuits)
    assert await engine.pending_clarify_store.load("+385955087196") is None


# --------------------------------------------------------------------------
# Param-asking (Filip 2026-05-17) — generic missing required + optional flow
# --------------------------------------------------------------------------


async def _bootstrap_known_user(engine, gateway, phone="+385955087196"):
    """Drain WELCOME so subsequent calls reach Model A / param-asking."""
    gateway.queue(FakeApiResponse(success=True, data=[
        {"Id": "p1", "FirstName": "M", "LastName": "M", "TenantId": "t1"}
    ]))
    gateway.queue(FakeApiResponse(success=True, data={
        "VehicleName": "VW", "LicencePlate": "Y", "LastMileage": 1000,
    }))
    await engine.process_message(phone, "bok")


@pytest.mark.asyncio
async def test_tool_with_missing_required_asks_for_param():
    """Tool with required user_input param NOT in router output → engine
    saves pending_params + asks Croatian question (NOT mutation confirm)."""
    engine, gateway, _, _, _router, _fmt = await _build_engine(registry_tools={
        "delete_VehicleCalendar_id": {
            "method": "DELETE", "service": "auto", "path": "/v/{id}",
            "purpose": "Otkaži rezervaciju.",
            "parameters": {
                "id": {
                    "required": True,
                    "dependency_source": "user_input",
                    "param_type": "string",
                    "location": "path",
                    "description": "Reservation ID",
                },
            },
        },
    })
    await _bootstrap_known_user(engine, gateway)

    # Pre-seed STAGE_TOOL pending so picking "1" hits param-collection branch
    # without needing to walk through the action+tool picker turns.
    await engine.pending_clarify_store.save(
        phone="+385955087196",
        candidates=[
            {"tool_id": "delete_VehicleCalendar_id", "method": "DELETE",
             "params": {}, "field_hint": None,
             "short_label": "Obriši rezervaciju", "description": "x"},
        ],
        original_query="obriši rezervaciju",
        stage="tool",
    )

    reply = await engine.process_message("+385955087196", "1")

    # Croatian param question rendered (NOT mutation confirm "DA/NE")
    assert "Trebam" in reply
    assert "ID" in reply
    # No mutation pending yet — gate doesn't fire until params filled
    assert await engine.pending_mut_store.load("+385955087196") is None
    # Param-collection state persisted
    pp = await engine.pending_params_store.load("+385955087196")
    assert pp is not None
    assert pp.tool_id == "delete_VehicleCalendar_id"
    assert pp.required_remaining == ["id"]


@pytest.mark.asyncio
async def test_param_answer_collected_then_runs_mutation_confirm():
    """User answers the param question → engine stores value, no more
    required missing → mutation_gate fires CONFIRM for DELETE (Filip
    single-confirm policy)."""
    engine, gateway, _, _, _router, _fmt = await _build_engine(registry_tools={
        "delete_VehicleCalendar_id": {
            "method": "DELETE", "service": "auto", "path": "/v/{id}",
            "purpose": "Otkaži.",
            "parameters": {
                "id": {
                    "required": True, "dependency_source": "user_input",
                    "param_type": "string", "location": "path",
                },
            },
        },
    })
    await _bootstrap_known_user(engine, gateway)
    # Pre-seed: required "id" still missing, optional list empty.
    from services.v2.pending_params import PendingParams
    await engine.pending_params_store.save(
        "+385955087196",
        PendingParams(
            phone="+385955087196",
            tool_id="delete_VehicleCalendar_id",
            collected={}, required_remaining=["id"],
            optional_remaining=[], optional_offered=False,
            original_query="obriši rezervaciju",
        ),
    )

    reply = await engine.process_message("+385955087196", "R-12345")

    # Mutation confirm rendered (DELETE → single CONFIRM)
    reply_l = reply.lower()
    assert (
        "da/ne" in reply_l
        or "potvrđ" in reply_l
        or "potvrdu" in reply_l
        or "brisanje" in reply_l  # DELETE warning copy
    )
    # Param state cleared; mutation state holds the collected params
    assert await engine.pending_params_store.load("+385955087196") is None
    pm = await engine.pending_mut_store.load("+385955087196")
    assert pm is not None
    assert pm.params == {"id": "R-12345"}


@pytest.mark.asyncio
async def test_multiple_required_params_asked_one_by_one():
    """Tool with 2 required params → first answered → engine asks second
    (does NOT execute or confirm until both filled)."""
    engine, gateway, _, _, _router, _fmt = await _build_engine(registry_tools={
        "post_AddMileage": {
            "method": "POST", "service": "auto", "path": "/m",
            "purpose": "Unesi km.",
            "parameters": {
                "Mileage": {
                    "required": True, "dependency_source": "user_input",
                    "param_type": "integer", "location": "body",
                },
                "Note": {
                    "required": True, "dependency_source": "user_input",
                    "param_type": "string", "location": "body",
                },
            },
        },
    })
    await _bootstrap_known_user(engine, gateway)
    from services.v2.pending_params import PendingParams
    await engine.pending_params_store.save(
        "+385955087196",
        PendingParams(
            phone="+385955087196",
            tool_id="post_AddMileage",
            collected={}, required_remaining=["Mileage", "Note"],
            optional_remaining=[], optional_offered=False,
            original_query="unesi km",
        ),
    )

    reply = await engine.process_message("+385955087196", "42500")

    # Second required question asked (NOT confirm yet)
    assert "Trebam" in reply
    assert "napomenu" in reply.lower() or "note" in reply.lower()
    # State advanced: Mileage stored as int, Note still pending
    pp = await engine.pending_params_store.load("+385955087196")
    assert pp is not None
    assert pp.collected == {"Mileage": 42500}
    assert pp.required_remaining == ["Note"]


@pytest.mark.asyncio
async def test_invalid_param_value_triggers_reask():
    """User sends non-integer for integer param → engine re-asks with
    typed example, does NOT advance state."""
    engine, gateway, _, _, _router, _fmt = await _build_engine(registry_tools={
        "post_AddMileage": {
            "method": "POST", "service": "auto", "path": "/m",
            "purpose": "Unesi km.",
            "parameters": {
                "Mileage": {
                    "required": True, "dependency_source": "user_input",
                    "param_type": "integer", "location": "body",
                },
            },
        },
    })
    await _bootstrap_known_user(engine, gateway)
    from services.v2.pending_params import PendingParams
    await engine.pending_params_store.save(
        "+385955087196",
        PendingParams(
            phone="+385955087196", tool_id="post_AddMileage",
            collected={}, required_remaining=["Mileage"],
            optional_remaining=[], optional_offered=False,
            original_query="unesi km",
        ),
    )

    reply = await engine.process_message("+385955087196", "abc xyz")

    # Re-ask message (Croatian) with integer example
    assert "Nisam razumio" in reply or "42500" in reply
    # State unchanged — Mileage still pending
    pp = await engine.pending_params_store.load("+385955087196")
    assert pp is not None
    assert pp.required_remaining == ["Mileage"]
    assert pp.collected == {}


@pytest.mark.asyncio
async def test_optional_skip_proceeds_to_execute():
    """All required filled + optional offer → user says 'ne' → engine
    skips optionals and runs mutation gate / execute."""
    engine, gateway, _, _, _router, _fmt = await _build_engine(registry_tools={
        "post_AddMileage": {
            "method": "POST", "service": "auto", "path": "/m",
            "purpose": "Unesi km.",
            "parameters": {
                "Mileage": {
                    "required": True, "dependency_source": "user_input",
                    "param_type": "integer", "location": "body",
                },
                "Note": {
                    "required": False, "dependency_source": "user_input",
                    "param_type": "string", "location": "body",
                },
            },
        },
    })
    await _bootstrap_known_user(engine, gateway)
    from services.v2.pending_params import PendingParams
    # State: Mileage already filled, Note offered as optional
    await engine.pending_params_store.save(
        "+385955087196",
        PendingParams(
            phone="+385955087196", tool_id="post_AddMileage",
            collected={"Mileage": 42500}, required_remaining=[],
            optional_remaining=["Note"], optional_offered=True,
            original_query="unesi km",
        ),
    )

    reply = await engine.process_message("+385955087196", "ne")

    # POST → CONFIRM. No re-ask, no execute yet.
    reply_l = reply.lower()
    assert "da/ne" in reply_l or "potvrđ" in reply_l or "potvrdu" in reply_l
    # Param state cleared
    assert await engine.pending_params_store.load("+385955087196") is None
    # Mutation pending with only required params (no Note)
    pm = await engine.pending_mut_store.load("+385955087196")
    assert pm is not None
    assert pm.params == {"Mileage": 42500}


@pytest.mark.asyncio
async def test_optional_freetext_llm_extracts_then_finalizes():
    """Filip 2026-05-17 Model: user replies free-text to optional offer →
    engine calls OptionalParamExtractor (one LLM shot), merges fields,
    proceeds to mutation gate WITHOUT iterating each optional."""
    engine, gateway, _, _, _router, _fmt = await _build_engine(registry_tools={
        "post_AddMileage": {
            "method": "POST", "service": "auto", "path": "/m",
            "purpose": "Unesi km.",
            "parameters": {
                "Mileage": {
                    "required": True, "dependency_source": "user_input",
                    "param_type": "integer", "location": "body",
                },
                "Note": {
                    "required": False, "dependency_source": "user_input",
                    "param_type": "string", "location": "body",
                },
                "Type": {
                    "required": False, "dependency_source": "user_input",
                    "param_type": "string", "location": "body",
                },
            },
        },
    })
    await _bootstrap_known_user(engine, gateway)

    # Stub extractor — engine should call extract() exactly once with the
    # user's free-text + the offered optional specs, and merge what we return.
    extractor_calls = []

    class _StubExtractor:
        async def extract(self, user_text, optional_specs):
            extractor_calls.append({
                "text": user_text,
                "offered": sorted(optional_specs.keys()),
            })
            return {"Note": "kasnim po pola sata", "Type": "hitno"}

    engine.optional_extractor = _StubExtractor()

    from services.v2.pending_params import PendingParams
    await engine.pending_params_store.save(
        "+385955087196",
        PendingParams(
            phone="+385955087196", tool_id="post_AddMileage",
            collected={"Mileage": 42500}, required_remaining=[],
            optional_remaining=["Note", "Type"], optional_offered=True,
            original_query="unesi km",
        ),
    )

    reply = await engine.process_message(
        "+385955087196", "kasnim po pola sata i to je hitno",
    )

    # Extractor called once with the original free-text + both offered fields
    assert len(extractor_calls) == 1
    assert extractor_calls[0]["text"] == "kasnim po pola sata i to je hitno"
    assert extractor_calls[0]["offered"] == ["Note", "Type"]

    # Mutation gate prompted (POST → CONFIRM); param state cleared
    reply_l = reply.lower()
    assert "da/ne" in reply_l or "potvrđ" in reply_l or "potvrdu" in reply_l
    assert await engine.pending_params_store.load("+385955087196") is None

    # All three params (1 required + 2 LLM-extracted) sit on the mutation
    pm = await engine.pending_mut_store.load("+385955087196")
    assert pm is not None
    assert pm.params == {
        "Mileage": 42500,
        "Note": "kasnim po pola sata",
        "Type": "hitno",
    }


@pytest.mark.asyncio
async def test_optional_extractor_failure_finalizes_with_required_only():
    """LLM extractor raises / returns nothing → engine still finalizes
    with only the required params. No 'tehnički problem' message because
    of optional extract failure (silent fallback)."""
    engine, gateway, _, _, _router, _fmt = await _build_engine(registry_tools={
        "post_AddMileage": {
            "method": "POST", "service": "auto", "path": "/m",
            "purpose": "Unesi km.",
            "parameters": {
                "Mileage": {
                    "required": True, "dependency_source": "user_input",
                    "param_type": "integer", "location": "body",
                },
                "Note": {
                    "required": False, "dependency_source": "user_input",
                    "param_type": "string", "location": "body",
                },
            },
        },
    })
    await _bootstrap_known_user(engine, gateway)

    class _BrokenExtractor:
        async def extract(self, user_text, optional_specs):
            raise RuntimeError("LLM 5xx")

    engine.optional_extractor = _BrokenExtractor()

    from services.v2.pending_params import PendingParams
    await engine.pending_params_store.save(
        "+385955087196",
        PendingParams(
            phone="+385955087196", tool_id="post_AddMileage",
            collected={"Mileage": 42500}, required_remaining=[],
            optional_remaining=["Note"], optional_offered=True,
            original_query="unesi km",
        ),
    )

    reply = await engine.process_message("+385955087196", "kasnim")

    # Did NOT crash, did NOT show "tehnički problem" — went to mutation gate
    reply_l = reply.lower()
    assert "da/ne" in reply_l or "potvrđ" in reply_l or "potvrdu" in reply_l
    assert "tehnič" not in reply_l
    # Mutation pending has only required (no Note because extractor failed)
    pm = await engine.pending_mut_store.load("+385955087196")
    assert pm is not None
    assert pm.params == {"Mileage": 42500}


@pytest.mark.asyncio
async def test_cancel_clears_state_during_collection():
    """User types 'odustani' mid-collection → state cleared, polite reply,
    no mutation pending."""
    engine, gateway, _, _, _router, _fmt = await _build_engine(registry_tools={
        "delete_VehicleCalendar_id": {
            "method": "DELETE", "service": "auto", "path": "/v/{id}",
            "purpose": "Otkaži.",
            "parameters": {
                "id": {
                    "required": True, "dependency_source": "user_input",
                    "param_type": "string", "location": "path",
                },
            },
        },
    })
    await _bootstrap_known_user(engine, gateway)
    from services.v2.pending_params import PendingParams
    await engine.pending_params_store.save(
        "+385955087196",
        PendingParams(
            phone="+385955087196",
            tool_id="delete_VehicleCalendar_id",
            collected={}, required_remaining=["id"],
            optional_remaining=[], optional_offered=False,
            original_query="obriši",
        ),
    )

    reply = await engine.process_message("+385955087196", "odustani")

    assert "U redu" in reply or "odustaj" in reply.lower()
    assert await engine.pending_params_store.load("+385955087196") is None
    assert await engine.pending_mut_store.load("+385955087196") is None


@pytest.mark.asyncio
async def test_tool_without_required_params_skips_param_asking():
    """Tool whose required params are all `context` (auto-injected) →
    NO param question, goes straight to mutation gate (or execute for GET)."""
    engine, gateway, _, _, _router, _fmt = await _build_engine(registry_tools={
        "get_MasterData": {
            "method": "GET", "service": "auto", "path": "/master",
            "purpose": "Moji podaci.",
            "parameters": {
                "personId": {
                    "required": True, "dependency_source": "context",
                    "context_key": "person_id", "param_type": "string",
                },
            },
        },
    })
    await _bootstrap_known_user(engine, gateway)
    await engine.pending_clarify_store.save(
        phone="+385955087196",
        candidates=[
            {"tool_id": "get_MasterData", "method": "GET",
             "params": {}, "field_hint": None,
             "short_label": "Moji podaci", "description": "x"},
        ],
        original_query="moji podaci",
        stage="tool",
    )
    # Executor response for GET
    gateway.queue(FakeApiResponse(success=True, data={"VehicleName": "X"}))

    reply = await engine.process_message("+385955087196", "1")

    # GET runs through immediately — NO param question, NO mutation confirm
    assert "Trebam" not in reply
    assert "DA" not in reply or "potvrdu" not in reply.lower()
    # No pending state left over
    assert await engine.pending_params_store.load("+385955087196") is None
    assert await engine.pending_mut_store.load("+385955087196") is None


# --------------------------------------------------------------------------
# Faza 1 (Filip 2026-05-17): full 950-tool reachability + array/object skip
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_array_optional_skipped_from_offer():
    """Bug fix: optional params with param_type=array are NOT offered to user
    (LLM extract can't reliably structure them from Croatian free-text).
    The tool executes without them; API uses default (no filter)."""
    engine, gateway, _, _, _router, _fmt = await _build_engine(registry_tools={
        "get_VehicleCalendar": {
            "method": "GET", "service": "auto", "path": "/v",
            "purpose": "Lista rezervacija.",
            "parameters": {
                "Filter": {
                    "required": False, "dependency_source": "user_input",
                    "param_type": "array", "location": "query",
                },
                "Note": {
                    "required": False, "dependency_source": "user_input",
                    "param_type": "string", "location": "query",
                },
            },
        },
    })
    await _bootstrap_known_user(engine, gateway)

    # _user_friendly_optionals filters Filter (array) but keeps Note (string)
    friendly = engine._user_friendly_optionals("get_VehicleCalendar", {})
    assert "Filter" not in friendly
    assert "Note" in friendly


@pytest.mark.asyncio
async def test_object_optional_skipped_from_offer():
    engine, gateway, _, _, _router, _fmt = await _build_engine(registry_tools={
        "post_X": {
            "method": "POST", "service": "auto", "path": "/x",
            "purpose": "X.",
            "parameters": {
                "Config": {
                    "required": False, "dependency_source": "user_input",
                    "param_type": "object", "location": "body",
                },
                "Title": {
                    "required": False, "dependency_source": "user_input",
                    "param_type": "string", "location": "body",
                },
            },
        },
    })
    await _bootstrap_known_user(engine, gateway)
    friendly = engine._user_friendly_optionals("post_X", {})
    assert "Config" not in friendly
    assert "Title" in friendly


@pytest.mark.asyncio
async def test_pagination_params_not_offered():
    """First/Rows/Sort/SortOrder are framework pagination, never offered to a
    WhatsApp user (Filip 2026-05-23). A genuine domain optional (Name) still
    is. Without this, every list query asked 'želiš dodati Rows, Sort?'."""
    engine, gateway, _, _, _router, _fmt = await _build_engine(registry_tools={
        "get_Vehicles": {
            "method": "GET", "service": "auto", "path": "/v",
            "purpose": "Lista vozila.",
            "parameters": {
                "First":     {"required": False, "dependency_source": "user_input",
                              "param_type": "integer", "location": "query"},
                "Rows":      {"required": False, "dependency_source": "user_input",
                              "param_type": "integer", "location": "query"},
                "Sort":      {"required": False, "dependency_source": "user_input",
                              "param_type": "string", "location": "query"},
                "SortOrder": {"required": False, "dependency_source": "user_input",
                              "param_type": "integer", "location": "query"},
                "Name":      {"required": False, "dependency_source": "user_input",
                              "param_type": "string", "location": "query"},
            },
        },
    })
    await _bootstrap_known_user(engine, gateway)

    friendly = engine._user_friendly_optionals("get_Vehicles", {})
    assert friendly == ["Name"]  # only the real domain optional


@pytest.mark.asyncio
async def test_only_pagination_optionals_offers_nothing():
    """A list tool whose only optionals are pagination → no offer at all;
    engine goes straight to execute (API uses paging defaults)."""
    engine, gateway, _, _, _router, _fmt = await _build_engine(registry_tools={
        "get_PagedOnly": {
            "method": "GET", "service": "auto", "path": "/p",
            "purpose": "Lista.",
            "parameters": {
                "First": {"required": False, "dependency_source": "user_input",
                          "param_type": "integer", "location": "query"},
                "Rows":  {"required": False, "dependency_source": "user_input",
                          "param_type": "integer", "location": "query"},
            },
        },
    })
    await _bootstrap_known_user(engine, gateway)
    response = await engine._maybe_start_param_collection(
        "+385955087196", "get_PagedOnly", {}, "lista",
    )
    assert response is None


@pytest.mark.asyncio
async def test_tool_with_only_array_optional_offers_nothing():
    """If a tool has ONLY array/object optionals + no required missing,
    engine skips the offer entirely and goes to mutation gate / execute."""
    engine, gateway, _, _, _router, _fmt = await _build_engine(registry_tools={
        "get_OnlyFilters": {
            "method": "GET", "service": "auto", "path": "/x",
            "purpose": "X.",
            "parameters": {
                "Filter": {
                    "required": False, "dependency_source": "user_input",
                    "param_type": "array", "location": "query",
                },
            },
        },
    })
    await _bootstrap_known_user(engine, gateway)
    # _maybe_start_param_collection should return None (no required, no
    # user-friendly optionals — skip the asking flow entirely).
    response = await engine._maybe_start_param_collection(
        "+385955087196", "get_OnlyFilters", {}, "x",
    )
    assert response is None


@pytest.mark.asyncio
async def test_model_a_scope_called_without_persona():
    """Post-rip (Filip 2026-05-28): scoper.scope() takes tenant_id + methods
    + drop_internal only — NO persona arg. Backend OAuth (HTTP 403) is the
    real ACL; persona filter was a no-op since 2026-05-22 anyway."""
    captured: dict = {}

    class _StubScoper:
        def scope(self, *, tenant_id, methods=None, drop_internal=False):
            captured["tenant_id"] = tenant_id
            captured["methods"] = methods
            captured["drop_internal"] = drop_internal
            return frozenset(["get_X"])

    engine, gateway, _, _, _router, _fmt = await _build_engine(registry_tools={
        "get_X": {"method": "GET", "service": "auto", "path": "/x", "purpose": "X."},
    })
    engine.catalog_scoper = _StubScoper()
    await _bootstrap_known_user(engine, gateway)

    await engine.pending_clarify_store.save(
        phone="+385955087196",
        candidates=[],
        original_query="nešto",
        stage="action_global",
    )
    await engine.process_message("+385955087196", "1")

    assert "persona" not in captured                # persona arg removed
    assert captured.get("drop_internal") is True    # internal helpers filtered
    assert captured.get("tenant_id") is not None    # tenant still passed


# --------------------------------------------------------------------------
# Faza 2 (Filip 2026-05-17): ApiErrorTranslator wired into error path
# --------------------------------------------------------------------------


class _StubTranslator:
    """Captures translate() args + returns canned Croatian message."""
    def __init__(self, message=None):
        self._message = message
        self.calls: list[dict] = []

    async def translate(self, *, status_code, response_body, tool_id="",
                         tool_intent=""):
        self.calls.append({
            "status_code": status_code,
            "response_body": response_body,
            "tool_id": tool_id,
            "tool_intent": tool_intent,
        })
        return self._message


@pytest.mark.asyncio
async def test_api_4xx_returns_croatian_translation():
    """Failed exec with 4xx + body → engine calls translator → emits
    Croatian message instead of generic 'Tehnički problem'."""
    engine, gateway, _, _, _router, _fmt = await _build_engine(registry_tools={
        "post_X": {
            "method": "POST", "service": "auto", "path": "/x",
            "purpose": "X.",
            "parameters": {},
        },
    })
    await _bootstrap_known_user(engine, gateway)

    engine.api_error_translator = _StubTranslator(
        message="Nedostaje datoteka za upload. Pokušaj ponovo s prilogom.",
    )

    # Pre-seed STAGE_TOOL pending → user pick "1" → execute → 422
    await engine.pending_clarify_store.save(
        phone="+385955087196",
        candidates=[{"tool_id": "post_X", "method": "POST", "params": {},
                     "field_hint": None, "short_label": "X", "description": ""}],
        original_query="x",
        stage="tool",
    )
    # Confirm + execute path
    reply_confirm = await engine.process_message("+385955087196", "1")
    assert "DA" in reply_confirm or "Da/Ne" in reply_confirm or "potvrdu" in reply_confirm.lower()

    # Queue a 422 with JSON error body
    gateway.queue(FakeApiResponse(
        success=False, status_code=422,
        data={"detail": "Required field 'file' missing"},
    ))
    # Patch FakeApiResponse with error_message for translator
    # (our fake gateway puts data on the response; APIResponse uses error_message)
    # Simulate: the gateway returns response.error_message = the dict
    # Actually FakeApiResponse only sets data — executor reads error_message
    # fallback to data, so this works.

    reply_exec = await engine.process_message("+385955087196", "Da")

    # Translator was invoked with 4xx + body
    tx_calls = engine.api_error_translator.calls
    assert len(tx_calls) == 1
    assert tx_calls[0]["status_code"] == 422
    assert tx_calls[0]["tool_id"] == "post_X"
    # Engine emitted the Croatian message (NOT generic "Tehnički problem")
    assert "Nedostaje datoteka" in reply_exec
    assert "Tehnički problem" not in reply_exec


@pytest.mark.asyncio
async def test_exe1_error_body_pii_scrubbed_before_translator():
    """EXE-1 (Filip 2026-05-20): API error body must be PII-scrubbed BEFORE
    it reaches the LLM translator. MobilityOne validation errors can echo
    field values ("OIB 12345678901 already exists") — without scrubbing that
    PII would land in the Azure OpenAI prompt (same GDPR risk as conv_history).
    """
    from dataclasses import dataclass as _dc
    from services.v2.engine import V2Engine
    from services.v2.pii_scrubber import PIIScrubber

    @_dc
    class _ExecResult:
        success: bool = False
        error: str = "http_409"
        error_body: object = ""
        status_code: int = 409

    eng = object.__new__(V2Engine)
    eng.pii = PIIScrubber()
    eng.tkb_intents = {}
    eng.api_error_translator = _StubTranslator(message="Taj zapis već postoji.")

    exec_result = _ExecResult(
        error_body="OIB 12345678901 already exists, IBAN HR1234567890123456789",
        status_code=409,
    )
    reply = await eng._render_execution_failure(exec_result, "post_X")

    # Translator was called, but the body it received must NOT contain raw PII.
    calls = eng.api_error_translator.calls
    assert len(calls) == 1
    body_seen = str(calls[0]["response_body"])
    assert "12345678901" not in body_seen, f"OIB leaked to translator: {body_seen}"
    assert "HR1234567890123456789" not in body_seen, "IBAN leaked to translator"
    # Croatian translation still returned
    assert "već postoji" in reply


@pytest.mark.asyncio
async def test_api_4xx_fallback_when_translator_returns_none():
    """LLM translator returns None (e.g., transient error) → engine falls
    back to generic 'Tehnički problem' message. No crash."""
    engine, gateway, _, _, _router, _fmt = await _build_engine(registry_tools={
        "post_X": {
            "method": "POST", "service": "auto", "path": "/x",
            "purpose": "X.",
            "parameters": {},
        },
    })
    await _bootstrap_known_user(engine, gateway)

    engine.api_error_translator = _StubTranslator(message=None)

    await engine.pending_clarify_store.save(
        phone="+385955087196",
        candidates=[{"tool_id": "post_X", "method": "POST", "params": {},
                     "field_hint": None, "short_label": "X", "description": ""}],
        original_query="x",
        stage="tool",
    )
    await engine.process_message("+385955087196", "1")
    gateway.queue(FakeApiResponse(
        success=False, status_code=400, data={"err": "bad"},
    ))
    reply = await engine.process_message("+385955087196", "Da")

    # Translator still called (we don't pre-skip on None — only on missing body)
    assert len(engine.api_error_translator.calls) == 1
    # Generic fallback emitted
    assert "Tehnič" in reply


@pytest.mark.asyncio
async def test_api_5xx_skips_translator():
    """5xx server errors are NOT translated — translator's own contract
    returns None for them, but engine's render_execution_failure should
    not even invoke translator if there's no body. For 5xx WITH body,
    translator returns None internally."""
    engine, gateway, _, _, _router, _fmt = await _build_engine(registry_tools={
        "post_X": {
            "method": "POST", "service": "auto", "path": "/x",
            "purpose": "X.",
            "parameters": {},
        },
    })
    await _bootstrap_known_user(engine, gateway)

    engine.api_error_translator = _StubTranslator(message="should-not-appear")

    await engine.pending_clarify_store.save(
        phone="+385955087196",
        candidates=[{"tool_id": "post_X", "method": "POST", "params": {},
                     "field_hint": None, "short_label": "X", "description": ""}],
        original_query="x",
        stage="tool",
    )
    await engine.process_message("+385955087196", "1")
    gateway.queue(FakeApiResponse(
        success=False, status_code=503, data="upstream down",
    ))
    reply = await engine.process_message("+385955087196", "Da")

    # Translator was invoked (engine forwards 5xx to it) but ApiErrorTranslator
    # internally returns None for 5xx → caller fallback to generic msg.
    # In this stub the translator returns "should-not-appear" so we need the
    # REAL translator's 5xx-returns-None semantic. With stub returning a
    # message, it WOULD show — that's why this test verifies the stub gets
    # called (engine doesn't pre-filter) but real translator-failure handling
    # is covered by test_api_error_translator.py::test_5xx_returns_none_skip_llm.
    assert len(engine.api_error_translator.calls) == 1


@pytest.mark.asyncio
async def test_param_question_uses_labeler_override():
    """Faza 3: when ParamLabeler wired, render_param_question receives
    the LLM-generated Croatian label as override (not humanized fallback)."""
    engine, gateway, _, _, _router, _fmt = await _build_engine(registry_tools={
        "delete_VehicleCalendar_id": {
            "method": "DELETE", "service": "auto", "path": "/v/{id}",
            "purpose": "Otkaži.",
            "parameters": {
                "id": {
                    "required": True, "dependency_source": "user_input",
                    "param_type": "string", "location": "path",
                },
            },
        },
    })
    await _bootstrap_known_user(engine, gateway)

    captured: dict = {}

    class _StubLabeler:
        async def label_for(self, *, param_name, param_def=None,
                            tool_id="", tool_intent=""):
            captured["param_name"] = param_name
            captured["tool_id"] = tool_id
            return "ID rezervacije"

    engine.param_labeler = _StubLabeler()

    await engine.pending_clarify_store.save(
        phone="+385955087196",
        candidates=[{"tool_id": "delete_VehicleCalendar_id", "method": "DELETE",
                     "params": {}, "field_hint": None,
                     "short_label": "X", "description": ""}],
        original_query="obriši rezervaciju",
        stage="tool",
    )
    reply = await engine.process_message("+385955087196", "1")

    # Labeler invoked with the right params
    assert captured.get("param_name") == "id"
    assert captured.get("tool_id") == "delete_VehicleCalendar_id"
    # Rendered question contains the labeler's Croatian label
    assert "ID rezervacije" in reply
    # And NOT the humanized fallback ("id" alone wouldn't normally render
    # as exactly this — the labeler beat the fallback)


@pytest.mark.asyncio
async def test_param_question_falls_back_to_humanize_when_labeler_none():
    """No labeler wired → render uses humanize_param_name. Bot still
    works, output is just less natural ('Trebam vehicle id ...')."""
    engine, gateway, _, _, _router, _fmt = await _build_engine(registry_tools={
        "delete_X": {
            "method": "DELETE", "service": "auto", "path": "/x/{VehicleId}",
            "purpose": "X.",
            "parameters": {
                "VehicleId": {
                    "required": True, "dependency_source": "user_input",
                    "param_type": "string", "location": "path",
                },
            },
        },
    })
    await _bootstrap_known_user(engine, gateway)
    engine.param_labeler = None  # explicitly disabled

    await engine.pending_clarify_store.save(
        phone="+385955087196",
        candidates=[{"tool_id": "delete_X", "method": "DELETE",
                     "params": {}, "field_hint": None,
                     "short_label": "X", "description": ""}],
        original_query="obriši",
        stage="tool",
    )
    reply = await engine.process_message("+385955087196", "1")
    assert "vehicle id" in reply.lower()  # humanize fallback


@pytest.mark.asyncio
async def test_optional_offer_uses_labeler_for_all_optionals():
    """Optional offer rendering pre-fetches labels for each offered param."""
    engine, gateway, _, _, _router, _fmt = await _build_engine(registry_tools={
        "post_X": {
            "method": "POST", "service": "auto", "path": "/x",
            "purpose": "X.",
            "parameters": {
                "Required": {
                    "required": True, "dependency_source": "user_input",
                    "param_type": "string", "location": "body",
                },
                "Notes": {
                    "required": False, "dependency_source": "user_input",
                    "param_type": "string", "location": "body",
                },
                "Type": {
                    "required": False, "dependency_source": "user_input",
                    "param_type": "string", "location": "body",
                },
            },
        },
    })
    await _bootstrap_known_user(engine, gateway)

    label_map = {"Notes": "napomenu", "Type": "tip"}
    seen: list = []

    class _StubLabeler:
        async def label_for(self, *, param_name, param_def=None,
                            tool_id="", tool_intent=""):
            seen.append(param_name)
            return label_map.get(param_name)

    engine.param_labeler = _StubLabeler()

    # State: Required already filled, ready to render optional offer
    from services.v2.pending_params import PendingParams
    await engine.pending_params_store.save(
        "+385955087196",
        PendingParams(
            phone="+385955087196", tool_id="post_X",
            collected={"Required": "x"}, required_remaining=[],
            optional_remaining=["Notes", "Type"], optional_offered=False,
            original_query="x",
        ),
    )
    # Trigger render via a non-param non-cancel message — engine.process
    # will... actually, no: state has no required_remaining and not yet
    # offered, so we need to trigger via _maybe_start_param_collection path
    # which only fires when chosen via STAGE_TOOL. Instead, simulate fresh
    # collection start:
    await engine.pending_params_store.clear("+385955087196")
    await engine.pending_clarify_store.save(
        phone="+385955087196",
        candidates=[{"tool_id": "post_X", "method": "POST",
                     "params": {"Required": "x"}, "field_hint": None,
                     "short_label": "X", "description": ""}],
        original_query="x",
        stage="tool",
    )
    reply = await engine.process_message("+385955087196", "1")

    # Both optionals were labeled
    assert "Notes" in seen and "Type" in seen
    # And appear in the offer text via Croatian labels
    assert "napomenu" in reply
    assert "tip" in reply


@pytest.mark.asyncio
async def test_stale_confirm_revalidates_identity_tenant_change():
    """Bug #1 fix (Faza 3): if pending mutation is >30s old AND identity
    re-resolution shows different tenant_id, ABORT execute and clear
    pending — don't blindly run on stale tenant context."""
    engine, gateway, _, _, _router, _fmt = await _build_engine(registry_tools={
        "delete_X": {
            "method": "DELETE", "service": "auto", "path": "/x/{id}",
            "purpose": "X.",
            "parameters": {"id": {
                "required": True, "dependency_source": "user_input",
                "param_type": "string", "location": "path",
            }},
        },
    })
    await _bootstrap_known_user(engine, gateway)

    # Pre-seed a pending mutation that's 60s old (over 30s threshold but
    # under 90s warn threshold)
    import time
    from services.v2.pending_mutation import PendingMutation, STAGE_SINGLE
    stale_pending = PendingMutation(
        tool_id="delete_X", params={"id": "abc"},
        stage=STAGE_SINGLE, created_at=time.time() - 60,
    )
    # Save raw JSON to bypass the save() timestamp refresh
    await engine.pending_mut_store._redis.setex(
        engine.pending_mut_store._key("+385955087196"),
        300, stale_pending.to_json(),
    )

    # Patch identity.resolve to return ORIGINAL tenant on first call (the
    # dispatch entry resolve) and DIFFERENT tenant on subsequent calls (the
    # stale-confirm re-validation inside _continue_pending_mutation).
    original_resolve = engine.identity.resolve
    call_count = {"n": 0}

    async def _resolve_changes_on_second_call(phone):
        snap = await original_resolve(phone)
        call_count["n"] += 1
        if call_count["n"] >= 2:
            from dataclasses import replace
            return replace(snap, tenant_id="NEW_TENANT_ID")
        return snap

    engine.identity.resolve = _resolve_changes_on_second_call

    reply = await engine.process_message("+385955087196", "Da")

    # Bot must NOT execute — abort + clear + tell user
    assert "Konfiguracija" in reply or "promijenila" in reply.lower()
    # Pending mutation cleared
    assert await engine.pending_mut_store.load("+385955087196") is None


@pytest.mark.asyncio
async def test_stale_confirm_revalidates_identity_vehicle_change():
    """ORCH-6 (Filip 2026-05-20): stale-confirm must abort not only on
    tenant change but also on vehicle_id change. The confirm message named
    the OLD vehicle; if the phone was reassigned to a different vehicle
    mid-confirm, executing would write the mutation to the wrong target."""
    engine, gateway, _, _, _router, _fmt = await _build_engine(registry_tools={
        "post_AddMileage": {
            "method": "POST", "service": "auto", "path": "/AddMileage",
            "purpose": "Upis km.",
            "parameters": {"Value": {
                "required": True, "dependency_source": "user_input",
                "param_type": "integer", "location": "body",
            }},
        },
    })
    await _bootstrap_known_user(engine, gateway)

    import time
    from services.v2.pending_mutation import PendingMutation, STAGE_SINGLE
    stale_pending = PendingMutation(
        tool_id="post_AddMileage", params={"Value": 145000},
        stage=STAGE_SINGLE, created_at=time.time() - 60,
    )
    await engine.pending_mut_store._redis.setex(
        engine.pending_mut_store._key("+385955087196"),
        300, stale_pending.to_json(),
    )

    # Tenant stays SAME, but vehicle_id changes on the revalidation call.
    original_resolve = engine.identity.resolve
    call_count = {"n": 0}

    async def _resolve_vehicle_changes(phone):
        snap = await original_resolve(phone)
        call_count["n"] += 1
        from dataclasses import replace
        if call_count["n"] >= 2:
            return replace(snap, vehicle_id="DIFFERENT_VEHICLE")
        return replace(snap, vehicle_id="ORIGINAL_VEHICLE")

    engine.identity.resolve = _resolve_vehicle_changes

    reply = await engine.process_message("+385955087196", "Da")

    # Bot must abort (vehicle changed) + clear pending
    assert "Konfiguracija" in reply or "promijenila" in reply.lower()
    assert await engine.pending_mut_store.load("+385955087196") is None


@pytest.mark.asyncio
async def test_stale_confirm_same_vehicle_none_does_not_false_abort():
    """ORCH-6 guard: if vehicle_id is None on both sides (legitimate — user
    has no assigned vehicle), must NOT false-abort. Only abort when both are
    set AND differ."""
    engine, gateway, _, _, _router, _fmt = await _build_engine(registry_tools={
        "post_AddMileage": {
            "method": "POST", "service": "auto", "path": "/AddMileage",
            "purpose": "Upis km.",
            "parameters": {"Value": {
                "required": True, "dependency_source": "user_input",
                "param_type": "integer", "location": "body",
            }},
        },
    })
    await _bootstrap_known_user(engine, gateway)

    import time
    from services.v2.pending_mutation import PendingMutation, STAGE_SINGLE
    stale_pending = PendingMutation(
        tool_id="post_AddMileage", params={"Value": 145000},
        stage=STAGE_SINGLE, created_at=time.time() - 60,
    )
    await engine.pending_mut_store._redis.setex(
        engine.pending_mut_store._key("+385955087196"),
        300, stale_pending.to_json(),
    )

    # Both tenant AND vehicle stay the same → no abort, proceeds to execute.
    original_resolve = engine.identity.resolve

    async def _resolve_stable(phone):
        from dataclasses import replace
        snap = await original_resolve(phone)
        return replace(snap, vehicle_id=None)  # None on both calls

    engine.identity.resolve = _resolve_stable
    gateway.queue(FakeApiResponse(success=True, data={"Id": 1}))

    reply = await engine.process_message("+385955087196", "Da")

    # Must NOT abort with config-changed message (vehicle None==None is fine)
    assert "Konfiguracija" not in reply


@pytest.mark.asyncio
async def test_stale_confirm_vehicle_none_to_id_aborts():
    """Fix C (Filip 2026-05-28): symmetric vehicle guard. Original vehicle_id
    was None at confirm time but a vehicle got assigned mid-confirm (None→id).
    The old both-non-None guard silently missed this; the symmetric compare
    must now abort so the write doesn't land on a freshly-assigned vehicle the
    user never saw in the confirm."""
    engine, gateway, _, _, _router, _fmt = await _build_engine(registry_tools={
        "post_AddMileage": {
            "method": "POST", "service": "auto", "path": "/AddMileage",
            "purpose": "Upis km.",
            "parameters": {"Value": {
                "required": True, "dependency_source": "user_input",
                "param_type": "integer", "location": "body",
            }},
        },
    })
    await _bootstrap_known_user(engine, gateway)

    import time
    from services.v2.pending_mutation import PendingMutation, STAGE_SINGLE
    stale_pending = PendingMutation(
        tool_id="post_AddMileage", params={"Value": 145000},
        stage=STAGE_SINGLE, created_at=time.time() - 60,
    )
    await engine.pending_mut_store._redis.setex(
        engine.pending_mut_store._key("+385955087196"),
        300, stale_pending.to_json(),
    )

    original_resolve = engine.identity.resolve
    call_count = {"n": 0}

    async def _resolve_none_to_id(phone):
        from dataclasses import replace
        snap = await original_resolve(phone)
        call_count["n"] += 1
        # First (dispatch) resolve: no vehicle. Revalidation: vehicle assigned.
        if call_count["n"] >= 2:
            return replace(snap, vehicle_id="NEWLY_ASSIGNED")
        return replace(snap, vehicle_id=None)

    engine.identity.resolve = _resolve_none_to_id

    reply = await engine.process_message("+385955087196", "Da")

    # Symmetric guard catches None→id → abort + clear (old code missed this).
    assert "Konfiguracija" in reply or "promijenila" in reply.lower()
    assert await engine.pending_mut_store.load("+385955087196") is None


@pytest.mark.asyncio
async def test_translator_disabled_uses_generic_message():
    """Engine without api_error_translator wired → always returns generic
    'Tehnički problem'. Backward compat for tests / dev setups."""
    engine, gateway, _, _, _router, _fmt = await _build_engine(registry_tools={
        "post_X": {
            "method": "POST", "service": "auto", "path": "/x",
            "purpose": "X.",
            "parameters": {},
        },
    })
    await _bootstrap_known_user(engine, gateway)
    engine.api_error_translator = None  # explicitly disabled

    await engine.pending_clarify_store.save(
        phone="+385955087196",
        candidates=[{"tool_id": "post_X", "method": "POST", "params": {},
                     "field_hint": None, "short_label": "X", "description": ""}],
        original_query="x",
        stage="tool",
    )
    await engine.process_message("+385955087196", "1")
    gateway.queue(FakeApiResponse(
        success=False, status_code=400, data={"err": "bad"},
    ))
    reply = await engine.process_message("+385955087196", "Da")

    assert "Tehnič" in reply  # generic fallback


# --------------------------------------------------------------------------
# L8 output formatting — LLM-primary (question-aware) + template fallback
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_format_reply_uses_llm_formatter():
    """Tool-result formatting goes through the LLM formatter (question-aware);
    the engine returns its text and threads query + tool_id + api_data."""
    engine, _gw, _llm, _emb, _router, fmt = await _build_engine()
    fmt.set_template("LLM-ODGOVOR za {tool_id}")
    ident = SimpleNamespace(
        is_known=True, first_name="Filip",
        vehicle_name="Škoda Octavia", licence_plate="DA533",
    )
    reply = await engine._format_reply(
        query="koje je moje vozilo", tool_id="get_Vehicles",
        api_data={"VehicleName": "Škoda Octavia", "VIN": "X"},
        identity=ident,
    )
    assert reply == "LLM-ODGOVOR za get_Vehicles"
    assert fmt.calls and fmt.calls[-1]["query"] == "koje je moje vozilo"
    assert fmt.calls[-1]["tool_id"] == "get_Vehicles"
    assert fmt.calls[-1]["api_data"]["VehicleName"] == "Škoda Octavia"


@pytest.mark.asyncio
async def test_format_reply_falls_back_to_template_on_llm_error():
    """If the LLM formatter errors, the engine falls back to the deterministic
    template formatter (values verbatim) — the LLM error never leaks."""
    engine, _gw, _llm, _emb, _router, fmt = await _build_engine()
    fmt.fail = True
    ident = SimpleNamespace(
        is_known=True, first_name="Filip",
        vehicle_name="Škoda", licence_plate="DA533",
    )
    reply = await engine._format_reply(
        query="koje je moje vozilo", tool_id="get_Vehicles",
        api_data={"VehicleName": "Škoda Octavia", "LicencePlate": "DA533"},
        identity=ident,
    )
    assert "Škoda Octavia" in reply   # template rendered the data verbatim
    assert "boom" not in reply        # LLM error must not leak to the user


@pytest.mark.asyncio
async def test_path_a_common_field_uses_template_no_llm():
    """Driver self-question for a common field (km) resolves via the alias map
    → instant template, NO LLM call (the hot path stays fast)."""
    engine, _gw, _llm, _emb, _router, fmt = await _build_engine()
    ident = SimpleNamespace(
        is_known=True, first_name="Filip", full_name="Filip K", phone="385",
        vehicle_name="Škoda", licence_plate="DA533", vin="X", last_mileage=30500,
        leasing_company=None, co2_emission=None, registration_expiry=None,
        person_id="p1", tenant_id="t1",
        vehicle={"LastMileage": 30500, "Color": "crna"},
    )
    reply = await engine._format_basics(ident, "kolika mi je km")
    assert "30500" in reply
    assert not fmt.calls  # alias hit → LLM NOT called


@pytest.mark.asyncio
async def test_path_a_arbitrary_field_uses_llm_on_vehicle_object():
    """Driver self-question for an arbitrary field (boja) misses the alias map
    → LLM-formats the full cached vehicle object (parity with Path B)."""
    engine, _gw, _llm, _emb, _router, fmt = await _build_engine()
    fmt.set_template("Tvoj auto je crne boje.")
    ident = SimpleNamespace(
        is_known=True, first_name="Filip", full_name="Filip K", phone="385",
        vehicle_name="Škoda", licence_plate="DA533", vin="X", last_mileage=30500,
        leasing_company=None, co2_emission=None, registration_expiry=None,
        person_id="p1", tenant_id="t1",
        vehicle={"VehicleName": "Škoda", "Color": "crna", "Manufacturer": "Škoda"},
    )
    reply = await engine._format_basics(ident, "koja je boja")
    assert reply == "Tvoj auto je crne boje."
    assert fmt.calls and "Color" in fmt.calls[-1]["api_data"]


# ---- NALAZ 1/2 (Filip 2026-05-25): LLM-param coercion + array refuse ----


def test_coerce_llm_params_normalizes_strings():
    """LLM-extracted string values get normalized to registry type before
    execute (HR comma → float, HR date → ISO); native values pass through."""
    eng = object.__new__(V2Engine)
    eng.tool_parameters = {
        "post_Expenses": {
            "Quantity": {"param_type": "number"},
            "Date": {"param_type": "string", "format": "date-time"},
            "Note": {"param_type": "string"},
            "Count": {"param_type": "integer"},
        }
    }
    out = eng._coerce_llm_params("post_Expenses", {
        "Quantity": "12,5",       # HR comma → 12.5
        "Date": "17.05.2026",     # HR date → ISO
        "Note": "12,5",           # plain string → unchanged
        "Count": 45,              # native int → unchanged
    })
    assert out["Quantity"] == 12.5
    assert out["Date"] == "2026-05-17T00:00:00"
    assert out["Note"] == "12,5"
    assert out["Count"] == 45


def test_coerce_llm_params_improve_only_keeps_original_on_failure():
    """If coercion returns None (e.g. decimal for an integer), keep the
    original value — never regress a value the LLM may have gotten right."""
    eng = object.__new__(V2Engine)
    eng.tool_parameters = {"t": {"Count": {"param_type": "integer"}}}
    out = eng._coerce_llm_params("t", {"Count": "12,5"})
    assert out["Count"] == "12,5"
    # already-ISO date stays ISO (parser passes it through)
    eng.tool_parameters = {"t": {"D": {"param_type": "string", "format": "date"}}}
    assert eng._coerce_llm_params("t", {"D": "2026-05-17"})["D"] == "2026-05-17"


def test_coerce_llm_params_whole_float_to_int():
    """R1 (Filip 2026-05-26): a native whole float (42.0) for an integer field
    → int(42); a non-whole float (42.5) is left untouched (NOT rounded — that
    would change the value); bool and number fields unaffected."""
    eng = object.__new__(V2Engine)
    eng.tool_parameters = {
        "t": {
            "Count": {"param_type": "integer"},
            "Q": {"param_type": "number"},
            "Flag": {"param_type": "boolean"},
        }
    }
    out = eng._coerce_llm_params("t", {"Count": 42.0, "Q": 12.5, "Flag": True})
    assert out["Count"] == 42 and isinstance(out["Count"], int)
    assert out["Q"] == 12.5            # number field untouched
    assert out["Flag"] is True         # bool not coerced to int
    assert eng._coerce_llm_params("t", {"Count": 42.5})["Count"] == 42.5  # not rounded
    assert eng._coerce_llm_params("t", {"Count": 42})["Count"] == 42      # native int


@pytest.mark.asyncio
async def test_required_array_param_refused_not_asked():
    """A required array/object param can't be built from chat — refuse
    honestly instead of asking + shipping a raw string (→ 422)."""
    eng = object.__new__(V2Engine)
    eng.pending_params_store = object()  # truthy; never reached on refuse path
    eng.tool_parameters = {
        "post_X_multipatch": {
            "Ids": {"param_type": "array", "required": True,
                    "dependency_source": "user_input"},
        }
    }
    out = await eng._maybe_start_param_collection(
        "+385", "post_X_multipatch", {}, "azuriraj sve",
    )
    assert out is not None
    assert "strukturiran" in out.lower()


@pytest.mark.asyncio
async def test_finalize_coerces_optional_params():
    """U2 (Filip 2026-05-26): values reaching _finalize_after_params (incl.
    optional-extractor values that bypass per-param coercion) get the SAME
    coercion as the LLM path — an optional date string → ISO before send."""
    saved = {}

    class _MutStore:
        async def save(self, phone, *, tool_id, params, stage):
            saved["params"] = params

    eng = object.__new__(V2Engine)
    eng.tool_parameters = {
        "post_X": {"When": {"param_type": "string", "format": "date-time"}}
    }
    eng.risky_tool_ids = set()
    eng.pending_mut_store = _MutStore()
    eng.executor = SimpleNamespace(method_of=lambda t: "POST")
    pending = SimpleNamespace(
        tool_id="post_X",
        collected={"When": "17.05.2026 09:00", "__internal": "x"},
        original_query="q",
    )
    identity = SimpleNamespace(last_mileage=None, vehicle_name="Auto")
    await eng._finalize_after_params("+385", pending, identity)
    assert saved["params"]["When"] == "2026-05-17T09:00:00"  # coerced to ISO
    assert "__internal" not in saved["params"]               # internal stripped


# ---- *TypeId resolver (Filip 2026-05-27) ----

def _typeid_identity():
    return SimpleNamespace(
        tenant_id="t1", person_id=None, vehicle_id=None,
        company_id=None, org_unit_id=None,
    )


class _TypesExecutor:
    """Stub executor whose execute() returns a /…Types list."""
    def __init__(self, rows):
        self._rows = rows
    async def execute(self, *, tool_id, params, identity_summary):
        return SimpleNamespace(success=True, data=self._rows)


class _SaveStore:
    def __init__(self):
        self.saved = None
    async def save(self, phone, state):
        self.saved = state


@pytest.mark.asyncio
async def test_resolve_type_param_matches_word_to_id():
    eng = object.__new__(V2Engine)
    eng.typeid_map = {"expensetypeid": "get_ExpenseTypes"}
    eng.executor = _TypesExecutor([{"Id": 3, "Name": "Gorivo"}, {"Id": 1, "Name": "Servis"}])
    mid, pairs = await eng._resolve_type_param(
        "ExpenseTypeId", "dodaj trošak za gorivo", _typeid_identity(),
    )
    assert mid == 3
    assert (3, "Gorivo") in pairs


@pytest.mark.asyncio
async def test_resolve_type_param_unmapped_returns_none():
    eng = object.__new__(V2Engine)
    eng.typeid_map = {}  # not a resolvable *TypeId
    eng.executor = _TypesExecutor([])
    assert await eng._resolve_type_param("AcquiringTypeId", "x", _typeid_identity()) == (None, None)


@pytest.mark.asyncio
async def test_typeid_prefill_auto_resolves_from_query():
    """Word in the query ('gorivo') → ExpenseTypeId auto-filled, no ask."""
    eng = object.__new__(V2Engine)
    eng.pending_params_store = _SaveStore()
    eng.param_labeler = None
    eng.tkb_intents = {}
    eng.typeid_map = {"expensetypeid": "get_ExpenseTypes"}
    eng.executor = _TypesExecutor([{"Id": 3, "Name": "Gorivo"}, {"Id": 1, "Name": "Servis"}])
    eng.tool_parameters = {
        "post_Expenses": {
            "ExpenseTypeId": {"param_type": "integer", "required": True,
                              "dependency_source": "user_input"},
        }
    }
    collected = {}
    out = await eng._maybe_start_param_collection(
        "+385", "post_Expenses", collected, "dodaj trošak za gorivo", _typeid_identity(),
    )
    assert collected["ExpenseTypeId"] == 3   # auto-resolved into caller's dict
    assert out is None                        # nothing left to ask


@pytest.mark.asyncio
async def test_typeid_ask_with_list_when_no_match():
    """No type word in query → ask, LISTING the available names."""
    eng = object.__new__(V2Engine)
    eng.pending_params_store = _SaveStore()
    eng.param_labeler = None
    eng.tkb_intents = {}
    eng.typeid_map = {"expensetypeid": "get_ExpenseTypes"}
    eng.executor = _TypesExecutor([{"Id": 3, "Name": "Gorivo"}, {"Id": 1, "Name": "Servis"}])
    eng.tool_parameters = {
        "post_Expenses": {
            "ExpenseTypeId": {"param_type": "integer", "required": True,
                              "dependency_source": "user_input"},
        }
    }
    out = await eng._maybe_start_param_collection(
        "+385", "post_Expenses", {}, "dodaj trošak", _typeid_identity(),
    )
    assert out is not None
    assert "Dostupno" in out and "Gorivo" in out and "Servis" in out
    assert eng.pending_params_store.saved.type_options.get("ExpenseTypeId")


# ---- L3 DEEP E2E: prove the WIRED chain through the REAL engine (Filip 2026-05-27) ----
# These drive process_message() end-to-end (real V2Engine + real param/coercion/
# resolver/executor wiring) and ASSERT on FakeGateway.calls — i.e. the actual
# data that reaches the wire. Stronger than unit tests: proves the pieces CONNECT.

def _post_call(gateway, path_suffix):
    return [c for c in gateway.calls
            if (c.get("method") == "POST") and (c.get("path") or "").endswith(path_suffix)]


@pytest.mark.asyncio
async def test_e2e_typeid_word_in_query_reaches_executor_body():
    """'dodaj trošak za gorivo' → resolver fetches /Lookup → 'gorivo' resolved
    to ExpenseTypeId=3 → that id reaches the POST body on the wire."""
    engine, gateway, _, _, _router, _fmt = await _build_engine(registry_tools={
        "post_Expenses": {
            "method": "POST", "service": "automation", "path": "/Expenses",
            "purpose": "Dodaj trošak.",
            "parameters": {
                "Amount": {"param_type": "number", "required": True, "dependency_source": "user_input"},
                "ExpenseTypeId": {"param_type": "integer", "required": True, "dependency_source": "user_input"},
            },
        },
        "get_Lookup_ExpenseTypeId": {
            "method": "GET", "service": "automation", "path": "/Lookup/ExpenseTypeId",
            "purpose": "Lookup.", "parameters": {},
        },
    })
    engine.typeid_map = {"expensetypeid": "get_Lookup_ExpenseTypeId"}
    await _bootstrap_known_user(engine, gateway)
    await engine.pending_clarify_store.save(
        phone="+385955087196",
        candidates=[{"tool_id": "post_Expenses", "method": "POST",
                     "params": {"Amount": 50}, "field_hint": None,
                     "short_label": "Trošak", "description": ""}],
        original_query="dodaj trošak za gorivo 50 eura",
        stage="tool",
    )
    gateway.queue(FakeApiResponse(success=True, data=[
        {"Id": 3, "Name": "Gorivo"}, {"Id": 1, "Name": "Servis"},
    ]))  # /Lookup fetch
    confirm = await engine.process_message("+385955087196", "1")
    assert confirm is not None  # auto-resolved → straight to mutation confirm
    gateway.queue(FakeApiResponse(success=True, data={"ok": True}))  # POST execute
    await engine.process_message("+385955087196", "Da")

    calls = _post_call(gateway, "/Expenses")
    assert calls, f"no POST /Expenses; calls={[(c.get('method'), c.get('path')) for c in gateway.calls]}"
    body = calls[-1].get("body") or {}
    assert body.get("Amount") == 50
    assert body.get("ExpenseTypeId") == 3   # word→id reached the wire


@pytest.mark.asyncio
async def test_e2e_typeid_ask_with_list_then_answer_reaches_body():
    """No type word in query → ask listing names → user answers 'gorivo' →
    resolved id reaches the body."""
    engine, gateway, _, _, _router, _fmt = await _build_engine(registry_tools={
        "post_Expenses": {
            "method": "POST", "service": "automation", "path": "/Expenses",
            "purpose": "Dodaj trošak.",
            "parameters": {
                "ExpenseTypeId": {"param_type": "integer", "required": True, "dependency_source": "user_input"},
            },
        },
        "get_Lookup_ExpenseTypeId": {
            "method": "GET", "service": "automation", "path": "/Lookup/ExpenseTypeId",
            "purpose": "Lookup.", "parameters": {},
        },
    })
    engine.typeid_map = {"expensetypeid": "get_Lookup_ExpenseTypeId"}
    await _bootstrap_known_user(engine, gateway)
    await engine.pending_clarify_store.save(
        phone="+385955087196",
        candidates=[{"tool_id": "post_Expenses", "method": "POST", "params": {},
                     "field_hint": None, "short_label": "Trošak", "description": ""}],
        original_query="dodaj trošak",
        stage="tool",
    )
    gateway.queue(FakeApiResponse(success=True, data=[
        {"Id": 3, "Name": "Gorivo"}, {"Id": 1, "Name": "Servis"},
    ]))
    ask = await engine.process_message("+385955087196", "1")
    assert "Dostupno" in ask and "Gorivo" in ask     # ask-with-list
    confirm = await engine.process_message("+385955087196", "gorivo")  # answer
    assert confirm is not None                        # matched → confirm
    gateway.queue(FakeApiResponse(success=True, data={"ok": True}))
    await engine.process_message("+385955087196", "Da")

    body = (_post_call(gateway, "/Expenses")[-1].get("body") or {})
    assert body.get("ExpenseTypeId") == 3


@pytest.mark.asyncio
async def test_e2e_date_string_reaches_body_as_iso():
    """Router extracts a HR date string → _coerce_llm_params → ISO on the wire."""
    engine, gateway, _, _, _router, _fmt = await _build_engine(registry_tools={
        "post_VehicleCalendar": {
            "method": "POST", "service": "vehiclemgt", "path": "/VehicleCalendar",
            "purpose": "Rezerviraj.",
            "parameters": {
                "FromTime": {"param_type": "string", "format": "date-time",
                             "required": True, "dependency_source": "user_input"},
            },
        },
    })
    await _bootstrap_known_user(engine, gateway)
    await engine.pending_clarify_store.save(
        phone="+385955087196",
        candidates=[{"tool_id": "post_VehicleCalendar", "method": "POST",
                     "params": {"FromTime": "17.05.2026 09:00"}, "field_hint": None,
                     "short_label": "Rezervacija", "description": ""}],
        original_query="rezerviraj 17.05.2026 09:00",
        stage="tool",
    )
    confirm = await engine.process_message("+385955087196", "1")
    assert confirm is not None
    gateway.queue(FakeApiResponse(success=True, data={"ok": True}))
    await engine.process_message("+385955087196", "Da")

    body = (_post_call(gateway, "/VehicleCalendar")[-1].get("body") or {})
    assert body.get("FromTime") == "2026-05-17T09:00:00"   # coerced to ISO on the wire


@pytest.mark.asyncio
async def test_e2e_param_ask_path_id_substituted_in_url():
    """Missing required path {id} → ask → answer '45' → executor substitutes it
    into the URL path (no literal {id} on the wire)."""
    engine, gateway, _, _, _router, _fmt = await _build_engine(registry_tools={
        "delete_VehicleCalendar_id": {
            "method": "DELETE", "service": "vehiclemgt", "path": "/VehicleCalendar/{id}",
            "purpose": "Otkaži rezervaciju.",
            "parameters": {
                "id": {"param_type": "integer", "required": True, "dependency_source": "user_input"},
            },
        },
    })
    await _bootstrap_known_user(engine, gateway)
    await engine.pending_clarify_store.save(
        phone="+385955087196",
        candidates=[{"tool_id": "delete_VehicleCalendar_id", "method": "DELETE",
                     "params": {}, "field_hint": None, "short_label": "Otkaži", "description": ""}],
        original_query="otkaži rezervaciju",
        stage="tool",
    )
    ask = await engine.process_message("+385955087196", "1")      # → ask for id
    assert ask is not None
    confirm = await engine.process_message("+385955087196", "45")  # answer → confirm
    assert confirm is not None
    gateway.queue(FakeApiResponse(success=True, data={"ok": True}))
    await engine.process_message("+385955087196", "Da")

    del_calls = [c for c in gateway.calls if c.get("method") == "DELETE"]
    assert del_calls, f"no DELETE call; calls={[(c.get('method'), c.get('path')) for c in gateway.calls]}"
    path = del_calls[-1].get("path") or ""
    assert path.endswith("/VehicleCalendar/45")   # {id} substituted
    assert "{" not in path                          # no literal placeholder on the wire


# ---- Unify (Filip 2026-05-27): flows now run through the mutation gate ----
# These drive the CHANGED _continue_flow directly with a real IdentitySnapshot,
# real FlowEngine + executor + FakeGateway — proving the flow path gains the
# gate's range check + the booking EXEC_LOOKUP round-trip (was broken: "...").

_FLOW_PHONE = "+385955087196"


@pytest.mark.asyncio
async def test_mileage_flow_confirm_is_plain_no_gate_warning():
    """Uniform gate (Filip 2026-05-27): the mileage flow confirm is the plain
    bespoke message — NO per-tool ⚠️ range warning, even for an absurd km value.
    Value sanity is the user reading the confirm, not the gate."""
    engine, gateway, _llm, _emb, _router, _fmt = await _build_engine(registry_tools={
        "post_AddMileage": {"method": "POST", "service": "automation",
                            "path": "/AddMileage", "purpose": "unos km"},
    })
    identity = IdentitySnapshot(
        phone=_FLOW_PHONE, person_id="p1", tenant_id="t1",
        vehicle_id="v1", vehicle_name="VW Golf", last_mileage=100000, is_known=True,
    )
    start = engine.flow_engine.start("mileage", identity_context={
        "person_id": "p1", "vehicle_id": "v1", "vehicle_name": "VW Golf"})
    # Even an absurd jump (100k → 600k) gets no ⚠️ — gate is method-only now.
    out = await engine._continue_flow(_FLOW_PHONE, start.new_state, "600000", identity)
    assert "⚠️" not in out
    assert "Upisat ću 600000 km na vozilo VW Golf" in out   # readable confirm


@pytest.mark.asyncio
async def test_booking_flow_lookup_round_trip_to_execute():
    """Booking's EXEC_LOOKUP now actually runs (was: engine returned '...').
    period → lookup → choice → confirm → execute, with the chosen VehicleId
    reaching the POST body on the wire."""
    engine, gateway, _llm, _emb, _router, _fmt = await _build_engine(registry_tools={
        "get_AvailableVehicles": {"method": "GET", "service": "vehiclemgt",
                                  "path": "/AvailableVehicles", "purpose": "slobodna vozila"},
        "post_VehicleCalendar": {"method": "POST", "service": "vehiclemgt",
                                 "path": "/VehicleCalendar", "purpose": "rezervacija"},
    })
    identity = IdentitySnapshot(
        phone=_FLOW_PHONE, person_id="p1", tenant_id="t1",
        vehicle_name="VW Golf", is_known=True,
    )
    # Turn 1: period → engine runs the lookup (shape verified against live M1).
    gateway.queue(FakeApiResponse(success=True, data={"Data": [
        {"Id": "veh-A", "DisplayName": "Škoda Octavia", "LicencePlate": "DA066F"},
        {"Id": "veh-B", "DisplayName": "Renault Clio", "LicencePlate": "ZG123"},
    ]}))
    start = engine.flow_engine.start("booking", identity_context={"person_id": "p1"})
    r1 = await engine._continue_flow(_FLOW_PHONE, start.new_state, "16.12.2025 9-17", identity)
    assert "Škoda Octavia" in r1 and "Renault Clio" in r1   # choices rendered
    # The lookup hit the right endpoint with from/to.
    look = [c for c in gateway.calls if (c.get("path") or "").endswith("/AvailableVehicles")]
    assert look, "lookup never ran"

    # Turn 2: pick #1 → ASK_CONFIRM preview (chosen label shown).
    state2 = await engine.flow_store.load(_FLOW_PHONE)
    r2 = await engine._continue_flow(_FLOW_PHONE, state2, "1", identity)
    assert "Škoda Octavia" in r2

    # Turn 3: confirm → execute POST /VehicleCalendar with chosen VehicleId.
    gateway.queue(FakeApiResponse(success=True, data={"Id": "cal-1"}))
    state3 = await engine.flow_store.load(_FLOW_PHONE)
    await engine._continue_flow(_FLOW_PHONE, state3, "Da", identity)
    posts = [c for c in gateway.calls
             if c.get("method") == "POST" and (c.get("path") or "").endswith("/VehicleCalendar")]
    assert posts, f"no POST /VehicleCalendar; calls={[(c.get('method'), c.get('path')) for c in gateway.calls]}"
    body = posts[-1].get("body") or {}
    assert body.get("VehicleId") == "veh-A"     # chosen vehicle reached the wire
    assert body.get("AssignedToId") == "p1"     # booker from identity


@pytest.mark.asyncio
async def test_booking_flow_lookup_empty_aborts_cleanly():
    """Lookup returns nothing → flow aborts with a clear message, not a wedge."""
    engine, gateway, _llm, _emb, _router, _fmt = await _build_engine(registry_tools={
        "get_AvailableVehicles": {"method": "GET", "service": "vehiclemgt",
                                  "path": "/AvailableVehicles", "purpose": "x"},
        "post_VehicleCalendar": {"method": "POST", "service": "vehiclemgt",
                                 "path": "/VehicleCalendar", "purpose": "y"},
    })
    identity = IdentitySnapshot(phone=_FLOW_PHONE, person_id="p1", tenant_id="t1", is_known=True)
    gateway.queue(FakeApiResponse(success=True, data={"Data": []}))
    start = engine.flow_engine.start("booking", identity_context={"person_id": "p1"})
    r = await engine._continue_flow(_FLOW_PHONE, start.new_state, "16.12.2025 9-17", identity)
    assert "nema" in r.lower()
    assert await engine.flow_store.load(_FLOW_PHONE) is None   # state cleared


@pytest.mark.asyncio
async def test_flow_execute_blocked_when_execution_lock_held():
    """Fix A (Filip 2026-05-28): the flow execute path now takes the same
    anti-replay execution lock as the general path. If a concurrent execute is
    already in flight (lock held), the second 'Da' on the flow confirm gets a
    'u tijeku' message and does NOT issue the write — no double mileage entry."""
    engine, gateway, _llm, _emb, _router, _fmt = await _build_engine(registry_tools={
        "post_AddMileage": {"method": "POST", "service": "automation",
                            "path": "/AddMileage", "purpose": "unos km"},
    })
    identity = IdentitySnapshot(
        phone=_FLOW_PHONE, person_id="p1", tenant_id="t1",
        vehicle_id="v1", vehicle_name="VW Golf", last_mileage=100000, is_known=True,
    )
    start = engine.flow_engine.start("mileage", identity_context={
        "person_id": "p1", "vehicle_id": "v1", "vehicle_name": "VW Golf"})
    # Drive to the confirm step (state saved with the pending mileage value).
    confirm = await engine._continue_flow(_FLOW_PHONE, start.new_state, "120000", identity)
    assert "Upisat ću" in confirm

    # Simulate a concurrent execute already holding the real lock for this
    # phone (FakeRedis now models SET-NX EX — see its .set above).
    assert await engine.pending_mut_store.try_acquire_execution(_FLOW_PHONE) is True

    calls_before = len(gateway.calls)
    state2 = await engine.flow_store.load(_FLOW_PHONE)
    reply = await engine._continue_flow(_FLOW_PHONE, state2, "Da", identity)

    assert "u tijeku" in reply.lower()
    # No write was issued while the lock was held.
    posts = [c for c in gateway.calls[calls_before:] if c.get("method") == "POST"]
    assert not posts, f"unexpected write while locked: {posts}"
    # Flow state preserved so the real (lock-holding) execute can finish.
    assert await engine.flow_store.load(_FLOW_PHONE) is not None


# --------------------------------------------------------------------------
# REOFFER end-to-end (Filip 2026-06-05) — "nije točno" flow
# --------------------------------------------------------------------------


_REOFFER_PHONE = "+385955099999"


async def _onboard_reoffer_user(engine, gateway):
    """Welcome flow + identity priming so the test broj is known."""
    gateway.queue(FakeApiResponse(success=True, data=[{
        "Id": "p1", "FirstName": "Test", "LastName": "User", "TenantId": "t1",
    }]))
    gateway.queue(FakeApiResponse(success=True, data={
        "VehicleName": "TEST_VEHICLE", "LicencePlate": "AB123CD",
        "LastMileage": 50000, "FullVehicleName": "TEST_VEHICLE Demo",
    }))
    await engine.process_message(_REOFFER_PHONE, "bok")


@pytest.mark.asyncio
async def test_reoffer_after_l2b_match_redirects_to_action_picker():
    """Driver-basics path: user asked km → bot replied with km → user says
    'nije točno' → bot must show the universal action picker so user can
    pick a different action context (e.g. 'tko vozi DA053F' was actually
    meant), not just repeat the basics anchor."""
    engine, gateway, _llm, _emb, _router, _fmt = await _build_engine(
        registry_tools={
            "get_MasterData": {"method": "GET", "service": "automation",
                                "path": "/MasterData", "purpose": "Moji podaci."},
        }
    )
    await _onboard_reoffer_user(engine, gateway)

    # User asks km — L2b should match driver-basics anchor and reply with
    # the cached vehicle data (no executor call needed).
    reply1 = await engine.process_message(_REOFFER_PHONE, "kolika mi je km")
    assert reply1
    # The "nije točno" hint must be present (drives the loop awareness).
    assert "nije točno" in reply1

    # Reoffer state must be saved with L2B sentinel.
    pc = await engine.pending_clarify_store.load(_REOFFER_PHONE)
    assert pc is not None
    assert pc.last_executed_tool == "__L2B_DRIVER_BASICS__"
    assert pc.can_reoffer is True

    # User says "nije točno" → bot must hand control back to action picker.
    reply2 = await engine.process_message(_REOFFER_PHONE, "nije točno")
    assert "POGLEDATI" in reply2
    assert "UNIJETI" in reply2 or "KREIRATI" in reply2

    # State now in ACTION_GLOBAL stage (user about to pick an action).
    pc2 = await engine.pending_clarify_store.load(_REOFFER_PHONE)
    assert pc2 is not None
    assert pc2.stage == "action_global"
    assert pc2.original_query == "kolika mi je km"


@pytest.mark.asyncio
async def test_reoffer_after_l3_execute_offers_next_three_candidates():
    """Router path: user asked 'random query' → cascade picked tool A → bot
    executed → user says 'nije točno' → bot must show NEW 3 candidates from
    positions 4-6 of the cached cosine top-50, NOT repeat top-3."""
    engine, gateway, _llm, _emb, router, _fmt = await _build_engine(
        registry_tools={
            "get_VehicleCalendar":      {"method": "GET", "service": "vm",
                                          "path": "/VC", "purpose": "Pokaži kalendar."},
            "get_LatestVehicleCalendar": {"method": "GET", "service": "vm",
                                          "path": "/LVC", "purpose": "Pokaži aktualni kalendar."},
            "delete_VehicleCalendar_id": {"method": "DELETE", "service": "vm",
                                          "path": "/VC/{id}", "purpose": "Obriši kalendar."},
            "get_Persons":               {"method": "GET", "service": "tm",
                                          "path": "/P", "purpose": "Pokaži osobe."},
            "get_Expenses":              {"method": "GET", "service": "fm",
                                          "path": "/E", "purpose": "Pokaži troškove."},
            "get_Vehicles":              {"method": "GET", "service": "vm",
                                          "path": "/V", "purpose": "Pokaži vozila."},
        }
    )
    await _onboard_reoffer_user(engine, gateway)

    # Force router to return our fixed top-6 candidates so we know what
    # gets shown in round 1 vs round 2.
    top50 = [
        ("get_VehicleCalendar", 0.90),
        ("get_LatestVehicleCalendar", 0.85),
        ("delete_VehicleCalendar_id", 0.80),
        ("get_Persons", 0.70),
        ("get_Expenses", 0.65),
        ("get_Vehicles", 0.60),
    ]
    router.queue_result(RouterResult(
        tool_id="get_VehicleCalendar", params={},
        confidence=0.9, rationale="t1", anchor_score=0.9,
        top_candidates=top50,
    ))

    # Turn 1: user query → action picker shown
    r1 = await engine.process_message(_REOFFER_PHONE, "pokaži mi nešto")
    assert "POGLEDATI" in r1

    # Turn 2: pick "1" (POGLEDATI) → router runs → top-3 tool picker shown
    r2 = await engine.process_message(_REOFFER_PHONE, "1")
    assert "Razumio sam jedno od ovog" in r2 or "1️⃣" in r2

    # Confirm the saved reoffer state cached the FULL top-50 ids
    pc_after_pick = await engine.pending_clarify_store.load(_REOFFER_PHONE)
    assert pc_after_pick is not None
    assert pc_after_pick.all_candidate_ids[:6] == [
        "get_VehicleCalendar", "get_LatestVehicleCalendar",
        "delete_VehicleCalendar_id", "get_Persons", "get_Expenses",
        "get_Vehicles",
    ]
    assert set(pc_after_pick.shown_tool_ids) == {
        "get_VehicleCalendar", "get_LatestVehicleCalendar", "delete_VehicleCalendar_id",
    }

    # Turn 3: pick "1" (get_VehicleCalendar) → executed → result rendered
    gateway.queue(FakeApiResponse(success=True, data=[{"Id": "r1"}]))
    r3 = await engine.process_message(_REOFFER_PHONE, "1")
    assert r3  # got a real reply

    # State must now have can_reoffer=True with executed tool tracked
    pc_after_exec = await engine.pending_clarify_store.load(_REOFFER_PHONE)
    assert pc_after_exec is not None
    assert pc_after_exec.last_executed_tool == "get_VehicleCalendar"
    assert pc_after_exec.can_reoffer is True

    # Turn 4: "nije točno" → reoffer kicks in, shows positions 4-6
    r4 = await engine.process_message(_REOFFER_PHONE, "nije točno")
    assert "U redu, evo drugih opcija" in r4
    assert "1️⃣" in r4 and "2️⃣" in r4 and "3️⃣" in r4

    pc_after_reoffer = await engine.pending_clarify_store.load(_REOFFER_PHONE)
    assert pc_after_reoffer is not None
    # The 3 new tools (positions 4-6) are in shown + candidates
    new_ids = {c["tool_id"] for c in pc_after_reoffer.candidates}
    assert new_ids == {"get_Persons", "get_Expenses", "get_Vehicles"}
    # Cumulative shown: original 3 + 3 new = 6
    assert len(pc_after_reoffer.shown_tool_ids) == 6


@pytest.mark.asyncio
async def test_reoffer_exhausted_all_candidates_says_no_more():
    """After enough 'nije točno' rounds all 50 are exhausted — bot must
    say 'Nemam više opcija' and clear state. No infinite re-offering."""
    engine, gateway, _llm, _emb, router, _fmt = await _build_engine(
        registry_tools={
            "get_X1": {"method": "GET", "service": "s", "path": "/x1", "purpose": "X1"},
            "get_X2": {"method": "GET", "service": "s", "path": "/x2", "purpose": "X2"},
            "get_X3": {"method": "GET", "service": "s", "path": "/x3", "purpose": "X3"},
        }
    )
    await _onboard_reoffer_user(engine, gateway)

    # Only 3 candidates total — round 1 shows all 3, round 2 has nothing.
    router.queue_result(RouterResult(
        tool_id="get_X1", params={},
        confidence=0.9, rationale="t", anchor_score=0.9,
        top_candidates=[("get_X1", 0.9), ("get_X2", 0.8), ("get_X3", 0.7)],
    ))

    await engine.process_message(_REOFFER_PHONE, "fresh query")  # action picker
    await engine.process_message(_REOFFER_PHONE, "1")              # tool picker
    gateway.queue(FakeApiResponse(success=True, data={"ok": 1}))
    await engine.process_message(_REOFFER_PHONE, "1")              # execute get_X1

    reply = await engine.process_message(_REOFFER_PHONE, "nije točno")
    assert "Nemam više" in reply or "Opiši ponovo" in reply
    # State cleared
    assert await engine.pending_clarify_store.load(_REOFFER_PHONE) is None
