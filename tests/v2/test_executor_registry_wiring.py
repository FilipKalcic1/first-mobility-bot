"""Regression: the REAL ToolRegistry must satisfy the executor's contract.

services/v2/executor.py calls self._registry.spec_for(tool_id) and
method_of(tool_id). The 2026-05-09 simplify pass rewrote ToolRegistry and
dropped BOTH methods, so EVERY tool execution crashed with AttributeError in
production — yet the suite stayed green because test_engine_wireup.py injects a
FAKE registry that has them. These tests wire the REAL ToolRegistry to the REAL
ToolExecutor so that gap can never regress again (Filip 2026-05-22).
"""
from __future__ import annotations

import pytest

from services.api_gateway import APIResponse
from services.registry import ToolRegistry
from services.tool_contracts import ParameterDefinition, UnifiedToolDefinition
from services.v2.executor import ToolExecutor


def _tool(op_id="get_Persons", method="GET", service="tenantmgt", path="/Persons"):
    return UnifiedToolDefinition(
        operation_id=op_id, service_name=service, service_url=f"/{service}",
        path=path, method=method,
    )


def _registry_with(*tools) -> ToolRegistry:
    reg = ToolRegistry()
    for t in tools:
        reg._store.add_tool(t)
    return reg


class _RecordingGateway:
    def __init__(self, response: APIResponse):
        self._response = response
        self.calls: list[dict] = []

    async def call(self, *, method, service, path,
                   query_params=None, body=None, tenant_id=None):
        self.calls.append({
            "method": method, "service": service, "path": path,
            "query_params": query_params, "body": body, "tenant_id": tenant_id,
        })
        return self._response


def test_real_registry_exposes_executor_contract():
    """The real ToolRegistry MUST expose spec_for + method_of — the executor
    calls both. Without them, production crashed with AttributeError."""
    reg = ToolRegistry()
    assert hasattr(reg, "spec_for"), "ToolRegistry missing spec_for (executor needs it)"
    assert hasattr(reg, "method_of"), "ToolRegistry missing method_of (executor needs it)"


def test_spec_for_shape_matches_what_executor_reads():
    reg = _registry_with(_tool())
    spec = reg.spec_for("get_Persons")
    assert spec is not None
    # executor.py reads exactly these keys:
    assert spec["service"] == "tenantmgt"
    assert spec["path"] == "/Persons"
    assert spec["method"] == "GET"
    assert spec["tenant_scoped"] is True
    assert reg.spec_for("does_not_exist") is None
    assert reg.method_of("get_Persons") == "GET"
    assert reg.method_of("does_not_exist") is None


@pytest.mark.asyncio
async def test_execute_with_real_registry_reaches_gateway():
    """End-to-end: real registry + real executor → gateway gets a valid call.
    This is the path that crashed in production."""
    reg = _registry_with(_tool())
    gw = _RecordingGateway(APIResponse(success=True, status_code=200, data={"ok": 1}))
    ex = ToolExecutor(gw, reg)

    res = await ex.execute(
        tool_id="get_Persons", params={"Rows": 5},
        identity_summary={"tenant_id": "tenant-123"},
    )

    assert res.success is True
    assert res.data == {"ok": 1}
    assert len(gw.calls) == 1
    c = gw.calls[0]
    assert c["service"] == "tenantmgt"
    assert c["path"] == "/Persons"
    assert c["method"] == "GET"
    assert c["tenant_id"] == "tenant-123"
    assert c["query_params"] == {"Rows": 5}  # GET → query params
    assert c["body"] is None


@pytest.mark.asyncio
async def test_execute_post_sends_body_not_query():
    reg = _registry_with(_tool(op_id="post_AddMileage", method="POST",
                               service="automation", path="/AddMileage"))
    gw = _RecordingGateway(APIResponse(success=True, status_code=200, data={"Id": 7}))
    ex = ToolExecutor(gw, reg)

    res = await ex.execute(
        tool_id="post_AddMileage", params={"Value": 145000},
        identity_summary={"tenant_id": "t1"},
    )
    assert res.success is True
    c = gw.calls[0]
    assert c["body"] == {"Value": 145000}  # non-GET → body
    assert c["query_params"] is None


