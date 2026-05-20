"""
Tests for API Capabilities Discovery

Comprehensive tests for services/api_capabilities.py covering:
1. ParameterSupport enum
2. ToolCapability dataclass (to_dict, from_dict)
3. APICapabilityRegistry class (all methods)
4. Global functions (get_capability_registry, initialize_capability_registry)
"""

import pytest
import json
from unittest.mock import AsyncMock, MagicMock, patch, mock_open
from pathlib import Path

from services.api_capabilities import (
    ParameterSupport,
    ToolCapability,
    APICapabilityRegistry,
    get_capability_registry,
    initialize_capability_registry,
    CAPABILITIES_CACHE_FILE,
    _capability_registry
)
from services.tool_contracts import (
    UnifiedToolDefinition,
    ParameterDefinition,
    DependencySource
)


# ============================================================================
# FIXTURES
# ============================================================================

@pytest.fixture
def sample_tool_capability():
    """Create a sample ToolCapability for testing."""
    return ToolCapability(
        operation_id="get_TestResource",
        method="GET",
        path="/api/v1/resource",
        supports_person_id=ParameterSupport.NATIVE,
        supports_vehicle_id=ParameterSupport.FILTER,
        supports_tenant_id=ParameterSupport.NOT_SUPPORTED,
        supports_filter=True,
        returns_list=True,
        returns_user_specific_data=True,
        successful_calls=5,
        failed_calls=2,
        last_error="Test error",
        learned_patterns={"pattern1": True}
    )


@pytest.fixture
def sample_tool_capability_dict():
    """Sample ToolCapability as dictionary."""
    return {
        "operation_id": "get_TestResource",
        "method": "GET",
        "path": "/api/v1/resource",
        "supports_person_id": "native",
        "supports_vehicle_id": "filter",
        "supports_tenant_id": "not_supported",
        "supports_filter": True,
        "returns_list": True,
        "returns_user_specific_data": True,
        "successful_calls": 5,
        "failed_calls": 2,
        "last_error": "Test error",
        "learned_patterns": {"pattern1": True}
    }


@pytest.fixture
def mock_tool_with_person_id():
    """Mock tool definition with person_id parameter."""
    tool = MagicMock()
    tool.method = "GET"
    tool.path = "/api/v1/myresource"
    tool.output_keys = ["Id", "Name", "Status", "Description"]

    person_id_param = MagicMock()
    person_id_param.context_key = "person_id"

    status_param = MagicMock()
    status_param.context_key = None

    tool.parameters = {
        "personId": person_id_param,
        "status": status_param
    }
    return tool


@pytest.fixture
def mock_tool_with_vehicle_id():
    """Mock tool definition with vehicle_id parameter."""
    tool = MagicMock()
    tool.method = "GET"
    tool.path = "/api/v1/vehicles"
    tool.output_keys = ["Id", "Name"]

    vehicle_id_param = MagicMock()
    vehicle_id_param.context_key = "vehicle_id"

    tool.parameters = {
        "vehicleId": vehicle_id_param
    }
    return tool


@pytest.fixture
def mock_tool_with_filter():
    """Mock tool definition with Filter parameter only."""
    tool = MagicMock()
    tool.method = "GET"
    tool.path = "/api/v1/data"
    tool.output_keys = ["Id"]

    filter_param = MagicMock()
    filter_param.context_key = None

    tool.parameters = {
        "Filter": filter_param
    }
    return tool


@pytest.fixture
def mock_tool_no_params():
    """Mock tool definition with no special parameters."""
    tool = MagicMock()
    tool.method = "GET"
    tool.path = "/api/v1/public"
    tool.output_keys = None
    tool.parameters = {}
    return tool


@pytest.fixture
def mock_tool_user_specific():
    """Mock tool that returns user-specific data based on path."""
    tool = MagicMock()
    tool.method = "GET"
    tool.path = "/api/v1/user/profile"
    tool.output_keys = ["AssignedDriver", "Owner"]
    tool.parameters = {}
    return tool


@pytest.fixture
def mock_tool_registry(
    mock_tool_with_person_id,
    mock_tool_with_vehicle_id,
    mock_tool_with_filter,
    mock_tool_no_params,
    mock_tool_user_specific
):
    """Mock tool registry with various tools."""
    registry = MagicMock()
    registry.tools = {
        "get_UserResource": mock_tool_with_person_id,
        "get_Vehicles": mock_tool_with_vehicle_id,
        "get_Data": mock_tool_with_filter,
        "get_Public": mock_tool_no_params,
        "get_UserProfile": mock_tool_user_specific
    }

    def get_tool(op_id):
        return registry.tools.get(op_id)

    registry.get_tool = MagicMock(side_effect=get_tool)
    return registry


