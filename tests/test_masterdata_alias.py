"""
Laboratory proof that the V6 alias-aware Graph Discovery resolves
the full bootstrap chain end-to-end:

    {"phone": "+385955087196"}
        -> get_Persons (Filter='Phone(=)+385955087196')
        -> alias Id -> PersonId  (and seed TenantId)
    PersonId
        -> get_MasterData (personId=PersonId)
        -> alias Id -> VehicleId
    VehicleId + Value
        -> post_AddMileage payload contains VehicleId="auto-777"
           and the session context carries TenantId="t-99".

Run:
    python scripts/sync_tools.py   # regenerates config/processed_tool_registry.json
    pytest tests/test_masterdata_alias.py -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from services.dependency_resolver.resolver import DependencyResolver  # noqa: E402
from services.tool_contracts import UnifiedToolDefinition  # noqa: E402


REGISTRY_PATH = ROOT / "config" / "processed_tool_registry.json"


def _load_tool(op_id: str) -> UnifiedToolDefinition:
    if not REGISTRY_PATH.exists():
        pytest.fail(
            f"{REGISTRY_PATH} missing - run `python scripts/migrate_registry_v6.py` first."
        )
    raw = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    tools = raw["tools"] if isinstance(raw, dict) and "tools" in raw else raw
    for entry in tools:
        if entry.get("operation_id") == op_id:
            return UnifiedToolDefinition(**entry)
    pytest.fail(f"operation_id '{op_id}' not found in {REGISTRY_PATH}")


class FakeRegistry:
    def __init__(self, tools: Dict[str, UnifiedToolDefinition]) -> None:
        self._tools = tools

    def get_all_tools(self) -> Dict[str, UnifiedToolDefinition]:
        return self._tools

    def get_tool(self, op_id: str) -> UnifiedToolDefinition | None:
        return self._tools.get(op_id)


@pytest.mark.asyncio
async def test_alias_chain_assembles_addmileage_payload():
    persons = _load_tool("get_Persons")
    masterdata = _load_tool("get_MasterData")
    addmileage = _load_tool("post_AddMileage")

    # --- Sanity: migration actually applied ---
    assert persons.output_key_aliases == {"Id": "PersonId"}, persons.output_key_aliases
    assert masterdata.output_key_aliases == {"Id": "VehicleId"}, masterdata.output_key_aliases
    assert masterdata.parameters["personId"].required is True
    filter_param = persons.parameters["Filter"]
    assert filter_param.dependency_source.value == "context"
    assert filter_param.filter_template == "Phone(=){phone}"

    registry = FakeRegistry({
        "get_Persons": persons,
        "get_MasterData": masterdata,
        "post_AddMileage": addmileage,
    })
    resolver = DependencyResolver(registry=registry)

    calls: list[tuple[str, Dict[str, Any]]] = []

    async def fake_executor(op_id: str, params: Dict[str, Any]) -> Any:
        calls.append((op_id, dict(params)))
        if op_id == "get_Persons":
            assert params.get("Filter") == "Phone(=)+385955087196", (
                f"filter_template did not render correctly: {params}"
            )
            return {"Data": [{"Id": "osoba-123", "TenantId": "t-99", "Phone": "+385955087196"}]}
        if op_id == "get_MasterData":
            assert params.get("personId") == "osoba-123", (
                f"personId from Persons.Id alias not propagated: {params}"
            )
            return [{"Id": "auto-777"}]
        raise AssertionError(f"unexpected executor call: {op_id}")

    session_context: Dict[str, Any] = {"phone": "+385955087196"}
    user_input: Dict[str, Any] = {"Value": 500}

    payload = await resolver.prepare_params_for_tool(
        target_tool=addmileage,
        context=session_context,
        executor=fake_executor,
        user_input=user_input,
    )

    # --- DOKAZ: VehicleId resolved through full graph ---
    assert payload.get("VehicleId") == "auto-777", payload
    assert payload.get("Value") == 500, payload

    # --- DOKAZ: TenantId seeded into session context (header source) ---
    assert session_context.get("TenantId") == "t-99", session_context

    # --- DOKAZ: graph fired in correct order, exactly once each ---
    op_order = [c[0] for c in calls]
    assert op_order == ["get_Persons", "get_MasterData"], op_order

    # --- DOKAZ: aliased keys live in context for downstream consumers ---
    assert session_context.get("PersonId") == "osoba-123"
    assert session_context.get("VehicleId") == "auto-777"
