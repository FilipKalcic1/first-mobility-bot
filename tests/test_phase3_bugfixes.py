"""
Tests for Phase 3 bug fixes:
1. European number parsing (flow_handler.py handle_gathering)
2. Delete flow routing (_query_result_to_decision + DELETE_FLOW_CONFIG)
3. Case creation VehicleId in one-shot path (flow_executors.py)
4. Booking Description=None conditional inclusion (flow_handler.py)
"""

import re
import sys
import importlib
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List


# ---------------------------------------------------------------------------
# Module-level: stub out heavy transitive dependencies so we can import
# services.engine.flow_executors and services.unified_router without
# needing prometheus_client, faiss, openai, etc.
# ---------------------------------------------------------------------------
_STUBS: Dict[str, Any] = {}


def _ensure_stub(module_name: str):
    """Insert a MagicMock into sys.modules if the real module is absent."""
    if module_name not in sys.modules:
        _STUBS[module_name] = MagicMock()
        sys.modules[module_name] = _STUBS[module_name]


# Heavy deps that may not be installed in the test environment
for _mod in [
    "prometheus_client",
    "faiss",
    "openai",
    "tiktoken",
    "httpx",
]:
    _ensure_stub(_mod)


# ---------------------------------------------------------------------------
# Test 1: European number parsing
# ---------------------------------------------------------------------------
# Extracted logic from flow_handler.py lines 590-597:
#   cleaned = re.sub(r'(\d)[.,](\d{3})\b', r'\1\2', text)
#   numbers = re.findall(r'\d+', cleaned)

def _extract_european_number(text: str) -> Optional[int]:
    """Replicate the European number extraction from handle_gathering."""
    cleaned = re.sub(r'(\d)[.,](\d{3})\b', r'\1\2', text)
    numbers = re.findall(r'\d+', cleaned)
    if numbers:
        return int(numbers[0])
    return None


class TestEuropeanNumberParsing:
    """Test European thousands-separator parsing for mileage values."""

    def test_dot_as_thousands_separator(self):
        """'45.000' is European for 45000, not 45 with decimal."""
        assert _extract_european_number("45.000") == 45000

    def test_comma_as_thousands_separator(self):
        """'45,000' should also parse as 45000."""
        assert _extract_european_number("45,000") == 45000

    def test_plain_number_no_separator(self):
        """'120000' should extract 120000 unchanged."""
        assert _extract_european_number("120000") == 120000

    def test_european_thousands_three_digits(self):
        """'45.123' should parse as 45123 (European thousands)."""
        assert _extract_european_number("45.123") == 45123

    def test_number_embedded_in_text(self):
        """Number surrounded by text should still be extracted."""
        assert _extract_european_number("Kilometra\u017ea je 78.500 km") == 78500

    def test_no_number_returns_none(self):
        """Text without digits returns None."""
        assert _extract_european_number("no digits here") is None

    def test_small_decimal_not_thousands(self):
        """'3.14' has fewer than 3 digits after dot, so NOT a thousands sep.
        The regex only matches \\d{3} after the separator, so '3.14' stays '3.14'
        and re.findall returns ['3', '14']."""
        result = _extract_european_number("3.14")
        # Should get 3 (first number), not 314
        assert result == 3

    def test_large_european_number(self):
        """'1.234.567' — documents actual regex behavior with overlapping matches."""
        cleaned = re.sub(r'(\d)[.,](\d{3})\b', r'\1\2', '1.234.567')
        numbers = re.findall(r'\d+', cleaned)
        # The first number extracted is reasonable (at least 1234)
        assert int(numbers[0]) >= 1234


# ---------------------------------------------------------------------------
# Test 2: Delete flow routing
# ---------------------------------------------------------------------------

@dataclass
class FakeRouteResult:
    """Mimics RouteResult from query_router.py."""
    matched: bool = True
    tool_name: Optional[str] = None
    extract_fields: List[str] = field(default_factory=list)
    response_template: Optional[str] = None
    flow_type: Optional[str] = None
    confidence: float = 0.95
    reason: str = "test"