# ============================================================================
# TEST: ParameterSupport Enum
# ============================================================================

class TestParameterSupport:
    """Test ParameterSupport enum."""

    def test_enum_values(self):
        """Test all enum values exist and have correct string values."""
        assert ParameterSupport.NATIVE.value == "native"
        assert ParameterSupport.FILTER.value == "filter"
        assert ParameterSupport.NOT_SUPPORTED.value == "not_supported"
        assert ParameterSupport.UNKNOWN.value == "unknown"

    def test_enum_from_value(self):
        """Test creating enum from string value."""
        assert ParameterSupport("native") == ParameterSupport.NATIVE
        assert ParameterSupport("filter") == ParameterSupport.FILTER
        assert ParameterSupport("not_supported") == ParameterSupport.NOT_SUPPORTED
        assert ParameterSupport("unknown") == ParameterSupport.UNKNOWN

    def test_enum_invalid_value(self):
        """Test that invalid value raises ValueError."""
        with pytest.raises(ValueError):
            ParameterSupport("invalid")


# ============================================================================
# TEST: ToolCapability Dataclass
# ============================================================================

class TestToolCapability:
    """Test ToolCapability dataclass."""

    def test_default_values(self):
        """Test default field values."""
        cap = ToolCapability(
            operation_id="test_op",
            method="GET",
            path="/api/test"
        )

        assert cap.operation_id == "test_op"
        assert cap.method == "GET"
        assert cap.path == "/api/test"
        assert cap.supports_person_id == ParameterSupport.UNKNOWN
        assert cap.supports_vehicle_id == ParameterSupport.UNKNOWN
        assert cap.supports_tenant_id == ParameterSupport.UNKNOWN
        assert cap.supports_filter is False
        assert cap.returns_list is False
        assert cap.returns_user_specific_data is False
        assert cap.successful_calls == 0
        assert cap.failed_calls == 0
        assert cap.last_error is None
        assert cap.learned_patterns == {}

    def test_to_dict(self, sample_tool_capability):
        """Test to_dict() method serialization."""
        result = sample_tool_capability.to_dict()

        assert result["operation_id"] == "get_TestResource"
        assert result["method"] == "GET"
        assert result["path"] == "/api/v1/resource"
        assert result["supports_person_id"] == "native"
        assert result["supports_vehicle_id"] == "filter"
        assert result["supports_tenant_id"] == "not_supported"
        assert result["supports_filter"] is True
        assert result["returns_list"] is True
        assert result["returns_user_specific_data"] is True
        assert result["successful_calls"] == 5
        assert result["failed_calls"] == 2
        assert result["last_error"] == "Test error"
        assert result["learned_patterns"] == {"pattern1": True}

    def test_from_dict(self, sample_tool_capability_dict):
        """Test from_dict() classmethod deserialization."""
        cap = ToolCapability.from_dict(sample_tool_capability_dict)

        assert cap.operation_id == "get_TestResource"
        assert cap.method == "GET"
        assert cap.path == "/api/v1/resource"
        assert cap.supports_person_id == ParameterSupport.NATIVE
        assert cap.supports_vehicle_id == ParameterSupport.FILTER
        assert cap.supports_tenant_id == ParameterSupport.NOT_SUPPORTED
        assert cap.supports_filter is True
        assert cap.returns_list is True
        assert cap.returns_user_specific_data is True
        assert cap.successful_calls == 5
        assert cap.failed_calls == 2
        assert cap.last_error == "Test error"
        assert cap.learned_patterns == {"pattern1": True}

    def test_from_dict_with_defaults(self):
        """Test from_dict() with minimal data uses defaults."""
        minimal_data = {
            "operation_id": "test_op",
            "method": "POST",
            "path": "/api/test"
        }

        cap = ToolCapability.from_dict(minimal_data)

        assert cap.operation_id == "test_op"
        assert cap.supports_person_id == ParameterSupport.UNKNOWN
        assert cap.supports_vehicle_id == ParameterSupport.UNKNOWN
        assert cap.supports_tenant_id == ParameterSupport.UNKNOWN
        assert cap.supports_filter is False
        assert cap.returns_list is False
        assert cap.returns_user_specific_data is False
        assert cap.successful_calls == 0
        assert cap.failed_calls == 0
        assert cap.last_error is None
        assert cap.learned_patterns == {}

    def test_roundtrip(self, sample_tool_capability):
        """Test to_dict() -> from_dict() roundtrip."""
        cap_dict = sample_tool_capability.to_dict()
        restored = ToolCapability.from_dict(cap_dict)

        assert restored.operation_id == sample_tool_capability.operation_id
        assert restored.method == sample_tool_capability.method
        assert restored.path == sample_tool_capability.path
        assert restored.supports_person_id == sample_tool_capability.supports_person_id
        assert restored.supports_vehicle_id == sample_tool_capability.supports_vehicle_id
        assert restored.supports_filter == sample_tool_capability.supports_filter
        assert restored.successful_calls == sample_tool_capability.successful_calls
        assert restored.failed_calls == sample_tool_capability.failed_calls


