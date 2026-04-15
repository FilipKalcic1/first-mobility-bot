"""
Test Configuration and Fixtures
"""

import pytest
from typing import AsyncGenerator, Dict, Any
from unittest.mock import AsyncMock, MagicMock, patch


# NOTE: No custom event_loop fixture needed.
# pytest-asyncio with asyncio_mode=auto (pytest.ini) manages event loops automatically.


# ============================================================================
# MOCK FIXTURES
# ============================================================================

@pytest.fixture
def mock_redis():
    """Mock Redis client."""
    redis = MagicMock()
    redis.ping = AsyncMock(return_value=True)
    redis.get = AsyncMock(return_value=None)
    redis.set = AsyncMock(return_value=True)
    redis.setex = AsyncMock(return_value=True)
    redis.delete = AsyncMock(return_value=True)
    redis.rpush = AsyncMock(return_value=1)
    redis.lpop = AsyncMock(return_value=None)
    redis.blpop = AsyncMock(return_value=None)
    redis.lrange = AsyncMock(return_value=[])
    redis.xadd = AsyncMock(return_value="1234567890-0")
    redis.xreadgroup = AsyncMock(return_value=[])
    redis.xgroup_create = AsyncMock(return_value=True)
    redis.xack = AsyncMock(return_value=1)
    redis.xdel = AsyncMock(return_value=1)
    redis.aclose = AsyncMock()
    return redis


@pytest.fixture
def mock_db_session():
    """Mock database session."""
    session = MagicMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.close = AsyncMock()
    return session


@pytest.fixture
def mock_gateway():
    """Mock API Gateway."""
    gateway = MagicMock()
    gateway.execute = AsyncMock()
    gateway.get = AsyncMock()
    gateway.post = AsyncMock()
    gateway.close = AsyncMock()
    return gateway


@pytest.fixture
def mock_registry():
    """Mock Tool Registry."""
    registry = MagicMock()
    registry.tools = {}
    registry.get_tool = MagicMock(return_value=None)
    return registry


# ============================================================================
# SAMPLE DATA FIXTURES
# ============================================================================

@pytest.fixture
def sample_user_context() -> Dict[str, Any]:
    """Sample user context."""
    return {
        "person_id": "test-person-id-12345678",
        "phone": "+385991234567",
        "tenant_id": "test-tenant-id-abcdefgh",
        "display_name": "Test User",
        "vehicle": {
            "id": "vehicle-123",
            "plate": "ZG-1234-AB",
            "name": "VW Passat",
            "mileage": "50000"
        }
    }


@pytest.fixture
def sample_tool_definition() -> Dict[str, Any]:
    """Sample tool definition."""
    return {
        "operationId": "get_VehicleAvailability",
        "method": "GET",
        "path": "/vehiclemgt/api/v2/vehicles/availability",
        "description": "Get available vehicles for a time period",
        "parameters": {
            "FromTime": {
                "type": "string",
                "format": "date-time",
                "in": "query",
                "required": True
            },
            "ToTime": {
                "type": "string",
                "format": "date-time",
                "in": "query",
                "required": True
            },
            "personId": {
                "type": "string",
                "in": "query"
            }
        },
        "required": ["FromTime", "ToTime"],
        "auto_inject": ["personId"]
    }


@pytest.fixture
def sample_vehicle_list() -> list:
    """Sample vehicle list."""
    return [
        {
            "Id": "vehicle-001",
            "FullVehicleName": "VW Passat 2020",
            "LicencePlate": "ZG-1234-AB",
            "VIN": "WVWZZZ123456789",
            "Mileage": 50000
        },
        {
            "Id": "vehicle-002",
            "FullVehicleName": "Škoda Octavia 2021",
            "LicencePlate": "ZG-5678-CD",
            "VIN": "TMBZZZ987654321",
            "Mileage": 35000
        }
    ]


@pytest.fixture
def sample_sensitive_data() -> Dict[str, Any]:
    """Sample data with sensitive fields."""
    return {
        "name": "Test User",
        "email": "test.user@example.com",
        "phone": "+385991234567",
        "password": "secret123",
        "api_key": "sk-1234567890abcdef",
        "oib": "12345678901",
        "credit_card": "4111-1111-1111-1111",
        "nested": {
            "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.test",
            "safe_field": "this is safe"
        }
    }


# ============================================================================
# ENVIRONMENT FIXTURES
# ============================================================================

@pytest.fixture
def mock_env_vars(monkeypatch):
    """Set mock environment variables."""
    env_vars = {
        "APP_ENV": "testing",
        "DEBUG": "true",
        "DATABASE_URL": "postgresql+asyncpg://test:test@localhost/test_db",
        "REDIS_URL": "redis://localhost:6379/1",
        "MOBILITY_API_URL": "https://test-api.example.com",
        "MOBILITY_AUTH_URL": "https://test-api.example.com/sso/connect/token",
        "MOBILITY_CLIENT_ID": "test_client",
        "MOBILITY_CLIENT_SECRET": "test_secret",
        "MOBILITY_TENANT_ID": "test-tenant-id",
        "AZURE_OPENAI_ENDPOINT": "https://test.openai.azure.com",
        "AZURE_OPENAI_API_KEY": "test-api-key",
        "INFOBIP_API_KEY": "test-infobip-key",
        "INFOBIP_SECRET_KEY": "test-webhook-secret",
    }

    for key, value in env_vars.items():
        monkeypatch.setenv(key, value)

    return env_vars


# ============================================================================
# TOOL EXECUTOR FIXTURES
# ============================================================================

@pytest.fixture
def sample_tool() -> "UnifiedToolDefinition":
    """Sample unified tool definition."""
    from services.tool_contracts import UnifiedToolDefinition, ParameterDefinition, DependencySource

    return UnifiedToolDefinition(
        operation_id="get_Vehicles",
        service_name="automation",
        swagger_name="automation",
        service_url="/automation",
        path="/api/v2/vehicles",
        method="GET",
        description="Get list of vehicles",
        parameters={
            "status": ParameterDefinition(
                name="status",
                param_type="string",
                description="Vehicle status",
                required=False,
                location="query",
                dependency_source=DependencySource.FROM_USER
            )
        },
        required_params=[],
        output_keys=["Id", "LicencePlate", "FullVehicleName"]
    )


@pytest.fixture
def sample_tool_with_injection() -> "UnifiedToolDefinition":
    """Sample tool definition with person_id injection."""
    from services.tool_contracts import UnifiedToolDefinition, ParameterDefinition, DependencySource

    return UnifiedToolDefinition(
        operation_id="get_VehicleCalendar",
        service_name="automation",
        swagger_name="automation",
        service_url="/automation",
        path="/api/v2/vehiclecalendar",
        method="GET",
        description="Get vehicle calendar entries",
        parameters={
            "FromTime": ParameterDefinition(
                name="FromTime",
                param_type="string",
                format="date-time",
                description="Start time",
                required=True,
                location="query",
                dependency_source=DependencySource.FROM_USER
            ),
            "ToTime": ParameterDefinition(
                name="ToTime",
                param_type="string",
                format="date-time",
                description="End time",
                required=True,
                location="query",
                dependency_source=DependencySource.FROM_USER
            ),
            "AssignedToId": ParameterDefinition(
                name="AssignedToId",
                param_type="string",
                description="Person ID",
                required=False,
                location="query",
                dependency_source=DependencySource.FROM_CONTEXT,
                context_key="person_id"
            )
        },
        required_params=["FromTime", "ToTime"],
        output_keys=["Id", "From", "To"]
    )
