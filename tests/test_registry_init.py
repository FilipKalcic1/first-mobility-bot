"""Tests for services/registry/__init__.py (ToolRegistry facade)."""

import json
import os
import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from services.registry import ToolRegistry
from services.tool_contracts import UnifiedToolDefinition, ParameterDefinition, DependencyGraph


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
# Init
# ---------------------------------------------------------------------------

class TestInit:
    def test_init_default_state(self):
        r = ToolRegistry()
        assert r.is_ready is False
        assert r.tools == {}
        assert r.dependency_graph == {}
        assert r.retrieval_tools == set()
        assert r.mutation_tools == set()


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
             patch("services.registry._read_json_file", return_value=registry_data):
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

    @pytest.mark.asyncio
    async def test_malformed_tool_skipped_not_fatal(self):
        """One bad tool must NOT kill the whole registry — it is skipped with a
        warning and the valid tools still load (resilience fix 2026-05-20)."""
        registry_data = {
            "tools": [
                _sample_tool_dict("get_Good"),
                {"operation_id": "bad_tool", "method": "NOTAMETHOD",  # invalid HTTP verb
                 "path": "/x", "service_name": "s", "service_url": "u"},
                _sample_tool_dict("post_AlsoGood", "POST"),
            ],
            "dependency_graph": [],
        }

        def _exists(p):
            return str(p).endswith("processed_tool_registry.json")

        with patch("os.path.exists", side_effect=_exists), \
             patch("services.registry._read_json_file", return_value=registry_data):
            r = ToolRegistry()
            ok = await r.initialize()

        assert ok is True          # registry still initializes
        assert r.is_ready is True
        assert "get_Good" in r.tools       # valid tools loaded
        assert "post_AlsoGood" in r.tools
        assert "bad_tool" not in r.tools   # malformed one skipped, not fatal


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


# Removed in 11.0.4 — TestHiddenDefaults class.
# `get_hidden_defaults` and `get_merged_params` removed from ToolRegistry.
# Tool-specific protocol constants (EntryType, AssigneeType, CaseEntryType)
# now live in services/booking_contracts.py and are applied explicitly by
# the matching flow handler. There is no longer a generic registry-level
# "hidden defaults" mechanism — that was domain knowledge masquerading
# as configuration.
#
# Test for explicit case-creation EntryType injection lives in the
# confirmation_handler test suite.