# ============================================================================
# TEST: APICapabilityRegistry - Initialization
# ============================================================================

class TestAPICapabilityRegistryInit:
    """Test APICapabilityRegistry initialization."""

    def test_init_without_registry(self):
        """Test initialization without tool registry."""
        registry = APICapabilityRegistry()

        assert registry.tool_registry is None
        assert registry.capabilities == {}
        assert registry._loaded is False

    def test_init_with_registry(self, mock_tool_registry):
        """Test initialization with tool registry."""
        registry = APICapabilityRegistry(tool_registry=mock_tool_registry)

        assert registry.tool_registry is mock_tool_registry
        assert registry.capabilities == {}
        assert registry._loaded is False


# ============================================================================
# TEST: APICapabilityRegistry - Initialize Method
# ============================================================================

class TestAPICapabilityRegistryInitialize:
    """Test APICapabilityRegistry.initialize() method."""

    @pytest.mark.asyncio
    async def test_initialize_no_registry(self):
        """Test initialize fails without tool registry."""
        registry = APICapabilityRegistry()

        result = await registry.initialize()

        assert result is False
        assert registry._loaded is False

    @pytest.mark.asyncio
    async def test_initialize_with_registry_param(self, mock_tool_registry):
        """Test initialize accepts registry as parameter."""
        registry = APICapabilityRegistry()

        with patch.object(registry, '_load_cache', new_callable=AsyncMock) as mock_load:
            mock_load.return_value = False
            with patch.object(registry, '_discover_from_registry', new_callable=AsyncMock) as mock_discover:
                with patch.object(registry, '_save_cache', new_callable=AsyncMock):
                    result = await registry.initialize(tool_registry=mock_tool_registry)

        assert registry.tool_registry is mock_tool_registry
        assert result is True
        assert registry._loaded is True

    @pytest.mark.asyncio
    async def test_initialize_cache_load_success(self, mock_tool_registry):
        """Test initialize uses cache when available."""
        registry = APICapabilityRegistry(tool_registry=mock_tool_registry)

        with patch.object(registry, '_load_cache', new_callable=AsyncMock) as mock_load:
            mock_load.return_value = True
            with patch.object(registry, '_discover_from_registry', new_callable=AsyncMock) as mock_discover:
                result = await registry.initialize()

        assert result is True
        assert registry._loaded is True
        mock_load.assert_called_once()
        mock_discover.assert_not_called()

    @pytest.mark.asyncio
    async def test_initialize_cache_load_failure_triggers_discovery(self, mock_tool_registry):
        """Test initialize discovers from registry when cache fails."""
        registry = APICapabilityRegistry(tool_registry=mock_tool_registry)

        with patch.object(registry, '_load_cache', new_callable=AsyncMock) as mock_load:
            mock_load.return_value = False
            with patch.object(registry, '_discover_from_registry', new_callable=AsyncMock) as mock_discover:
                with patch.object(registry, '_save_cache', new_callable=AsyncMock) as mock_save:
                    result = await registry.initialize()

        assert result is True
        assert registry._loaded is True
        mock_discover.assert_called_once()
        mock_save.assert_called_once()


# ============================================================================
# TEST: APICapabilityRegistry - Discovery
# ============================================================================

