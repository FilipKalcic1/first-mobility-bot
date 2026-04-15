"""Tests for ToolStore - In-memory storage for tools and dependencies."""

import pytest

from services.registry.tool_store import ToolStore
from services.tool_contracts import UnifiedToolDefinition, ParameterDefinition, DependencyGraph


@pytest.fixture
def store():
    return ToolStore()


@pytest.fixture
def sample_tool():
    return UnifiedToolDefinition(
        operation_id="get_Vehicles",
        method="GET",
        path="/api/vehicles",
        description="Get all vehicles",
        parameters={
            "VehicleId": ParameterDefinition(
                name="VehicleId", param_type="string",
                required=False, description="Vehicle ID"
            )
        },
        service_name="fleet",
        service_url="https://api.example.com",
        swagger_name="fleet",
    )


@pytest.fixture
def mutation_tool():
    return UnifiedToolDefinition(
        operation_id="post_Vehicle",
        method="POST",
        path="/api/vehicles",
        description="Create a vehicle",
        parameters={
            "Name": ParameterDefinition(
                name="Name", param_type="string",
                required=True, description="Vehicle name"
            )
        },
        service_name="fleet",
        service_url="https://api.example.com",
        swagger_name="fleet",
    )


class TestToolStoreInit:
    def test_init_empty(self, store):
        assert store.tools == {}
        assert store.dependency_graph == {}
        assert store.retrieval_tools == set()
        assert store.mutation_tools == set()


class TestAddTool:
    def test_add_retrieval_tool(self, store, sample_tool):
        store.add_tool(sample_tool)
        assert sample_tool.operation_id in store.tools
        assert sample_tool.operation_id in store.retrieval_tools
        assert sample_tool.operation_id not in store.mutation_tools

    def test_add_mutation_tool(self, store, mutation_tool):
        store.add_tool(mutation_tool)
        assert mutation_tool.operation_id in store.tools
        assert mutation_tool.operation_id in store.mutation_tools

    def test_add_multiple_tools(self, store, sample_tool, mutation_tool):
        store.add_tool(sample_tool)
        store.add_tool(mutation_tool)
        assert len(store.tools) == 2


class TestGetTool:
    def test_get_existing_tool(self, store, sample_tool):
        store.add_tool(sample_tool)
        assert store.get_tool(sample_tool.operation_id) is sample_tool

    def test_get_missing_tool(self, store):
        assert store.get_tool("nonexistent_tool") is None


class TestHasTool:
    def test_has_existing_tool(self, store, sample_tool):
        store.add_tool(sample_tool)
        assert store.has_tool(sample_tool.operation_id) is True

    def test_has_missing_tool(self, store):
        assert store.has_tool("nonexistent") is False


class TestListTools:
    def test_list_empty(self, store):
        assert store.list_tools() == []

    def test_list_with_tools(self, store, sample_tool, mutation_tool):
        store.add_tool(sample_tool)
        store.add_tool(mutation_tool)
        tool_list = store.list_tools()
        assert len(tool_list) == 2
        assert sample_tool.operation_id in tool_list


class TestGetAllTools:
    def test_get_all_empty(self, store):
        assert store.get_all_tools() == {}

    def test_get_all_with_tools(self, store, sample_tool):
        store.add_tool(sample_tool)
        assert sample_tool.operation_id in store.get_all_tools()


class TestCount:
    def test_count_empty(self, store):
        assert store.count() == 0

    def test_count_with_tools(self, store, sample_tool, mutation_tool):
        store.add_tool(sample_tool)
        store.add_tool(mutation_tool)
        assert store.count() == 2


class TestDependencies:
    def test_add_dependency(self, store):
        dep = DependencyGraph(tool_id="get_Vehicles", required_outputs=["VehicleTypeId"])
        store.add_dependency(dep)
        assert "get_Vehicles" in store.dependency_graph

    def test_get_dependency(self, store):
        dep = DependencyGraph(tool_id="get_Vehicles", required_outputs=["VehicleTypeId"])
        store.add_dependency(dep)
        assert store.get_dependency("get_Vehicles") is dep

    def test_get_dependency_missing(self, store):
        assert store.get_dependency("nonexistent") is None


class TestClear:
    def test_clear_all_data(self, store, sample_tool):
        store.add_tool(sample_tool)
        dep = DependencyGraph(tool_id=sample_tool.operation_id)
        store.add_dependency(dep)

        store.clear()

        assert store.tools == {}
        assert store.dependency_graph == {}
        assert store.retrieval_tools == set()
        assert store.mutation_tools == set()


class TestGetStats:
    def test_get_stats_empty(self, store):
        stats = store.get_stats()
        assert stats["total_tools"] == 0
        assert stats["retrieval_tools"] == 0
        assert stats["mutation_tools"] == 0
        assert stats["dependencies"] == 0

    def test_get_stats_with_data(self, store, sample_tool, mutation_tool):
        store.add_tool(sample_tool)
        store.add_tool(mutation_tool)
        dep = DependencyGraph(tool_id=sample_tool.operation_id)
        store.add_dependency(dep)

        stats = store.get_stats()
        assert stats["total_tools"] == 2
        assert stats["retrieval_tools"] == 1
        assert stats["mutation_tools"] == 1
        assert stats["dependencies"] == 1
