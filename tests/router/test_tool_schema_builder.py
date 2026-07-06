"""Unit tests for ToolSchemaBuilder."""
from __future__ import annotations

import pytest

from services.router.tool_schema_builder import (
    MAX_TOOL_NAME_LEN, ToolSchemaBuilder, _cardinality_hint,
    _make_alias, _param_to_jsonschema,
)


def _mk_tool(operation_id, method="GET", parameters=None, required=None, is_mutation=False):
    return {
        "operation_id": operation_id,
        "method": method,
        "parameters": parameters or {},
        "required_params": required or [],
        "is_mutation": is_mutation,
    }


def _mk_param(name, ptype="string", desc="", required=False, dep="user_input", enum=None, items=None):
    return {
        "name": name,
        "param_type": ptype,
        "description": desc,
        "required": required,
        "dependency_source": dep,
        "enum_values": enum,
        "items_type": items,
    }


def test_alias_under_64_chars():
    long_name = "put_PeriodicActivitiesSchedules_id_documents_documentId_SetAsDefault"
    assert len(long_name) > MAX_TOOL_NAME_LEN
    alias = _make_alias(long_name)
    assert len(alias) <= MAX_TOOL_NAME_LEN
    assert alias.startswith("put_PeriodicActivitiesSchedules")
    # Deterministic across calls
    assert _make_alias(long_name) == alias


def test_short_names_not_aliased():
    reg = {"tools": [_mk_tool("get_MasterData")]}
    b = ToolSchemaBuilder.from_registry(reg)
    assert b.schema_name_for("get_MasterData") == "get_MasterData"
    assert b.operation_id_for("get_MasterData") == "get_MasterData"


def test_long_names_aliased_round_trip():
    long_name = "put_VehiclesHistoricalEntries_id_documents_documentId_SetAsDefault"
    reg = {"tools": [_mk_tool(long_name)]}
    b = ToolSchemaBuilder.from_registry(reg)
    alias = b.schema_name_for(long_name)
    assert alias != long_name
    assert len(alias) <= MAX_TOOL_NAME_LEN
    assert b.operation_id_for(alias) == long_name


def test_cardinality_hint_by_suffix():
    assert "METADATA" in _cardinality_hint("get_X_metadata")
    assert "BY_ID" in _cardinality_hint("get_X_id")
    assert "AGGREGATE" in _cardinality_hint("get_X_Agg")
    assert "GROUPBY" in _cardinality_hint("get_X_GroupBy")
    assert "HIERARCHY" in _cardinality_hint("get_X_tree")
    assert "PROJECTION" in _cardinality_hint("get_X_ProjectTo")
    assert _cardinality_hint("get_Vehicles") == ""


def test_param_to_jsonschema_basic_types():
    assert _param_to_jsonschema(_mk_param("a", "string"))["type"] == "string"
    assert _param_to_jsonschema(_mk_param("a", "integer"))["type"] == "integer"
    assert _param_to_jsonschema(_mk_param("a", "boolean"))["type"] == "boolean"
    assert _param_to_jsonschema(_mk_param("a", "number"))["type"] == "number"
    # Unknown type defaults to string
    assert _param_to_jsonschema(_mk_param("a", "weird"))["type"] == "string"


def test_param_to_jsonschema_array_with_items_type():
    p = _param_to_jsonschema(_mk_param("filters", "array", items="integer"))
    assert p["type"] == "array"
    assert p["items"] == {"type": "integer"}


def test_param_to_jsonschema_includes_enum():
    p = _param_to_jsonschema(_mk_param("op", "string", enum=["Sum", "Min", "Max"]))
    assert p["enum"] == ["Sum", "Min", "Max"]


def test_build_skips_context_injected_params():
    tool = _mk_tool("get_X", parameters={
        "personId": _mk_param("personId", "string", dep="context"),
        "Rows": _mk_param("Rows", "integer", dep="user_input"),
    })
    b = ToolSchemaBuilder.from_registry({"tools": [tool]})
    schemas = b.build_for_tools([tool], tkb={})
    assert len(schemas) == 1
    props = schemas[0]["function"]["parameters"]["properties"]
    assert "Rows" in props
    assert "personId" not in props  # auto-injected, hidden from LLM


def test_build_suppresses_filter_params():
    """Filter/UseANDFor are hidden from the LLM schema (Filip 2026-05-25) —
    the deterministic filter feature is reset to zero, so the LLM must not
    free-fill a (possibly malformed) filter while it waits on the M1 schema."""
    tool = _mk_tool("get_Vehicles", parameters={
        "Filter": _mk_param("Filter", "string", dep="user_input"),
        "UseANDFor": _mk_param("UseANDFor", "string", dep="user_input"),
        "Rows": _mk_param("Rows", "integer", dep="user_input"),
    })
    b = ToolSchemaBuilder.from_registry({"tools": [tool]})
    schemas = b.build_for_tools([tool], tkb={})
    props = schemas[0]["function"]["parameters"]["properties"]
    assert "Rows" in props
    assert "Filter" not in props
    assert "UseANDFor" not in props