class TestAPICapabilityRegistryDiscovery:
    """Test APICapabilityRegistry._discover_from_registry() method."""

    @pytest.mark.asyncio
    async def test_discover_tool_with_native_person_id(self, mock_tool_registry):
        """Test discovery of tool with native PersonId support."""
        registry = APICapabilityRegistry(tool_registry=mock_tool_registry)

        await registry._discover_from_registry()

        cap = registry.capabilities.get("get_UserResource")
        assert cap is not None
        assert cap.supports_person_id == ParameterSupport.NATIVE
        assert cap.returns_list is True  # 4 output_keys > 3

    @pytest.mark.asyncio
    async def test_discover_tool_with_native_vehicle_id(self, mock_tool_registry):
        """Test discovery of tool with native VehicleId support."""
        registry = APICapabilityRegistry(tool_registry=mock_tool_registry)

        await registry._discover_from_registry()

        cap = registry.capabilities.get("get_Vehicles")
        assert cap is not None
        assert cap.supports_vehicle_id == ParameterSupport.NATIVE

    @pytest.mark.asyncio
    async def test_discover_tool_with_filter_only(self, mock_tool_registry):
        """Test discovery of tool with Filter parameter only."""
        registry = APICapabilityRegistry(tool_registry=mock_tool_registry)

        await registry._discover_from_registry()

        cap = registry.capabilities.get("get_Data")
        assert cap is not None
        assert cap.supports_filter is True
        assert cap.supports_person_id == ParameterSupport.UNKNOWN
        assert cap.supports_vehicle_id == ParameterSupport.UNKNOWN

    @pytest.mark.asyncio
    async def test_discover_tool_no_params(self, mock_tool_registry):
        """Test discovery of tool with no special parameters."""
        registry = APICapabilityRegistry(tool_registry=mock_tool_registry)

        await registry._discover_from_registry()

        cap = registry.capabilities.get("get_Public")
        assert cap is not None
        assert cap.supports_person_id == ParameterSupport.NOT_SUPPORTED
        assert cap.supports_vehicle_id == ParameterSupport.NOT_SUPPORTED
        assert cap.supports_filter is False

    @pytest.mark.asyncio
    async def test_discover_user_specific_path(self, mock_tool_registry):
        """Test discovery of user-specific data based on path."""
        registry = APICapabilityRegistry(tool_registry=mock_tool_registry)

        await registry._discover_from_registry()

        cap = registry.capabilities.get("get_UserProfile")
        assert cap is not None
        assert cap.returns_user_specific_data is True

    @pytest.mark.asyncio
    async def test_discover_user_specific_output_keys(self, mock_tool_registry):
        """Test discovery of user-specific data based on output keys."""
        registry = APICapabilityRegistry(tool_registry=mock_tool_registry)

        await registry._discover_from_registry()

        # get_UserProfile has "AssignedDriver", "Owner" in output_keys
        cap = registry.capabilities.get("get_UserProfile")
        assert cap is not None
        assert cap.returns_user_specific_data is True

    @pytest.mark.asyncio
    async def test_discover_returns_list_detection(self, mock_tool_registry):
        """Test returns_list detection based on output_keys count."""
        registry = APICapabilityRegistry(tool_registry=mock_tool_registry)

        await registry._discover_from_registry()

        # get_UserResource has 4 output_keys
        cap = registry.capabilities.get("get_UserResource")
        assert cap.returns_list is True

        # get_Vehicles has 2 output_keys
        cap = registry.capabilities.get("get_Vehicles")
        assert cap.returns_list is False


# ============================================================================
# TEST: APICapabilityRegistry - get_capability
# ============================================================================

class TestAPICapabilityRegistryGetCapability:
    """Test APICapabilityRegistry.get_capability() method."""

    def test_get_capability_exists(self, sample_tool_capability):
        """Test getting existing capability."""
        registry = APICapabilityRegistry()
        registry.capabilities["get_TestResource"] = sample_tool_capability

        result = registry.get_capability("get_TestResource")

        assert result is sample_tool_capability

    def test_get_capability_not_exists(self):
        """Test getting non-existent capability returns None."""
        registry = APICapabilityRegistry()

        result = registry.get_capability("non_existent")

        assert result is None


# ============================================================================
# TEST: APICapabilityRegistry - should_inject_person_id
# ============================================================================

