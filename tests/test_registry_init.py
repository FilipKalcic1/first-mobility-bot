"""Tests for services/registry/__init__.py (ToolRegistry facade)."""

import json
import os
import pytest
from unittest.mock import patch, MagicMock, AsyncMock

import services.registry as registry_module
from services.registry import ToolRegistry
from services.tool_contracts import UnifiedToolDefinition, ParameterDefinition, DependencyGraph


@pytest.fixture(autouse=True)
def _reset_doc_cache():
    registry_module._documentation_cache = None
    yield
    registry_module._documentation_cache = None


def _sample_tool_dict(op_id="get_Vehicles", method="GET"):
    return {
        "operation_id": op_id,
        "method": method,
        "path": "/api/vehicles",
        "description": "desc",
        "parameters": {},
        "service_name": "fleet",
        "service_url": "https://api.example.com",
        "swagger_name": "fleet",
    }


# ---------------------------------------------------------------------------
# Init + _load_documentation
# ---------------------------------------------------------------------------

class TestInit:
    def test_init_no_doc_file(self, tmp_path):
        with patch("os.path.exists", return_value=False):
            r = ToolRegistry()
        assert r.is_ready is False
        assert r._tool_documentation == {}
        assert r.tools == {}
        assert r.dependency_graph == {}
        assert r.retrieval_tools == set()
        assert r.mutation_tools == set()

    def test_init_loads_documentation(self):
        doc = {"get_Vehicles": {"description_hr": "dohvaća vozila"}}
        m = MagicMock()
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", MagicMock()), \
             patch("json.load", return_value=doc):
            r = ToolRegistry()
        assert r._tool_documentation == doc

    def test_documentation_cache_reused(self):
        registry_module._documentation_cache = {"x": {"y": 1}}
        r = ToolRegistry()
        assert r._tool_documentation == {"x": {"y": 1}}

    def test_documentation_load_error_swallowed(self):
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", side_effect=OSError("boom")):
            r = ToolRegistry()
        assert r._tool_documentation == {}


# ---------------------------------------------------------------------------
# initialize()
# ---------------------------------------------------------------------------

class TestInitialize:
    @pytest.mark.asyncio
    async def test_missing_registry_file_fails(self):
        with patch("os.path.exists", return_value=False):
            r = ToolRegistry()
            ok = await r.initialize()
        assert ok is False
        assert r.is_ready is False

    @pytest.mark.asyncio
    async def test_loads_tools_and_deps(self, tmp_path):
        registry_data = {
            "tools": [_sample_tool_dict("get_A"), _sample_tool_dict("post_B", "POST")],
            "dependency_graph": [
                {"tool_id": "post_B", "required_outputs": ["AId"], "provider_tools": ["get_A"]}
            ],
        }

        # doc file missing, registry file present
        def _exists(p):
            return str(p).endswith("processed_tool_registry.json")

        with patch("os.path.exists", side_effect=_exists), \
             patch("services.registry._read_json_file", return_value=registry_data), \
             patch.object(ToolRegistry, "_initialize_faiss", new=AsyncMock()):
            r = ToolRegistry()
            ok = await r.initialize()

        assert ok is True
        assert r.is_ready is True
        assert "get_A" in r.tools
        assert "post_B" in r.tools
        assert "get_A" in r.retrieval_tools
        assert "post_B" in r.mutation_tools
        assert "post_B" in r.dependency_graph

    @pytest.mark.asyncio
    async def test_load_exception_returns_false(self):
        with patch("os.path.exists", return_value=True), \
             patch("services.registry._read_json_file", side_effect=RuntimeError("boom")):
            r = ToolRegistry()
            ok = await r.initialize()
        assert ok is False
        assert r.is_ready is False


# ---------------------------------------------------------------------------
# _initialize_faiss
# ---------------------------------------------------------------------------