def test_build_suppresses_filter_params_case_insensitively():
    """The registry carries BOTH casings (`Filter` ×113 / `filter` ×110,
    `UseANDFor` / `useANDFor`). The old exact-match set let the lowercase
    half through — the LLM could free-fill a hallucinated filter that the
    gateway then shipped to MobilityOne."""
    tool = _mk_tool("get_Trips", parameters={
        "filter": _mk_param("filter", "array", dep="user_input"),
        "useANDFor": _mk_param("useANDFor", "array", dep="user_input"),
        "Rows": _mk_param("Rows", "integer", dep="user_input"),
    })
    b = ToolSchemaBuilder.from_registry({"tools": [tool]})
    schemas = b.build_for_tools([tool], tkb={})
    props = schemas[0]["function"]["parameters"]["properties"]
    assert "Rows" in props
    assert "filter" not in props
    assert "useANDFor" not in props


def test_build_includes_tkb_intent_in_description():
    tool = _mk_tool("get_VehicleContracts")
    tkb = {
        "get_VehicleContracts": {
            "intent_summary": "Ugovori za vozila — lizing.",
            "use_when": ["manager traži ugovor"],
            "do_not_use_when": [{"alt_tool": "get_LatestVehicleContracts", "razlog": "samo aktivni"}],
        }
    }
    b = ToolSchemaBuilder.from_registry({"tools": [tool]})
    schemas = b.build_for_tools([tool], tkb)
    desc = schemas[0]["function"]["description"]
    assert "Ugovori za vozila" in desc
    assert "manager traži ugovor" in desc
    assert "samo aktivni" in desc
    assert desc.startswith("[GET]")  # method prefix


def test_build_marks_mutating_tools():
    tool = _mk_tool("post_AddCase", method="POST", is_mutation=True)
    b = ToolSchemaBuilder.from_registry({"tools": [tool]})
    schemas = b.build_for_tools([tool], tkb={})
    assert "MUTATING" in schemas[0]["function"]["description"]


def test_build_skips_tools_without_operation_id():
    reg = {"tools": [{"description": "no id"}]}
    b = ToolSchemaBuilder.from_registry(reg)
    schemas = b.build_for_tools(reg["tools"], tkb={})
    assert schemas == []


# ANTI-FABRICATION (Filip 2026-05-24): the LLM schema intentionally emits an
# EMPTY `required` so the model only fills params the user actually stated
# (no guessing required values). Required-ness is enforced downstream from the
# registry (engine._compute_missing_required → param-ask). These tests pin
# that contract: the param still appears in `properties`, but NOT in `required`.

def test_required_params_omitted_from_llm_schema_required():
    tool = _mk_tool(
        "post_X", method="POST",
        parameters={"name": _mk_param("name", "string")},
        required=["name"],
    )
    b = ToolSchemaBuilder.from_registry({"tools": [tool]})
    schemas = b.build_for_tools([tool], tkb={})
    params = schemas[0]["function"]["parameters"]
    assert params["required"] == []          # never forced on the LLM
    assert "name" in params["properties"]     # but still offered as a property


def test_param_required_flag_not_forced_on_llm():
    tool = _mk_tool(
        "post_X", method="POST",
        parameters={"name": _mk_param("name", "string", required=True)},
    )
    b = ToolSchemaBuilder.from_registry({"tools": [tool]})
    schemas = b.build_for_tools([tool], tkb={})
    params = schemas[0]["function"]["parameters"]
    assert params["required"] == []
    assert "name" in params["properties"]


def test_real_registry_three_aliased_tools_under_limit():
    """Validate against the real registry — 3 tools >64 chars become aliases."""
    import json
    from pathlib import Path
    p = Path(__file__).resolve().parents[2] / "config" / "processed_tool_registry.json"
    reg = json.loads(p.read_text(encoding="utf-8"))
    b = ToolSchemaBuilder.from_registry(reg)
    # We expect exactly 3 aliases (verified earlier by grep)
    assert len(b._name_aliases) == 3
    for op_id, alias in b._name_aliases.items():
        assert len(op_id) > MAX_TOOL_NAME_LEN
        assert len(alias) <= MAX_TOOL_NAME_LEN
        assert b.operation_id_for(alias) == op_id