class TestAPICapabilityRegistryShouldInjectPersonId:
    """Test APICapabilityRegistry.should_inject_person_id() method."""

    def test_unknown_tool_returns_false(self):
        """Test that unknown tool returns (False, None, None)."""
        registry = APICapabilityRegistry()

        result = registry.should_inject_person_id("unknown_tool", "person-123")

        assert result == (False, None, None)

    def test_native_support_returns_true(self, mock_tool_registry):
        """Test native PersonId support returns correct injection info."""
        registry = APICapabilityRegistry(tool_registry=mock_tool_registry)

        # Set up capability with native support
        cap = ToolCapability(
            operation_id="get_UserResource",
            method="GET",
            path="/api/v1/myresource",
            supports_person_id=ParameterSupport.NATIVE
        )
        registry.capabilities["get_UserResource"] = cap

        result = registry.should_inject_person_id("get_UserResource", "person-123")

        assert result[0] is True
        assert result[1] == "personId"
        assert result[2] == "native"

    def test_filter_support_returns_true(self):
        """Test filter PersonId support returns correct injection info."""
        registry = APICapabilityRegistry()

        cap = ToolCapability(
            operation_id="get_Data",
            method="GET",
            path="/api/v1/data",
            supports_person_id=ParameterSupport.FILTER,
            supports_filter=True
        )
        registry.capabilities["get_Data"] = cap

        result = registry.should_inject_person_id("get_Data", "person-123")

        assert result == (True, "Filter", "filter")

    def test_not_supported_returns_false(self):
        """Test NOT_SUPPORTED returns (False, None, None)."""
        registry = APICapabilityRegistry()

        cap = ToolCapability(
            operation_id="get_Public",
            method="GET",
            path="/api/v1/public",
            supports_person_id=ParameterSupport.NOT_SUPPORTED
        )
        registry.capabilities["get_Public"] = cap

        result = registry.should_inject_person_id("get_Public", "person-123")

        assert result == (False, None, None)

    def test_unknown_support_returns_false(self):
        """Test UNKNOWN support returns (False, None, None) for safety."""
        registry = APICapabilityRegistry()

        cap = ToolCapability(
            operation_id="get_Untested",
            method="GET",
            path="/api/v1/untested",
            supports_person_id=ParameterSupport.UNKNOWN
        )
        registry.capabilities["get_Untested"] = cap

        result = registry.should_inject_person_id("get_Untested", "person-123")

        assert result == (False, None, None)

    def test_native_support_no_tool_in_registry(self):
        """Test native support when tool not in registry."""
        registry = APICapabilityRegistry()
        registry.tool_registry = MagicMock()
        registry.tool_registry.get_tool.return_value = None

        cap = ToolCapability(
            operation_id="get_Missing",
            method="GET",
            path="/api/v1/missing",
            supports_person_id=ParameterSupport.NATIVE
        )
        registry.capabilities["get_Missing"] = cap

        result = registry.should_inject_person_id("get_Missing", "person-123")

        # Falls through to return False because tool not found
        assert result == (False, None, None)


# ============================================================================
# TEST: APICapabilityRegistry - record_success
# ============================================================================

class TestAPICapabilityRegistryRecordSuccess:
    """Test APICapabilityRegistry.record_success() method."""

    def test_record_success_unknown_tool(self):
        """Test recording success for unknown tool does nothing."""
        registry = APICapabilityRegistry()

        # Should not raise exception
        registry.record_success("unknown_tool", {"param": "value"})

    def test_record_success_increments_counter(self):
        """Test that successful calls counter is incremented."""
        registry = APICapabilityRegistry()

        cap = ToolCapability(
            operation_id="get_Test",
            method="GET",
            path="/api/test"
        )
        registry.capabilities["get_Test"] = cap

        registry.record_success("get_Test", {})

        assert cap.successful_calls == 1

    def test_record_success_learns_native_person_id(self, mock_tool_registry):
        """Test learning native PersonId support from success."""
        registry = APICapabilityRegistry(tool_registry=mock_tool_registry)

        cap = ToolCapability(
            operation_id="get_UserResource",
            method="GET",
            path="/api/test",
            supports_person_id=ParameterSupport.UNKNOWN
        )
        registry.capabilities["get_UserResource"] = cap

        registry.record_success("get_UserResource", {"personId": "person-123"})

        assert cap.supports_person_id == ParameterSupport.NATIVE

    def test_record_success_learns_filter_person_id(self):
        """Test learning PersonId filter support from success."""
        registry = APICapabilityRegistry()

        cap = ToolCapability(
            operation_id="get_Data",
            method="GET",
            path="/api/data",
            supports_person_id=ParameterSupport.UNKNOWN
        )
        registry.capabilities["get_Data"] = cap

        registry.record_success("get_Data", {"Filter": "PersonId(=)abc-123"})

        assert cap.supports_person_id == ParameterSupport.FILTER

    def test_record_success_does_not_override_known(self, mock_tool_registry):
        """Test that known support level is not overridden."""
        registry = APICapabilityRegistry(tool_registry=mock_tool_registry)

        cap = ToolCapability(
            operation_id="get_UserResource",
            method="GET",
            path="/api/test",
            supports_person_id=ParameterSupport.NATIVE
        )
        registry.capabilities["get_UserResource"] = cap

        registry.record_success("get_UserResource", {"personId": "person-123"})

        # Should still be NATIVE, not changed
        assert cap.supports_person_id == ParameterSupport.NATIVE

    def test_record_success_no_tool_registry(self):
        """Test record_success when tool_registry is None."""
        registry = APICapabilityRegistry()

        cap = ToolCapability(
            operation_id="get_Test",
            method="GET",
            path="/api/test",
            supports_person_id=ParameterSupport.UNKNOWN
        )
        registry.capabilities["get_Test"] = cap

        # Should not raise, just increment counter
        registry.record_success("get_Test", {"personId": "123"})

        assert cap.successful_calls == 1