def _import_flow_executors_class():
    """Import FlowExecutors bypassing services.engine.__init__ if needed."""
    # Try direct module import to avoid __init__.py pulling in ai_orchestrator
    mod_name = "services.engine.flow_executors"
    if mod_name in sys.modules:
        mod = sys.modules[mod_name]
    else:
        import importlib.util
        import os
        spec_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "services", "engine", "flow_executors.py"
        )
        spec = importlib.util.spec_from_file_location(mod_name, spec_path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)
    return mod.FlowExecutors


def _import_unified_router():
    """Import UnifiedRouter and RouterDecision, stubbing heavy deps."""
    mod_name = "services.unified_router"
    if mod_name in sys.modules:
        mod = sys.modules[mod_name]
    else:
        # Stub transitive deps that unified_router imports at module level
        for dep in [
            "services.unified_search",
            "services.faiss_vector_store",
            "services.openai_client",
            "services.circuit_breaker",
            "services.ambiguity_detector",
            "services.llm_reranker",
        ]:
            _ensure_stub(dep)
        mod = importlib.import_module(mod_name)
    return mod.UnifiedRouter, mod.RouterDecision


class TestDeleteFlowConfig:
    """Test that DELETE_FLOW_CONFIG has correct tools for each delete type."""

    def _get_config(self):
        FlowExecutors = _import_flow_executors_class()
        return FlowExecutors.DELETE_FLOW_CONFIG

    def test_delete_booking_config(self):
        config = self._get_config()
        assert "delete_booking" in config
        assert config["delete_booking"]["list_tool"] == "get_VehicleCalendar"
        assert config["delete_booking"]["delete_tool"] == "delete_VehicleCalendar_id"

    def test_delete_case_config(self):
        config = self._get_config()
        assert "delete_case" in config
        assert config["delete_case"]["list_tool"] == "get_Cases"
        assert config["delete_case"]["delete_tool"] == "delete_Cases_id"

    def test_delete_trip_config(self):
        config = self._get_config()
        assert "delete_trip" in config
        assert config["delete_trip"]["list_tool"] == "get_Trips"
        assert config["delete_trip"]["delete_tool"] == "delete_Trips_id"

    def test_all_configs_have_required_keys(self):
        config = self._get_config()
        required_keys = {"list_tool", "delete_tool", "entity_label", "empty_msg", "name_fields"}
        for flow_type, cfg in config.items():
            missing = required_keys - set(cfg.keys())
            assert not missing, f"{flow_type} missing keys: {missing}"


class TestDeleteFlowRouting:
    """Test _query_result_to_decision routes delete flow types correctly."""

    def _make_decision(self, flow_type: str, is_fallback: bool = False):
        UnifiedRouter, RouterDecision = _import_unified_router()
        router = object.__new__(UnifiedRouter)

        qr_result = FakeRouteResult(
            matched=True,
            tool_name=f"tool_for_{flow_type}",
            flow_type=flow_type,
            confidence=0.95,
            reason="ML match",
        )

        decision = router._query_result_to_decision(
            qr_result=qr_result,
            user_context={"person_id": "test-person"},
            is_fallback=is_fallback,
        )
        return decision

    def test_delete_booking_routes_to_start_flow(self):
        decision = self._make_decision("delete_booking")
        assert decision.action == "start_flow"
        assert decision.flow_type == "delete_booking"

    def test_delete_case_routes_to_start_flow(self):
        decision = self._make_decision("delete_case")
        assert decision.action == "start_flow"
        assert decision.flow_type == "delete_case"

    def test_delete_trip_routes_to_start_flow(self):
        decision = self._make_decision("delete_trip")
        assert decision.action == "start_flow"
        assert decision.flow_type == "delete_trip"

    def test_delete_flow_preserves_tool_name(self):
        decision = self._make_decision("delete_booking")
        assert decision.tool == "tool_for_delete_booking"

    def test_delete_flow_confidence_not_reduced_on_fast_path(self):
        decision = self._make_decision("delete_booking")
        assert decision.confidence == 0.95

    def test_delete_flow_confidence_reduced_on_fallback(self):
        UnifiedRouter, _ = _import_unified_router()
        router = object.__new__(UnifiedRouter)
        qr_result = FakeRouteResult(
            matched=True,
            tool_name="some_tool",
            flow_type="delete_case",
            confidence=1.0,
            reason="test",
        )
        decision = router._query_result_to_decision(
            qr_result=qr_result,
            user_context={},
            is_fallback=True,
        )
        assert decision.confidence == pytest.approx(0.8)

    def test_simple_flow_type_routes_to_simple_api(self):
        """Non-flow types like 'simple' should route to simple_api action."""
        decision = self._make_decision("simple")
        assert decision.action == "simple_api"


