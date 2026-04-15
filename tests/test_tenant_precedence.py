"""
Strict Tenant Isolation (V6.2).

Rule: env MOBILITY_TENANT_ID is dev-test only. For arbitrary users, falling
back to it would route their data into the wrong tenant (cross-tenant write).
Therefore:

    explicit tenant_id kwarg   >   context.TenantId   >   REFUSE

No env fallback. Missing tenant => APIResponse with error_code
GATEWAY_MISSING_TENANT_CONTEXT.

Run:
    pytest tests/test_tenant_precedence.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from services.api_gateway import APIGateway, APIResponse, HttpMethod  # noqa: E402


def _bare_gateway(env_tenant: str) -> APIGateway:
    """
    Build an APIGateway without triggering config/env/httpx/redis init.
    Only fields needed by _execute_inner + _do_request hook are set.
    """
    gw = APIGateway.__new__(APIGateway)
    gw.base_url = "http://test.invalid"
    gw.tenant_id = env_tenant
    gw._consecutive_failures = 0
    gw._circuit_open_until = 0.0

    class _FakeTokenMgr:
        async def get_token(self) -> str:
            return "fake-token"

        async def invalidate(self) -> None:
            pass

    gw.token_manager = _FakeTokenMgr()
    return gw


def _install_capture(gw: APIGateway) -> dict:
    captured: dict = {}

    async def fake_do_request(method, url, headers, body):
        captured["headers"] = dict(headers)
        captured["url"] = url
        return None  # _parse_response is also patched below

    def fake_parse(_resp):
        return APIResponse(success=True, status_code=200, data={"ok": True})

    gw._do_request = fake_do_request  # type: ignore[assignment]
    gw._parse_response = fake_parse    # type: ignore[assignment]
    return captured


@pytest.mark.asyncio
async def test_missing_tenant_context_is_refused():
    """Cold call with no context and no kwarg => hard reject, no env fallback."""
    gw = _bare_gateway(env_tenant="ENV-TENANT")
    captured = _install_capture(gw)

    resp = await gw.execute(method=HttpMethod.GET, path="/MasterData", params={})

    assert resp.success is False
    assert resp.error_code == "GATEWAY_MISSING_TENANT_CONTEXT"
    assert "captured" not in captured or "headers" not in captured, (
        "request must not be dispatched when tenant is missing"
    )


@pytest.mark.asyncio
async def test_env_tenant_is_never_leaked_to_user_call():
    """Even though env is configured, it must not surface on any user call."""
    gw = _bare_gateway(env_tenant="BOSS-DEV-TENANT")
    captured = _install_capture(gw)

    resp = await gw.execute(
        method=HttpMethod.POST,
        path="/AddMileage",
        params={},
        body={"VehicleId": "v-1", "Value": 500},
    )

    assert resp.success is False
    assert resp.error_code == "GATEWAY_MISSING_TENANT_CONTEXT"
    assert captured.get("headers") is None


@pytest.mark.asyncio
async def test_session_context_wins_over_env():
    gw = _bare_gateway(env_tenant="ENV-TENANT")
    captured = _install_capture(gw)

    session_ctx = {"TenantId": "CTX-TENANT-FROM-PERSONS"}
    await gw.execute(
        method=HttpMethod.GET,
        path="/MasterData",
        params={},
        context=session_ctx,
    )

    assert captured["headers"]["x-tenant"] == "CTX-TENANT-FROM-PERSONS", (
        f"context TenantId should beat env. Got: {captured['headers']}"
    )


@pytest.mark.asyncio
async def test_explicit_kwarg_wins_over_context():
    gw = _bare_gateway(env_tenant="ENV-TENANT")
    captured = _install_capture(gw)

    session_ctx = {"TenantId": "CTX-TENANT"}
    await gw.execute(
        method=HttpMethod.GET,
        path="/MasterData",
        params={},
        context=session_ctx,
        tenant_id="EXPLICIT-OVERRIDE",
    )

    assert captured["headers"]["x-tenant"] == "EXPLICIT-OVERRIDE"


@pytest.mark.asyncio
async def test_snake_case_context_key_also_works():
    """Tolerate both 'TenantId' (Persons alias output) and 'tenant_id' (legacy)."""
    gw = _bare_gateway(env_tenant="ENV-TENANT")
    captured = _install_capture(gw)

    await gw.execute(
        method=HttpMethod.GET,
        path="/MasterData",
        params={},
        context={"tenant_id": "SNAKE-CTX"},
    )

    assert captured["headers"]["x-tenant"] == "SNAKE-CTX"