# ============================================================================
# TEST: APICapabilityRegistry - record_failure
# ============================================================================

class TestAPICapabilityRegistryRecordFailure:
    """Test APICapabilityRegistry.record_failure() method."""

    def test_record_failure_unknown_tool(self):
        """Test recording failure for unknown tool does nothing."""
        registry = APICapabilityRegistry()

        # Should not raise exception
        registry.record_failure("unknown_tool", "error", {})

    def test_record_failure_increments_counter_and_stores_error(self):
        """Test that failed calls counter is incremented and error stored."""
        registry = APICapabilityRegistry()

        cap = ToolCapability(
            operation_id="get_Test",
            method="GET",
            path="/api/test"
        )
        registry.capabilities["get_Test"] = cap

        registry.record_failure("get_Test", "Some error message", {})

        assert cap.failed_calls == 1
        assert cap.last_error == "Some error message"

    def test_record_failure_learns_person_id_not_supported(self):
        """Test learning PersonId not supported from error."""
        registry = APICapabilityRegistry()

        cap = ToolCapability(
            operation_id="get_Data",
            method="GET",
            path="/api/data",
            supports_person_id=ParameterSupport.UNKNOWN
        )
        registry.capabilities["get_Data"] = cap

        registry.record_failure(
            "get_Data",
            "Unknown filter field: PersonId",
            {"Filter": "PersonId(=)abc"}
        )

        assert cap.supports_person_id == ParameterSupport.NOT_SUPPORTED

    def test_record_failure_learns_vehicle_id_not_supported(self):
        """Test learning VehicleId not supported from error."""
        registry = APICapabilityRegistry()

        cap = ToolCapability(
            operation_id="get_Data",
            method="GET",
            path="/api/data",
            supports_vehicle_id=ParameterSupport.UNKNOWN
        )
        registry.capabilities["get_Data"] = cap

        registry.record_failure(
            "get_Data",
            "Unknown filter field: VehicleId is not valid",
            {"Filter": "VehicleId(=)xyz"}
        )

        assert cap.supports_vehicle_id == ParameterSupport.NOT_SUPPORTED

    def test_record_failure_learns_filter_syntax_error(self):
        """Test learning filter syntax error from 400 error."""
        registry = APICapabilityRegistry()

        cap = ToolCapability(
            operation_id="get_Data",
            method="GET",
            path="/api/data"
        )
        registry.capabilities["get_Data"] = cap

        registry.record_failure(
            "get_Data",
            "400 Bad Request: Invalid filter syntax",
            {"filter": "invalid_syntax"}
        )

        assert cap.learned_patterns.get("filter_syntax_error") is True


# ============================================================================
# TEST: APICapabilityRegistry - Cache Operations
# ============================================================================