@pytest.mark.asyncio
async def test_execute_substitutes_path_param_into_url():
    """Fix #2: by-id tools (path `/Roles/{id}`, param id location=path) must
    substitute {id} into the URL — not leave a literal {id} and dump id in the
    query. Previously every by-id tool built `/Roles/{id}` → 404."""
    tool = UnifiedToolDefinition(
        operation_id="get_Roles_id", service_name="tenantmgt",
        service_url="/tenantmgt", path="/Roles/{id}", method="GET",
        parameters={
            "id": ParameterDefinition(name="id", param_type="string", location="path"),
            "Rows": ParameterDefinition(name="Rows", param_type="integer", location="query"),
        },
    )
    gw = _RecordingGateway(APIResponse(success=True, status_code=200, data={"Id": "abc"}))
    ex = ToolExecutor(gw, _registry_with(tool))

    res = await ex.execute(
        tool_id="get_Roles_id", params={"id": "abc-123", "Rows": 5},
        identity_summary={"tenant_id": "t1"},
    )
    assert res.success is True
    c = gw.calls[0]
    assert c["path"] == "/Roles/abc-123"          # {id} substituted
    assert "{id}" not in c["path"]
    assert c["query_params"] == {"Rows": 5}        # query stays in query
    assert "id" not in (c["query_params"] or {})   # id NOT leaked to query


@pytest.mark.asyncio
async def test_execute_refuses_when_path_param_missing():
    """If a {placeholder} can't be filled, refuse rather than send /Roles/{id}."""
    tool = UnifiedToolDefinition(
        operation_id="get_Roles_id", service_name="tenantmgt",
        service_url="/tenantmgt", path="/Roles/{id}", method="GET",
        parameters={"id": ParameterDefinition(name="id", param_type="string", location="path")},
    )
    gw = _RecordingGateway(APIResponse(success=True, status_code=200, data={}))
    ex = ToolExecutor(gw, _registry_with(tool))

    res = await ex.execute(tool_id="get_Roles_id", params={}, identity_summary={"tenant_id": "t1"})
    assert res.success is False
    assert res.error == "missing_path_param"
    assert gw.calls == []  # never reached the gateway with a broken URL


@pytest.mark.asyncio
async def test_execute_injects_context_param_from_identity():
    """Fix #3: a `context` param (VehicleId, dependency_source=context,
    context_key=vehicle_id) is auto-injected from identity — never asked from
    the user. Without it, post_AddMileage hit the API with no VehicleId → 422."""
    tool = UnifiedToolDefinition(
        operation_id="post_AddMileage", service_name="automation",
        service_url="/automation", path="/AddMileage", method="POST",
        parameters={
            "VehicleId": ParameterDefinition(
                name="VehicleId", param_type="string", location="body",
                dependency_source="context", context_key="vehicle_id",
            ),
            "Value": ParameterDefinition(
                name="Value", param_type="integer", location="body",
                dependency_source="user_input",
            ),
        },
    )
    gw = _RecordingGateway(APIResponse(success=True, status_code=200, data={"Id": 9}))
    ex = ToolExecutor(gw, _registry_with(tool))

    res = await ex.execute(
        tool_id="post_AddMileage", params={"Value": 145000},  # user gave km only
        identity_summary={"tenant_id": "t1", "vehicle_id": "veh-9", "person_id": "p1"},
    )
    assert res.success is True
    body = gw.calls[0]["body"]
    assert body["Value"] == 145000        # user_input param
    assert body["VehicleId"] == "veh-9"   # context param injected from identity


@pytest.mark.asyncio
async def test_execute_context_param_not_overwritten_if_user_gave_it():
    """If the user/LLM already supplied a context param, don't clobber it."""
    tool = UnifiedToolDefinition(
        operation_id="post_AddMileage", service_name="automation",
        service_url="/automation", path="/AddMileage", method="POST",
        parameters={
            "VehicleId": ParameterDefinition(
                name="VehicleId", param_type="string", location="body",
                dependency_source="context", context_key="vehicle_id",
            ),
        },
    )
    gw = _RecordingGateway(APIResponse(success=True, status_code=200, data={}))
    ex = ToolExecutor(gw, _registry_with(tool))
    res = await ex.execute(
        tool_id="post_AddMileage", params={"VehicleId": "explicit-veh"},
        identity_summary={"tenant_id": "t1", "vehicle_id": "veh-9"},
    )
    assert res.success is True
    assert gw.calls[0]["body"]["VehicleId"] == "explicit-veh"  # not overwritten


@pytest.mark.asyncio
async def test_execute_unknown_tool_returns_error_not_crash():
    ex = ToolExecutor(_RecordingGateway(APIResponse(success=True, status_code=200, data={})),
                      ToolRegistry())
    res = await ex.execute(tool_id="nope", params={}, identity_summary={"tenant_id": "t"})
    assert res.success is False
    assert "unknown_tool" in (res.error or "")


@pytest.mark.asyncio
async def test_execute_missing_tenant_refused():
    reg = _registry_with(_tool())
    ex = ToolExecutor(_RecordingGateway(APIResponse(success=True, status_code=200, data={})), reg)
    res = await ex.execute(tool_id="get_Persons", params={}, identity_summary={})
    assert res.success is False
    assert res.error == "missing_tenant_id"