class TestInitializeFaiss:
    @pytest.mark.asyncio
    async def test_skips_when_docs_empty(self):
        with patch("os.path.exists", return_value=False):
            r = ToolRegistry()
        # No exception, no call
        with patch("services.faiss_vector_store.initialize_faiss_store") as mock_init:
            await r._initialize_faiss()
        mock_init.assert_not_called()

    @pytest.mark.asyncio
    async def test_calls_faiss_when_docs_present(self):
        with patch("os.path.exists", return_value=False):
            r = ToolRegistry()
        r._tool_documentation = {"t1": {"x": 1}}

        faiss_store = MagicMock()
        faiss_store.get_stats.return_value = {"total_tools": 1}

        with patch(
            "services.faiss_vector_store.initialize_faiss_store",
            AsyncMock(return_value=faiss_store),
        ) as mock_init:
            await r._initialize_faiss()
        mock_init.assert_called_once()


# ---------------------------------------------------------------------------
# Documentation access
# ---------------------------------------------------------------------------

class TestDocumentationAccess:
    def _registry_with_docs(self, docs):
        with patch("os.path.exists", return_value=False):
            r = ToolRegistry()
        r._tool_documentation = docs
        return r

    def test_get_tool_documentation(self):
        r = self._registry_with_docs({"op1": {"description_hr": "test"}})
        assert r.get_tool_documentation("op1") == {"description_hr": "test"}
        assert r.get_tool_documentation("missing") is None

    def test_get_parameter_origin_guide(self):
        r = self._registry_with_docs({
            "op1": {"parameter_origin_guide": {"Id": "CONTEXT"}}
        })
        assert r.get_parameter_origin_guide("op1") == {"Id": "CONTEXT"}
        assert r.get_parameter_origin_guide("missing") == {}

    def test_is_context_param(self):
        r = self._registry_with_docs({
            "op1": {"parameter_origin_guide": {"TenantId": "CONTEXT", "Name": "USER"}}
        })
        assert r.is_context_param("op1", "TenantId") is True
        assert r.is_context_param("op1", "Name") is False

    def test_is_user_param_defaults_true(self):
        r = self._registry_with_docs({})
        assert r.is_user_param("op1", "Name") is True

    def test_get_tool_with_documentation_returns_none_if_missing(self):
        r = self._registry_with_docs({})
        assert r.get_tool_with_documentation("missing") is None

    def test_get_tool_with_documentation(self):
        r = self._registry_with_docs({"op1": {"description_hr": "test"}})
        tool = UnifiedToolDefinition(**_sample_tool_dict("op1"))
        r._store.add_tool(tool)

        result = r.get_tool_with_documentation("op1")
        assert result is not None
        assert result["documentation"] == {"description_hr": "test"}
        assert "tool" in result


# ---------------------------------------------------------------------------
# Tool access
# ---------------------------------------------------------------------------

class TestToolAccess:
    def test_get_tool_and_list(self):
        with patch("os.path.exists", return_value=False):
            r = ToolRegistry()
        tool = UnifiedToolDefinition(**_sample_tool_dict("op1"))
        r._store.add_tool(tool)

        assert r.get_tool("op1") is tool
        assert r.get_tool("missing") is None
        assert r.list_tools() == ["op1"]


# ---------------------------------------------------------------------------
# Hidden defaults
# ---------------------------------------------------------------------------

class TestHiddenDefaults:
    def test_known_tool(self):
        with patch("os.path.exists", return_value=False):
            r = ToolRegistry()
        defaults = r.get_hidden_defaults("post_VehicleCalendar")
        assert defaults == {"EntryType": 0, "AssigneeType": 1}

    def test_unknown_tool(self):
        with patch("os.path.exists", return_value=False):
            r = ToolRegistry()
        assert r.get_hidden_defaults("nonexistent") == {}

    def test_merged_user_wins(self):
        with patch("os.path.exists", return_value=False):
            r = ToolRegistry()
        merged = r.get_merged_params(
            "post_VehicleCalendar",
            {"EntryType": 5, "Extra": "x"},
        )
        assert merged["EntryType"] == 5
        assert merged["AssigneeType"] == 1
        assert merged["Extra"] == "x"

    def test_merged_filters_none(self):
        with patch("os.path.exists", return_value=False):
            r = ToolRegistry()
        merged = r.get_merged_params(
            "post_VehicleCalendar",
            {"EntryType": None},
        )
        assert merged["EntryType"] == 0  # default kept, None dropped

    def test_merged_no_user_params(self):
        with patch("os.path.exists", return_value=False):
            r = ToolRegistry()
        merged = r.get_merged_params("post_VehicleCalendar", {})
        assert merged == {"EntryType": 0, "AssigneeType": 1}