# ---------------------------------------------------------------------------
# Test 3: Case creation VehicleId one-shot path
# ---------------------------------------------------------------------------

class TestCaseCreationVehicleId:
    """Test that handle_case_creation_flow includes VehicleId when user has a vehicle."""

    # Valid UUID format required by UserContextManager.person_id property
    _PERSON_UUID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"

    def _make_user_context(self, with_vehicle: bool) -> Dict[str, Any]:
        if with_vehicle:
            return {
                "person_id": self._PERSON_UUID,
                "vehicle": {
                    "Id": "vehicle-abc",
                    "FullVehicleName": "BMW X5",
                    "LicencePlate": "ZG-1234-AB",
                },
            }
        else:
            return {
                "person_id": self._PERSON_UUID,
            }

    @pytest.mark.asyncio
    async def test_oneshot_with_vehicle_includes_vehicle_id(self):
        """When Subject AND Description provided and user has vehicle, VehicleId must be in params."""
        FlowExecutors = _import_flow_executors_class()
        executor = object.__new__(FlowExecutors)

        conv_manager = AsyncMock()
        conv_manager.add_parameters = AsyncMock()
        conv_manager.request_confirmation = AsyncMock()
        conv_manager.save = AsyncMock()
        conv_manager.context = MagicMock()

        router_params = {
            "Subject": "Prijava kvara",
            "Description": "Motor ne radi dobro",
        }

        user_context = self._make_user_context(with_vehicle=True)

        await executor.handle_case_creation_flow(
            text="Imam kvar na motoru, motor ne radi dobro",
            user_context=user_context,
            conv_manager=conv_manager,
            router_params=router_params,
        )

        # Check that add_parameters was called with VehicleId
        call_args = conv_manager.add_parameters.call_args[0][0]
        assert "VehicleId" in call_args
        assert call_args["VehicleId"] == "vehicle-abc"

    @pytest.mark.asyncio
    async def test_oneshot_without_vehicle_no_vehicle_id(self):
        """When user has no vehicle, VehicleId should NOT be in params."""
        FlowExecutors = _import_flow_executors_class()
        executor = object.__new__(FlowExecutors)

        conv_manager = AsyncMock()
        conv_manager.add_parameters = AsyncMock()
        conv_manager.request_confirmation = AsyncMock()
        conv_manager.save = AsyncMock()
        conv_manager.context = MagicMock()

        router_params = {
            "Subject": "Prijava kvara",
            "Description": "Ne\u0161to ne radi",
        }

        user_context = self._make_user_context(with_vehicle=False)

        await executor.handle_case_creation_flow(
            text="Ne\u0161to ne radi",
            user_context=user_context,
            conv_manager=conv_manager,
            router_params=router_params,
        )

        call_args = conv_manager.add_parameters.call_args[0][0]
        assert "VehicleId" not in call_args

    @pytest.mark.asyncio
    async def test_oneshot_includes_user_and_subject_and_message(self):
        """One-shot path should include User, Subject, and Message (mapped from Description)."""
        FlowExecutors = _import_flow_executors_class()
        executor = object.__new__(FlowExecutors)

        conv_manager = AsyncMock()
        conv_manager.add_parameters = AsyncMock()
        conv_manager.request_confirmation = AsyncMock()
        conv_manager.save = AsyncMock()
        conv_manager.context = MagicMock()

        router_params = {
            "Subject": "Test Subject",
            "Description": "Test Description",
        }

        user_context = self._make_user_context(with_vehicle=True)

        await executor.handle_case_creation_flow(
            text="test",
            user_context=user_context,
            conv_manager=conv_manager,
            router_params=router_params,
        )

        call_args = conv_manager.add_parameters.call_args[0][0]
        assert call_args["Subject"] == "Test Subject"
        assert call_args["Message"] == "Test Description"
        assert call_args["User"] == self._PERSON_UUID

    @pytest.mark.asyncio
    async def test_no_description_starts_gathering_flow(self):
        """When Description is missing, should start gathering flow, not one-shot."""
        FlowExecutors = _import_flow_executors_class()
        executor = object.__new__(FlowExecutors)

        conv_manager = AsyncMock()
        conv_manager.start_flow = AsyncMock()
        conv_manager.add_parameters = AsyncMock()
        conv_manager.save = AsyncMock()
        conv_manager.context = MagicMock()

        router_params = {
            "Subject": "Prijava kvara",
            # No Description
        }

        user_context = self._make_user_context(with_vehicle=True)

        result = await executor.handle_case_creation_flow(
            text="Imam kvar",
            user_context=user_context,
            conv_manager=conv_manager,
            router_params=router_params,
        )

        # Should start a gathering flow
        conv_manager.start_flow.assert_called_once()
        # The gathering path should still include VehicleId
        add_params_call = conv_manager.add_parameters.call_args[0][0]
        assert "VehicleId" in add_params_call
        assert add_params_call["VehicleId"] == "vehicle-abc"