class TestAPICapabilityRegistryCache:
    """Test APICapabilityRegistry cache operations."""

    @pytest.mark.asyncio
    async def test_load_cache_file_not_exists(self):
        """Test _load_cache returns False when file doesn't exist."""
        registry = APICapabilityRegistry()

        with patch.object(Path, 'exists', return_value=False):
            result = await registry._load_cache()

        assert result is False
        assert registry.capabilities == {}

    @pytest.mark.asyncio
    async def test_load_cache_success(self, sample_tool_capability_dict):
        """Test _load_cache successfully loads capabilities."""
        registry = APICapabilityRegistry()

        cache_data = {
            "version": "1.0",
            "capabilities": [sample_tool_capability_dict]
        }

        with patch.object(Path, 'exists', return_value=True):
            with patch('builtins.open', mock_open(read_data=json.dumps(cache_data))):
                result = await registry._load_cache()

        assert result is True
        assert "get_TestResource" in registry.capabilities
        cap = registry.capabilities["get_TestResource"]
        assert cap.operation_id == "get_TestResource"
        assert cap.supports_person_id == ParameterSupport.NATIVE

    @pytest.mark.asyncio
    async def test_load_cache_exception(self):
        """Test _load_cache returns False on exception."""
        registry = APICapabilityRegistry()

        with patch.object(Path, 'exists', return_value=True):
            with patch('builtins.open', side_effect=IOError("Read error")):
                result = await registry._load_cache()

        assert result is False

    @pytest.mark.asyncio
    async def test_load_cache_invalid_json(self):
        """Test _load_cache handles invalid JSON."""
        registry = APICapabilityRegistry()

        with patch.object(Path, 'exists', return_value=True):
            with patch('builtins.open', mock_open(read_data="invalid json {")):
                result = await registry._load_cache()

        assert result is False

    @pytest.mark.asyncio
    async def test_save_cache_success(self, sample_tool_capability):
        """Test _save_cache successfully saves capabilities."""
        registry = APICapabilityRegistry()
        registry.capabilities["get_TestResource"] = sample_tool_capability

        mock_file = mock_open()
        with patch.object(Path, 'mkdir') as mock_mkdir:
            with patch('builtins.open', mock_file):
                await registry._save_cache()

        mock_mkdir.assert_called_once()
        mock_file.assert_called_once()

        # Verify JSON was written
        written_data = ''.join(
            call.args[0] for call in mock_file().write.call_args_list
        )
        parsed = json.loads(written_data)
        assert parsed["version"] == "1.0"
        assert len(parsed["capabilities"]) == 1
        assert parsed["capabilities"][0]["operation_id"] == "get_TestResource"

    @pytest.mark.asyncio
    async def test_save_cache_exception(self, sample_tool_capability):
        """Test _save_cache handles exception gracefully."""
        registry = APICapabilityRegistry()
        registry.capabilities["get_TestResource"] = sample_tool_capability

        with patch.object(Path, 'mkdir', side_effect=OSError("Cannot create directory")):
            # Should not raise exception
            await registry._save_cache()

    @pytest.mark.asyncio
    async def test_save_public_method(self, sample_tool_capability):
        """Test public save() method calls _save_cache()."""
        registry = APICapabilityRegistry()
        registry.capabilities["get_TestResource"] = sample_tool_capability

        with patch.object(registry, '_save_cache', new_callable=AsyncMock) as mock_save:
            await registry.save()

        mock_save.assert_called_once()


# ============================================================================
# TEST: Global Functions
# ============================================================================

class TestGlobalFunctions:
    """Test module-level functions."""

    @pytest.fixture
    def _capability_module_isolated(self):
        """Fixture-based save/restore of the module-level singleton.

        Replaces hand-rolled try/finally save/restore in each test —
        with try/finally, an assertion failure inside the test body
        STILL restores correctly, but a crash between save and
        try-block (e.g. import error) leaks the polluted state.
        Pytest fixtures handle teardown via the test runner so the
        restore happens even on hard crashes.
        """
        import services.api_capabilities as module
        original = module._capability_registry
        try:
            yield module
        finally:
            module._capability_registry = original

    def test_get_capability_registry_initial(self, _capability_module_isolated):
        """Test get_capability_registry returns None initially."""
        _capability_module_isolated._capability_registry = None
        result = get_capability_registry()
        assert result is None

    def test_get_capability_registry_after_init(self, _capability_module_isolated):
        """Test get_capability_registry returns initialized registry."""
        mock_registry = APICapabilityRegistry()
        _capability_module_isolated._capability_registry = mock_registry
        result = get_capability_registry()
        assert result is mock_registry

    @pytest.mark.asyncio
    async def test_initialize_capability_registry(self, mock_tool_registry):
        """Test initialize_capability_registry creates and initializes registry."""
        import services.api_capabilities as module
        original = module._capability_registry

        try:
            with patch.object(APICapabilityRegistry, 'initialize', new_callable=AsyncMock) as mock_init:
                mock_init.return_value = True

                result = await initialize_capability_registry(mock_tool_registry)

            assert isinstance(result, APICapabilityRegistry)
            assert result.tool_registry is mock_tool_registry
            mock_init.assert_called_once()

            # Check global was set
            assert module._capability_registry is result
        finally:
            module._capability_registry = original


