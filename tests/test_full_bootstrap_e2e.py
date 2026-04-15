"""
Acid Test — Option C end-to-end bootstrap (DB-backed resolver).

Proves the chain from inbound WhatsApp number to outbound API call:

    +385955087196  (seeded into in-memory user_mappings)
        ↓
    services.tenant_resolver.resolve_tenant_for_phone(...)
        → Redis cache → Postgres user_mappings.tenant_id
        ↓
    [worker writes session:{phone}:tenant_id, engine seeds user_context]
        ↓
    services.api_gateway.APIGateway.execute(..., tenant_id=<from DB>)
        ↓
    OUTBOUND HTTP call → headers carry x-tenant = <DB value>

…and the dependency_resolver chain proceeds:
    get_Persons   (x-tenant = DB value)  →  PersonId
    get_MasterData (x-tenant = DB value)  →  VehicleId
    post_AddMileage payload assembled from aliases.

Acid criterion (Damir): the FIRST tenant-scoped call MUST already carry the
TenantId from the user_mappings table — never from env, never from a chicken-
and-egg loop.

Pre-requisites:
    python scripts/migrate_registry_v6.py
    python scripts/migrate_registry_v6_1.py

Run:
    pytest tests/test_full_bootstrap_e2e.py -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from services.api_gateway import APIGateway, APIResponse, HttpMethod  # noqa: E402
from services.dependency_resolver.resolver import DependencyResolver  # noqa: E402
from services.tenant_resolver import (  # noqa: E402
    TenantResolver,
    resolve_tenant_for_phone,
    set_tenant_resolver_for_tests,
)
from services.tool_contracts import UnifiedToolDefinition  # noqa: E402


# ---------------------------------------------------------------------------
# Constants — fixture data, no production secrets
# ---------------------------------------------------------------------------

REGISTRY_PATH = ROOT / "config" / "processed_tool_registry.json"

DAMIR_PHONE = "+385955087196"
SEEDED_TENANT_ID = "tenant-from-user-mappings-table"


# ---------------------------------------------------------------------------
# In-memory DB + Redis fixtures (mirror test_tenant_resolver.py)
# ---------------------------------------------------------------------------

CREATE_USER_MAPPINGS_SQL = """
CREATE TABLE user_mappings (
    id           TEXT PRIMARY KEY,
    phone_number TEXT NOT NULL UNIQUE,
    tenant_id    TEXT,
    is_active    INTEGER NOT NULL DEFAULT 1
)
"""


class FakeRedis:
    def __init__(self) -> None:
        self.store: Dict[str, str] = {}

    async def get(self, key: str):
        return self.store.get(key)

    async def set(self, key: str, value: Any, ex=None) -> bool:
        self.store[key] = str(value)
        return True

    async def delete(self, *keys: str) -> int:
        n = 0
        for k in keys:
            if k in self.store:
                del self.store[k]
                n += 1
        return n


@pytest.fixture
async def seeded_db_and_resolver():
    """
    Build an in-memory aiosqlite DB containing user_mappings with Damir's
    seeded row, install a TenantResolver singleton wired to it, yield to test.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.execute(text(CREATE_USER_MAPPINGS_SQL))

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as session:
        await session.execute(
            text(
                "INSERT INTO user_mappings (id, phone_number, tenant_id, is_active) "
                "VALUES (:id, :p, :t, 1)"
            ),
            {"id": "row-damir", "p": DAMIR_PHONE, "t": SEEDED_TENANT_ID},
        )
        await session.commit()

    redis = FakeRedis()
    resolver = TenantResolver(session_factory=factory, redis_client=redis)
    set_tenant_resolver_for_tests(resolver)
    try:
        yield {"factory": factory, "redis": redis, "resolver": resolver}
    finally:
        set_tenant_resolver_for_tests(None)
        await engine.dispose()


# ---------------------------------------------------------------------------
# Tool registry helpers
# ---------------------------------------------------------------------------

def _load_tool(op_id: str) -> UnifiedToolDefinition:
    if not REGISTRY_PATH.exists():
        pytest.fail(
            f"{REGISTRY_PATH} missing — run migrate_registry_v6.py + migrate_registry_v6_1.py first."
        )
    raw = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    tools = raw["tools"] if isinstance(raw, dict) and "tools" in raw else raw
    for entry in tools:
        if entry.get("operation_id") == op_id:
            return UnifiedToolDefinition(**entry)
    pytest.fail(f"operation_id '{op_id}' missing in {REGISTRY_PATH}")