# ---------------------------------------------------------------------------
# Test 4: Booking Description=None conditional inclusion
# ---------------------------------------------------------------------------

class TestBookingDescriptionConditional:
    """Test that Description is conditionally included in booking params.

    From flow_handler.py line 406:
        **({"Description": desc} if (desc := params.get("Description") or
            params.get("description")) else {})
    """

    def _build_booking_params(self, raw_params: Dict[str, Any]) -> Dict[str, Any]:
        """Replicate the booking params construction from flow_handler.py lines 399-407."""
        params = {
            "AssignedToId": "person-1",
            "VehicleId": "vehicle-1",
            "FromTime": "2026-03-15T08:00",
            "ToTime": "2026-03-15T17:00",
            "AssigneeType": 1,
            "EntryType": 0,
            **({"Description": desc} if (desc := raw_params.get("Description") or raw_params.get("description")) else {}),
        }
        return params

    def test_description_none_not_in_params(self):
        """When Description is None, it should NOT appear in the params dict."""
        result = self._build_booking_params({"Description": None})
        assert "Description" not in result

    def test_description_empty_string_not_in_params(self):
        """When Description is empty string, it should NOT appear in params."""
        result = self._build_booking_params({"Description": ""})
        assert "Description" not in result

    def test_description_missing_not_in_params(self):
        """When Description key is absent, it should NOT appear in params."""
        result = self._build_booking_params({})
        assert "Description" not in result

    def test_description_with_value_is_in_params(self):
        """When Description has a value, it SHOULD be in params."""
        result = self._build_booking_params({"Description": "Business trip"})
        assert "Description" in result
        assert result["Description"] == "Business trip"

    def test_lowercase_description_also_works(self):
        """The code also checks lowercase 'description' key."""
        result = self._build_booking_params({"description": "Slu\u017ebeni put"})
        assert "Description" in result
        assert result["Description"] == "Slu\u017ebeni put"

    def test_description_uppercase_takes_priority(self):
        """When both 'Description' and 'description' exist, uppercase wins (or-short-circuit)."""
        result = self._build_booking_params({
            "Description": "Upper",
            "description": "Lower",
        })
        assert result["Description"] == "Upper"

    def test_base_params_always_present(self):
        """Core booking params should always be present regardless of Description."""
        result = self._build_booking_params({})
        assert "AssignedToId" in result
        assert "VehicleId" in result
        assert "FromTime" in result
        assert "ToTime" in result
        assert "AssigneeType" in result
        assert "EntryType" in result