# ============================================================================
# INTEGRATION TESTS
# ============================================================================

class TestAPICapabilityRegistryIntegration:
    """Integration tests for APICapabilityRegistry."""

    @pytest.mark.asyncio
    async def test_full_workflow(self, mock_tool_registry):
        """Test complete workflow: init, discover, learn, save, load."""
        registry = APICapabilityRegistry(tool_registry=mock_tool_registry)

        # Step 1: Discover from registry
        with patch.object(registry, '_load_cache', new_callable=AsyncMock, return_value=False):
            with patch.object(registry, '_save_cache', new_callable=AsyncMock):
                await registry.initialize()

        assert len(registry.capabilities) > 0

        # Step 2: Record some success and failure
        registry.record_success("get_UserResource", {"personId": "123"})
        registry.record_failure(
            "get_Data",
            "Unknown filter field: PersonId",
            {"Filter": "PersonId(=)123"}
        )

        # Verify learning happened
        cap = registry.get_capability("get_Data")
        assert cap.supports_person_id == ParameterSupport.NOT_SUPPORTED

    @pytest.mark.asyncio
    async def test_capability_detection_all_paths(self):
        """Test all detection paths in _discover_from_registry."""
        # Create tools that exercise all detection paths
        tool_native_person = MagicMock()
        tool_native_person.method = "GET"
        tool_native_person.path = "/api/v1/resource"
        tool_native_person.output_keys = ["Id"]
        person_param = MagicMock()
        person_param.context_key = "person_id"
        tool_native_person.parameters = {"personId": person_param}

        tool_native_vehicle = MagicMock()
        tool_native_vehicle.method = "GET"
        tool_native_vehicle.path = "/api/v1/vehicle"
        tool_native_vehicle.output_keys = ["Id"]
        vehicle_param = MagicMock()
        vehicle_param.context_key = "vehicle_id"
        tool_native_vehicle.parameters = {"vehicleId": vehicle_param}

        tool_filter_only = MagicMock()
        tool_filter_only.method = "GET"
        tool_filter_only.path = "/api/v1/data"
        tool_filter_only.output_keys = ["Id"]
        filter_param = MagicMock()
        filter_param.context_key = None
        tool_filter_only.parameters = {"Filter": filter_param}

        tool_no_support = MagicMock()
        tool_no_support.method = "GET"
        tool_no_support.path = "/api/v1/public"
        tool_no_support.output_keys = None
        tool_no_support.parameters = {}

        tool_driver_path = MagicMock()
        tool_driver_path.method = "GET"
        tool_driver_path.path = "/api/v1/driver/profile"
        tool_driver_path.output_keys = None
        tool_driver_path.parameters = {}

        mock_registry = MagicMock()
        mock_registry.tools = {
            "get_NativePerson": tool_native_person,
            "get_NativeVehicle": tool_native_vehicle,
            "get_FilterOnly": tool_filter_only,
            "get_NoSupport": tool_no_support,
            "get_DriverPath": tool_driver_path
        }

        registry = APICapabilityRegistry(tool_registry=mock_registry)
        await registry._discover_from_registry()

        # Verify all paths were hit
        assert registry.capabilities["get_NativePerson"].supports_person_id == ParameterSupport.NATIVE
        assert registry.capabilities["get_NativeVehicle"].supports_vehicle_id == ParameterSupport.NATIVE
        assert registry.capabilities["get_FilterOnly"].supports_filter is True
        assert registry.capabilities["get_FilterOnly"].supports_person_id == ParameterSupport.UNKNOWN
        assert registry.capabilities["get_NoSupport"].supports_person_id == ParameterSupport.NOT_SUPPORTED
        assert registry.capabilities["get_DriverPath"].returns_user_specific_data is True
