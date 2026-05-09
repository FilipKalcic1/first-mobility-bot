"""Tests for v2 ToolRegistry adapter."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.v2.registry import ToolRegistry


REGISTRY_PATH = (
    Path(__file__).resolve().parents[2]
    / "config" / "processed_tool_registry.json"
)


@pytest.fixture(scope="module")
def real_registry() -> ToolRegistry:
    return ToolRegistry.from_file(REGISTRY_PATH)


def _sample_tool(operation_id="get_X", **kw) -> dict:
    base = {
        "operation_id": operation_id,
        "service_name": "automation",
        "path": "/X",
        "method": "GET",
        "embedding_text": f"{operation_id}. Sample purpose.",
    }
    base.update(kw)
    return base


# --- contract conformance with in-memory fixtures -----------------------


def test_lookup_known_tool():
    r = ToolRegistry([_sample_tool("get_A"), _sample_tool("post_B", method="POST")])
    assert r.has_tool("get_A")
    assert r.has_tool("post_B")
    assert not r.has_tool("nonexistent")


def test_method_of_returns_uppercase():
    r = ToolRegistry([_sample_tool(method="get")])
    assert r.method_of("get_X") == "GET"


def test_method_of_unknown_returns_none():
    r = ToolRegistry([])
    assert r.method_of("anything") is None


def test_spec_for_unknown_returns_none():
    r = ToolRegistry([])
    assert r.spec_for("anything") is None


def test_spec_for_known_returns_normalized_dict():
    r = ToolRegistry([_sample_tool("get_A", service_name="vehiclemgt")])
    spec = r.spec_for("get_A")
    assert spec["service"] == "vehiclemgt"
    assert spec["method"] == "GET"
    assert spec["path"] == "/X"


def test_anchor_text_prepends_method_marker_when_enabled():
    """Action markers must prepend Croatian action prefix when
    enable_action_markers=True. Default is OFF (regressed accuracy on
    benchmark) but available as opt-in for production data tests."""
    delete_tool = _sample_tool(
        "delete_Vehicles_id", method="DELETE",
        embedding_text="Briše vozilo prema identifikatoru.",
    )
    get_tool = _sample_tool(
        "get_Vehicles_id", method="GET",
        embedding_text="Dohvaća vozilo prema identifikatoru.",
    )
    r = ToolRegistry(
        [delete_tool, get_tool], enable_action_markers=True,
    )
    delete_anchor = r.anchor_text_for(delete_tool)
    get_anchor = r.anchor_text_for(get_tool)
    assert delete_anchor.startswith("BRISANJE")
    assert get_anchor.startswith("DOHVAT")
    assert "JEDAN" in delete_anchor
    assert "JEDAN" in get_anchor


def test_anchor_text_marker_distinguishes_DeleteByCriteria_from_id():
    one = _sample_tool(
        "delete_Vehicles_id", method="DELETE",
        embedding_text="Briše jedno vozilo.",
    )
    many = _sample_tool(
        "delete_Vehicles_DeleteByCriteria", method="DELETE",
        embedding_text="Briše vozila po kriteriju.",
    )
    r = ToolRegistry([one, many], enable_action_markers=True)
    a_one = r.anchor_text_for(one)
    a_many = r.anchor_text_for(many)
    assert "JEDAN" in a_one
    assert "VIŠE" in a_many or "FILTRIRAJ" in a_many
    assert a_one != a_many


def test_anchor_text_uses_embedding_text():
    tool = _sample_tool("get_A", embedding_text="Custom anchor text here.")
    r = ToolRegistry([tool])
    out = r.anchor_text_for(tool)
    assert "Custom anchor text here." in out


def test_anchor_text_falls_back_to_op_path_when_no_embedding():
    tool = _sample_tool("get_A", embedding_text=None)
    r = ToolRegistry([tool])
    text = r.anchor_text_for(tool)
    assert "get_A" in text and "/X" in text


def test_anchor_text_prepends_enrichment_phrases_when_present():
    tool = _sample_tool("get_X", embedding_text="terse anchor")
    r = ToolRegistry(
        [tool],
        anchor_enrichments={
            "get_X": ["Croatian paraphrase one.", "Croatian paraphrase two."],
        },
    )
    out = r.anchor_text_for(tool)
    assert "Croatian paraphrase one." in out
    assert "Croatian paraphrase two." in out
    assert "terse anchor" in out


def test_anchor_text_no_enrichment_returns_original():
    tool = _sample_tool("get_X", embedding_text="terse anchor")
    r = ToolRegistry([tool])  # markers default OFF
    assert r.anchor_text_for(tool) == "terse anchor"


def test_tool_id_of_extracts_operation_id():
    tool = _sample_tool("get_VehicleCalendar")
    assert ToolRegistry.tool_id_of(tool) == "get_VehicleCalendar"


def test_purpose_strips_operation_id_prefix():
    tool = _sample_tool(
        "get_X",
        embedding_text="get_X. The real purpose lives here.",
        description=None,
    )
    r = ToolRegistry([tool])
    purpose = r.purpose_of("get_X")
    assert "real purpose" in purpose
    assert not purpose.startswith("get_X. ")


def test_purpose_uses_description_when_present():
    tool = _sample_tool("get_X", description="Hand-curated purpose.")
    r = ToolRegistry([tool])
    assert r.purpose_of("get_X") == "Hand-curated purpose."


def test_purpose_truncates_long_text():
    long_text = "get_X. " + ("x" * 500)
    tool = _sample_tool("get_X", embedding_text=long_text, description=None)
    r = ToolRegistry([tool])
    assert len(r.purpose_of("get_X")) <= 300


def test_purpose_unknown_tool_returns_empty_string():
    r = ToolRegistry([])
    assert r.purpose_of("nonexistent") == ""




# --- real registry sanity ------------------------------------------------


def test_real_registry_loads(real_registry):
    """Sanity: the JSON exists and parses."""
    tools = list(real_registry.tools)
    assert len(tools) > 100  # actual is ~950
    assert all("operation_id" in t for t in tools)


def test_real_registry_methods_uppercase(real_registry):
    """All methods normalize to uppercase via method_of()."""
    sample_ids = [t["operation_id"] for t in list(real_registry.tools)[:50]]
    for tid in sample_ids:
        m = real_registry.method_of(tid)
        assert m in {"GET", "POST", "PUT", "PATCH", "DELETE"}


def test_real_registry_anchor_text_nonempty(real_registry):
    """Every tool produces non-empty anchor text — L3 needs this."""
    empties = [
        t["operation_id"]
        for t in real_registry.tools
        if not real_registry.anchor_text_for(t)
    ]
    assert not empties, f"Tools without anchor text: {empties[:5]}"


def test_real_registry_spec_for_executor_shape(real_registry):
    """Executor expects {service, path, method} on every spec."""
    for t in list(real_registry.tools)[:20]:
        spec = real_registry.spec_for(t["operation_id"])
        assert spec is not None
        assert spec.get("service")
        assert spec.get("method") in {"GET", "POST", "PUT", "PATCH", "DELETE"}
        assert "path" in spec


def test_real_registry_loads_anchor_enrichments_when_sidecar_present(
    real_registry,
):
    """Regression: the production config/tool_anchor_enrichments.json
    must auto-load and prepend phrases to anchor_text. Lifted Judge
    accuracy from 13.8% to 20.3% on the adversarial paraphrase
    benchmark (see tests/benchmarks/v2_real_accuracy_results.*.json)."""
    enrichment_path = REGISTRY_PATH.parent / "tool_anchor_enrichments.json"
    if not enrichment_path.exists():
        pytest.skip("enrichment sidecar not built in this environment")
    cc = real_registry.spec_for("delete_CostCenters")
    assert cc is not None
    enriched = real_registry.anchor_text_for(cc)
    # Must contain at least one Croatian enrichment phrase, not just
    # the original English-y embedding_text
    assert any(
        kw in enriched.lower()
        for kw in ("mjest", "trošk", "centre", "centra", "centri")
    ), f"enrichment didn't reach delete_CostCenters anchor: {enriched[:200]}"


def test_real_registry_known_critical_tools_present(real_registry):
    """Spot-check: tools the v2 engine depends on must exist."""
    for tid in (
        "get_MasterData",
        "post_AddMileage",
        "post_AddCase",
    ):
        assert real_registry.has_tool(tid), f"missing critical tool: {tid}"
