"""End-to-end conversation test for THE core system task:

    User (WhatsApp): "Želim vidjeti moja posljednja putovanja"
      → bot recognizes the intent (Model A cascade over the REAL
        950-tool registry in config/tool_data.json)
      → bot calls EXACTLY the right MobilityOne endpoint
        (GET /vehiclemgt/Trips with the user's resolved tenant)
      → bot answers in Croatian with the real data.

Everything external is faked (Azure LLM, Azure embeddings, MobilityOne
gateway, Redis) but the SYSTEM is real: the production engine factory,
the real tool_data.json registry, the real dispatch/cascade/executor
logic. This is the closest a test can get to proving "zadaća sustava"
without live credentials.

Fake-embedding trick: bag-of-words vectors (hash word → dim). Cosine then
correlates with word overlap, so anchor retrieval ("moja posljednja
putovanja" vs the registry's real "želim vidjeti sva putovanja" anchors)
behaves like a weak-but-sane retriever; the fake router LLM then picks
get_Trips from the candidate schemas the same way gpt-4o-mini would.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path

import pytest

from services.v2.engine import make_v2_engine_for_production

ROOT = Path(__file__).resolve().parents[2]
ANCHOR_CACHE = ROOT / "tests" / "benchmarks" / "router_anchor_cache.json"

PHONE = "385955087196"
TENANT = "tenant-e2e-X"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeRedis:
    """Stateful in-memory Redis — pending states must survive between turns."""

    def __init__(self):
        self.kv: dict = {}
        self.lists: dict = {}

    async def get(self, key):
        return self.kv.get(key)

    async def set(self, key, value, nx=False, ex=None, **kw):
        if nx and key in self.kv:
            return None
        self.kv[key] = value
        return True

    async def setex(self, key, ttl, value):
        self.kv[key] = value
        return True

    async def delete(self, *keys):
        n = 0
        for k in keys:
            if self.kv.pop(k, None) is not None:
                n += 1
        return n

    async def expire(self, *a, **kw):
        return True

    async def exists(self, key):
        return 1 if key in self.kv else 0

    async def incr(self, key):
        self.kv[key] = int(self.kv.get(key, 0)) + 1
        return self.kv[key]

    async def incrby(self, key, n=1):
        self.kv[key] = int(self.kv.get(key, 0)) + n
        return self.kv[key]

    async def rpush(self, key, *vals):
        self.lists.setdefault(key, []).extend(vals)
        return len(self.lists[key])

    async def lpush(self, key, *vals):
        self.lists.setdefault(key, [])
        for v in vals:
            self.lists[key].insert(0, v)
        return len(self.lists[key])

    async def ltrim(self, *a, **kw):
        return True

    async def lrange(self, key, start, end):
        lst = self.lists.get(key, [])
        end = len(lst) if end == -1 else end + 1
        return lst[start:end]

    async def script_load(self, *a, **kw):
        return "sha"

    async def evalsha(self, *a, **kw):
        return 1

    async def eval(self, *a, **kw):
        return 1

    async def zadd(self, *a, **kw):
        return 1

    async def zrangebyscore(self, *a, **kw):
        return []

    async def zremrangebyscore(self, *a, **kw):
        return 0


class FakeGatewayResponse:
    def __init__(self, success=True, data=None, status_code=200,
                 error_message=None):
        self.success = success
        self.data = data
        self.status_code = status_code
        self.error_message = error_message


class FakeGateway:
    """Routes by (method, service, path). Records every call so the test
    can assert the EXACT API call the bot composed."""

    def __init__(self):
        self.calls: list[dict] = []

    async def call(self, *, method, service, path, query_params=None,
                   body=None, tenant_id=None):
        self.calls.append({
            "method": method, "service": service, "path": path,
            "query_params": dict(query_params or {}), "body": body,
            "tenant_id": tenant_id,
        })
        if path == "/Persons":
            return FakeGatewayResponse(data={"Data": [{
                "Id": "person-77", "FirstName": "Filip", "LastName": "K",
                "Phone": PHONE, "TenantId": TENANT,
                "CompanyId": "comp-1", "CompanyName": "Damir d.o.o.",
                "OrgUnitId": "org-1", "OrgUnitName": "Vozni park",
            }]})
        if path == "/MasterData":
            return FakeGatewayResponse(data={
                "Id": "veh-5", "VehicleName": "VW Golf",
                "LicencePlate": "ZG-1234-AB", "LastMileage": 50000,
            })
        if path == "/AddMileage":
            return FakeGatewayResponse(data={"Id": "mil-1", "Value": (body or {}).get("Value")})
        if path == "/Trips":
            return FakeGatewayResponse(data={"Data": [
                {"Id": 1, "TripNo": "T-001", "Route": "Zagreb–Split",
                 "VehicleName": "VW Golf"},
                {"Id": 2, "TripNo": "T-002", "Route": "Zagreb–Rijeka",
                 "VehicleName": "VW Golf"},
            ], "Total": 2})
        return FakeGatewayResponse(
            success=False, status_code=404, error_message="not found",
        )


def _bow_vector(text: str, dim: int = 128) -> list[float]:
    """Bag-of-words embedding: cosine ≈ word overlap. Diacritics folded so
    'želim' and 'zelim' agree, mirroring real-embedding robustness."""
    folded = (text or "").lower()
    for a, b in (("š", "s"), ("ž", "z"), ("č", "c"), ("ć", "c"), ("đ", "d")):
        folded = folded.replace(a, b)
    vec = [0.0] * dim
    for w in re.findall(r"[a-z0-9]+", folded):
        h = int(hashlib.md5(w.encode()).hexdigest(), 16) % dim
        vec[h] += 1.0
    n = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / n for x in vec]


class _FakeEmbeddings:
    async def create(self, *, input, model):  # noqa: A002 — mirrors SDK kwarg
        texts = input if isinstance(input, list) else [input]
        data = [
            type("_E", (), {"embedding": _bow_vector(t)})() for t in texts
        ]
        return type("_R", (), {"data": data})()


class FakeEmbeddingClient:
    def __init__(self):
        self.embeddings = _FakeEmbeddings()


class _Msg:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


class _ToolCall:
    def __init__(self, name, arguments="{}"):
        self.function = type("_F", (), {"name": name, "arguments": arguments})()


def _resp(msg):
    return type("_R", (), {
        "choices": [type("_C", (), {"message": msg})()],
    })()


class FakeChatCompletions:
    """Dispatches on call shape:
      - kwargs contain `tools`   → router call → pick get_Trips if offered
      - system mentions klasifikator → intent JSON
      - otherwise                → formatter Croatian text
    """

    def __init__(self):
        self.router_calls: list[dict] = []
        self.formatter_calls: list[dict] = []

    async def create(self, **kwargs):
        if kwargs.get("tools"):
            self.router_calls.append(kwargs)
            offered = {
                t["function"]["name"] for t in kwargs["tools"]
            }
            assert "get_Trips" in offered, (
                "anchor retrieval must surface get_Trips in the candidate "
                f"schemas; offered sample: {sorted(offered)[:10]}"
            )
            return _resp(_Msg(tool_calls=[_ToolCall("get_Trips")]))
        system = (kwargs.get("messages") or [{}])[0].get("content") or ""
        if "klasifikator" in system:
            # Mirror real gpt-4o-mini: imperative flow verbs → flow_request
            # (the engine's flow gate is keyword AND classifier, 2-of-2).
            user = (kwargs.get("messages") or [{}, {}])[-1].get("content") or ""
            is_flow = any(
                w in user.lower()
                for w in ("upiši", "upisi", "rezerviraj", "prijavi")
            )
            return _resp(_Msg(content=json.dumps({
                "kind": "flow_request" if is_flow else "other",
                "confidence": 0.95,
            })))
        self.formatter_calls.append(kwargs)
        return _resp(_Msg(
            content="Imaš 2 putovanja: Zagreb–Split i Zagreb–Rijeka."
        ))


class FakeChatClient:
    def __init__(self):
        self.completions = FakeChatCompletions()
        self.chat = self


class FakeSettings:
    AZURE_OPENAI_DEPLOYMENT_NAME = "gpt-4o-mini"
    AZURE_OPENAI_EMBEDDING_DEPLOYMENT = "text-embedding-ada-002"
    MOBILITY_TENANT_ID = "env-tenant-should-not-leak"


class StubTenantResolver:
    """Unknown phone (None) → identity does the Persons lookup itself."""

    async def resolve_tenant_for_phone(self, phone, **kw):
        return None

    async def upsert_user_mapping(self, phone, **kw):
        return True


# ---------------------------------------------------------------------------
# The scenario
# ---------------------------------------------------------------------------


@pytest.fixture
async def system(monkeypatch):
    # A stale anchor cache (e.g. zero-vectors from another test run) would
    # defeat the bag-of-words retriever — fingerprint covers anchor CONTENT,
    # not embedding values.
    if ANCHOR_CACHE.exists():
        ANCHOR_CACHE.unlink()

    chat = FakeChatClient()
    monkeypatch.setattr(
        "services.openai_client.get_openai_client", lambda: chat,
    )
    monkeypatch.setattr(
        "services.openai_client.get_embedding_client", FakeEmbeddingClient,
    )

    async def _stub_resolver():
        return StubTenantResolver()
    monkeypatch.setattr(
        "services.tenant_resolver.get_tenant_resolver", _stub_resolver,
    )

    # REAL registry — the same processed_tool_registry.json production loads.
    from services.registry import ToolRegistry
    registry = ToolRegistry(redis_client=None)
    assert await registry.initialize(), "real registry must load"

    gateway = FakeGateway()
    bundle = await make_v2_engine_for_production(
        redis_client=FakeRedis(),
        gateway=gateway,
        tool_registry=registry,
        settings=FakeSettings(),
    )
    yield bundle.engine, gateway, chat
    if ANCHOR_CACHE.exists():
        ANCHOR_CACHE.unlink()


@pytest.mark.asyncio
async def test_user_asks_for_trips_bot_calls_exact_endpoint_and_answers(system):
    engine, gateway, chat = system

    # Turn 0 — first contact: welcome (identity resolved via /Persons).
    welcome = await engine.process_message(PHONE, "bok")
    assert "Filip" in welcome  # personalized greeting from resolved identity

    # Turn 1 — THE task: natural-language request.
    r1 = await engine.process_message(
        PHONE, "Želim vidjeti moja posljednja putovanja",
    )
    assert "POGLEDATI" in r1  # Model A Turn 1: universal action picker

    # Turn 2 — user picks POGLEDATI → scoped router → top-3 tool picker.
    r2 = await engine.process_message(PHONE, "1")
    assert "1" in r2 and "POGLEDATI" in r2
    assert len(chat.completions.router_calls) == 1  # exactly one routing LLM call

    # Turn 3 — user picks tool #1 (router's pick leads the card list)
    #          → GET tool auto-executes → Croatian answer from real data.
    r3 = await engine.process_message(PHONE, "1")
    assert "Zagreb–Split" in r3 and "Zagreb–Rijeka" in r3
    # The reply teaches the correction phrase (reoffer state was saved).
    assert "nije točno" in r3

    # --- THE核 claim: the API call was EXACTLY right -----------------------
    trips_calls = [c for c in gateway.calls if c["path"] == "/Trips"]
    assert len(trips_calls) == 1, f"expected one /Trips call, got {gateway.calls}"
    call = trips_calls[0]
    assert call["method"] == "GET"
    assert call["service"] == "vehiclemgt"
    assert call["tenant_id"] == TENANT, (
        "call must carry the USER'S resolved tenant — never the env default"
    )
    assert call["body"] is None  # GET has no body
    # No hallucinated filter params — Filter/UseANDFor are suppressed.
    assert not set(call["query_params"]) & {"Filter", "UseANDFor", "filter", "useANDFor"}

    # The formatter saw the REAL rows (grounding), not an empty envelope.
    fmt_payload = chat.completions.formatter_calls[-1]["messages"][-1]["content"]
    assert "Zagreb–Split" in fmt_payload


@pytest.mark.asyncio
async def test_wrong_route_recovers_via_nije_tocno_reoffer(system):
    """Follow-up loop: after an executed read, 'nije točno' must offer the
    NEXT candidates instead of repeating the same tool."""
    engine, gateway, chat = system

    await engine.process_message(PHONE, "bok")
    await engine.process_message(PHONE, "Želim vidjeti moja posljednja putovanja")
    await engine.process_message(PHONE, "1")   # POGLEDATI
    r3 = await engine.process_message(PHONE, "1")   # pick tool → executes
    assert "putovanja" in r3 or "Zagreb" in r3

    r4 = await engine.process_message(PHONE, "nije točno")
    # Bot offers alternatives (next candidates picker) — not silence, not
    # the same executed tool presented as done.
    assert "opcij" in r4.lower() or "1" in r4


@pytest.mark.asyncio
async def test_mutation_path_mileage_flow_confirm_and_exact_post(system):
    """The WRITE half of the core task: 'upiši 145000 km' →
    mileage flow pre-fills the value from the message → Da/Ne confirm →
    POST /automation/AddMileage with the context-injected VehicleId
    (driver never typed it — identity provides it) + the typed Value."""
    engine, gateway, chat = system

    await engine.process_message(PHONE, "bok")

    r1 = await engine.process_message(PHONE, "upiši 145000 km")
    # Value pre-filled from the message → flow skips straight to confirm.
    assert "145000" in r1
    assert "Da" in r1 and "Ne" in r1
    # Nothing written yet — confirm gate holds the mutation.
    assert not [c for c in gateway.calls if c["path"] == "/AddMileage"]

    r2 = await engine.process_message(PHONE, "Da")
    assert "uspješno" in r2.lower()

    posts = [c for c in gateway.calls if c["path"] == "/AddMileage"]
    assert len(posts) == 1, f"exactly one write expected, got {posts}"
    call = posts[0]
    assert call["method"] == "POST"
    assert call["service"] == "automation"
    assert call["tenant_id"] == TENANT
    body = call["body"] or {}
    assert body.get("Value") == 145000            # typed by the user
    assert body.get("VehicleId") == "veh-5"       # injected from identity
    assert call["query_params"] == {}             # body params stay in body


@pytest.mark.asyncio
async def test_mutation_declined_with_ne_never_calls_api(system):
    engine, gateway, chat = system

    await engine.process_message(PHONE, "bok")
    await engine.process_message(PHONE, "upiši 145000 km")
    r = await engine.process_message(PHONE, "Ne")

    assert "odusta" in r.lower()
    assert not [c for c in gateway.calls if c["path"] == "/AddMileage"]