class _FakeRegistry:
    def __init__(self, tools: Dict[str, UnifiedToolDefinition]) -> None:
        self._tools = tools

    def get_all_tools(self) -> Dict[str, UnifiedToolDefinition]:
        return self._tools

    def get_tool(self, op_id: str) -> UnifiedToolDefinition | None:
        return self._tools.get(op_id)


# ---------------------------------------------------------------------------
# Gateway helpers (real APIGateway, init bypassed)
# ---------------------------------------------------------------------------

def _build_bare_gateway() -> APIGateway:
    gw = APIGateway.__new__(APIGateway)
    gw.base_url = "http://test.invalid"
    gw.tenant_id = "ENV-TENANT-MUST-NEVER-LEAK"  # if this surfaces, V6.2 broke
    gw._consecutive_failures = 0
    gw._circuit_open_until = 0.0

    class _FakeTokenMgr:
        async def get_token(self) -> str:
            return "fake-sso-token"

        async def invalidate(self) -> None:
            pass

    gw.token_manager = _FakeTokenMgr()
    return gw


class _StubHttpResponse:
    """Minimal stand-in for httpx.Response (gateway only reads .status_code)."""
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code


# ---------------------------------------------------------------------------
# PHASE 1 — DB lookup via the singleton resolver
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_phase1_resolver_returns_seeded_tenant(seeded_db_and_resolver):
    tid = await resolve_tenant_for_phone(DAMIR_PHONE)
    assert tid == SEEDED_TENANT_ID, (
        f"resolver returned {tid!r} for {DAMIR_PHONE}; "
        f"expected the value seeded into user_mappings."
    )


@pytest.mark.asyncio
async def test_phase1_unknown_phone_returns_none_not_env(seeded_db_and_resolver):
    assert await resolve_tenant_for_phone("+15555550000") is None


@pytest.mark.asyncio
async def test_phase1_cache_warms_on_hit(seeded_db_and_resolver):
    """Second call should be served from Redis (no DB I/O)."""
    redis: FakeRedis = seeded_db_and_resolver["redis"]
    await resolve_tenant_for_phone(DAMIR_PHONE)
    assert f"tenant_phone:{DAMIR_PHONE}" in redis.store


# ---------------------------------------------------------------------------
# PHASE 2 — gateway honors the tenant from the resolver
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_phase2_gateway_emits_x_tenant_from_db_value(seeded_db_and_resolver):
    gw = _build_bare_gateway()
    captured: Dict[str, Any] = {}

    async def fake_do_request(method, url, headers, body):
        captured["headers"] = dict(headers)
        captured["url"] = url
        return _StubHttpResponse(200)

    def fake_parse(_resp):
        return APIResponse(success=True, status_code=200, data={"ok": True})

    gw._do_request = fake_do_request          # type: ignore[assignment]
    gw._parse_response = fake_parse           # type: ignore[assignment]

    db_tenant = await resolve_tenant_for_phone(DAMIR_PHONE)
    assert db_tenant == SEEDED_TENANT_ID

    resp = await gw.execute(
        method=HttpMethod.GET,
        path="/tenantmgt/Persons",
        params={"Filter": f"Phone(=){DAMIR_PHONE}"},
        tenant_id=db_tenant,
    )

    assert resp.success is True, f"gateway refused tenant'd call: {resp.error_message}"
    sent_headers = {k.lower(): v for k, v in (captured.get("headers") or {}).items()}
    assert sent_headers.get("x-tenant") == SEEDED_TENANT_ID, sent_headers
    assert sent_headers.get("x-tenant") != "ENV-TENANT-MUST-NEVER-LEAK"


# ---------------------------------------------------------------------------
# PHASE 3 — full chain: DB tenant → x-tenant on every call → aliased payload
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_phase3_full_chain_persons_masterdata_addmileage(seeded_db_and_resolver):
    persons = _load_tool("get_Persons")
    masterdata = _load_tool("get_MasterData")
    addmileage = _load_tool("post_AddMileage")

    registry = _FakeRegistry({
        "get_Persons": persons,
        "get_MasterData": masterdata,
        "post_AddMileage": addmileage,
    })
    resolver = DependencyResolver(registry=registry)

    gw = _build_bare_gateway()
    requests: List[Tuple[str, str, Dict[str, str]]] = []

    async def fake_do_request(method, url, headers, body):
        requests.append((str(method), url, dict(headers)))
        return _StubHttpResponse(200)

    def _response_for(url: str) -> APIResponse:
        if "/tenantmgt/Persons" in url:
            # In production, Persons.TenantId IS the same value the DB
            # bootstrapped us with. The DB row mirrors what the API reports.
            return APIResponse(success=True, status_code=200, data={
                "Data": [{
                    "Id": "osoba-123",
                    "TenantId": SEEDED_TENANT_ID,
                    "Phone": DAMIR_PHONE,
                }]
            })
        if "/automation/MasterData" in url:
            return APIResponse(success=True, status_code=200, data=[
                {"Id": "auto-777", "LicencePlate": "ZG-1234-AB"}
            ])
        return APIResponse(success=True, status_code=200, data={"ok": True})

    def fake_parse(_resp):
        last_url = requests[-1][1] if requests else ""
        return _response_for(last_url)

    gw._do_request = fake_do_request    # type: ignore[assignment]
    gw._parse_response = fake_parse     # type: ignore[assignment]

    db_tenant = await resolve_tenant_for_phone(DAMIR_PHONE)
    assert db_tenant == SEEDED_TENANT_ID

    session_context: Dict[str, Any] = {
        "phone": DAMIR_PHONE,
        # webhook+worker+engine seed both casings; gateway accepts either.
        "TenantId": db_tenant,
        "tenant_id": db_tenant,
    }

    async def gateway_executor(op_id: str, params: Dict[str, Any]) -> Any:
        tool = registry.get_tool(op_id)
        assert tool is not None, f"unknown op {op_id}"
        full_path = f"{tool.service_url}{tool.path}"
        tenant_for_call = session_context.get("TenantId")
        if tool.method.upper() == "GET":
            resp = await gw.execute(
                method=HttpMethod.GET,
                path=full_path,
                params=params,
                tenant_id=tenant_for_call,
                context=session_context,
            )
        else:
            resp = await gw.execute(
                method=HttpMethod[tool.method.upper()],
                path=full_path,
                params=None,
                body=params,
                tenant_id=tenant_for_call,
                context=session_context,
            )
        assert resp.success, f"{op_id} refused: {resp.error_message}"
        return resp.data

    user_input: Dict[str, Any] = {"Value": 500}

    payload = await resolver.prepare_params_for_tool(
        target_tool=addmileage,
        context=session_context,
        executor=gateway_executor,
        user_input=user_input,
    )

    # ── DOKAZ 1: AddMileage payload assembled correctly through the chain
    assert payload.get("VehicleId") == "auto-777", payload
    assert payload.get("Value") == 500, payload

    # ── DOKAZ 2: gateway dispatched both bootstrap calls in order
    urls_in_order = [u for (_m, u, _h) in requests]
    persons_idx = next(i for i, u in enumerate(urls_in_order) if "/Persons" in u)
    master_idx = next(i for i, u in enumerate(urls_in_order) if "/MasterData" in u)
    assert persons_idx < master_idx, urls_in_order

    # ── DOKAZ 3: x-tenant on EVERY outbound call equals DB-seeded value
    for method, url, headers in requests:
        h = {k.lower(): v for k, v in headers.items()}
        assert h.get("x-tenant") == SEEDED_TENANT_ID, (
            f"call {method} {url} carried wrong x-tenant: {h.get('x-tenant')!r}"
        )
        assert h.get("x-tenant") != "ENV-TENANT-MUST-NEVER-LEAK", (
            f"env tenant leaked on {method} {url}"
        )

    # ── DOKAZ 4: bootstrap Persons call carried the DB tenant FIRST
    persons_headers = {k.lower(): v for k, v in requests[persons_idx][2].items()}
    assert persons_headers.get("x-tenant") == SEEDED_TENANT_ID

    # ── DOKAZ 5: aliases populated context for downstream consumers
    assert session_context.get("PersonId") == "osoba-123"
    assert session_context.get("VehicleId") == "auto-777"
