"""Tests for services/engine/tool_handler.py – ToolHandler."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from services.engine.tool_handler import ToolHandler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_handler():
    registry = MagicMock()
    registry.CONTEXT_PARAM_FALLBACK = {
        "VehicleId": "vehicle_id",
        "PersonId": "person_id",
    }
    executor = AsyncMock()
    dep_resolver = MagicMock()
    dep_resolver.detect_value_type = MagicMock(return_value=None)
    dep_resolver.resolve_dependency = AsyncMock()
    error_learning = AsyncMock()
    error_learning.record_error = AsyncMock()
    error_learning.suggest_correction = AsyncMock(return_value=None)
    formatter = MagicMock()
    formatter.format_result = MagicMock(return_value="Formatted result")

    handler = ToolHandler(registry, executor, dep_resolver, error_learning, formatter)
    return handler


def _user_context():
    return {
        "person_id": "00000000-0000-0000-0000-000000000001",
        "phone": "+385991234567",
        "tenant_id": "t1",
    }


def _conv():
    conv = MagicMock()
    conv.context = MagicMock()
    conv.context.tool_outputs = {}
    conv.save = AsyncMock()
    return conv


@pytest.fixture
def handler():
    return _make_handler()


# ===========================================================================
# __init__ & _load_context_param_examples
# ===========================================================================

class TestInit:
    def test_init(self, handler):
        assert handler.MAX_CHAIN_DEPTH == 3
        assert "VehicleId" in handler._context_param_examples

    def test_no_registry_fallback(self):
        h = ToolHandler(None, AsyncMock(), MagicMock(), AsyncMock(), MagicMock())
        assert "VehicleId" in h._context_param_examples

    def test_empty_context_param_fallback(self):
        reg = MagicMock()
        reg.CONTEXT_PARAM_FALLBACK = {}
        h = ToolHandler(reg, AsyncMock(), MagicMock(), AsyncMock(), MagicMock())
        assert "VehicleId" in h._context_param_examples


# ===========================================================================
# requires_confirmation
# ===========================================================================

class TestRequiresConfirmation:
    def test_post_method(self, handler):
        assert handler.requires_confirmation("any_tool", method="POST") is True

    def test_put_method(self, handler):
        assert handler.requires_confirmation("any_tool", method="PUT") is True

    def test_delete_method(self, handler):
        assert handler.requires_confirmation("any_tool", method="DELETE") is True

    def test_get_method(self, handler):
        assert handler.requires_confirmation("get_tool", method="GET") is False

    def test_tool_name_prefix_post(self, handler):
        handler.registry.get_tool.return_value = None
        assert handler.requires_confirmation("post_VehicleCalendar") is True

    def test_tool_name_prefix_delete(self, handler):
        handler.registry.get_tool.return_value = None
        assert handler.requires_confirmation("delete_Something") is True

    def test_calendar_pattern(self, handler):
        handler.registry.get_tool.return_value = None
        assert handler.requires_confirmation("get_calendar_view") is True

    def test_registry_method_check(self, handler):
        tool = MagicMock()
        tool.method = "POST"
        handler.registry.get_tool.return_value = tool
        assert handler.requires_confirmation("create_booking") is True

    def test_get_no_pattern(self, handler):
        tool = MagicMock()
        tool.method = "GET"
        handler.registry.get_tool.return_value = tool
        assert handler.requires_confirmation("get_Vehicles") is False


# ===========================================================================
# _extract_missing_param_from_error
# ===========================================================================

class TestExtractMissingParam:
    def test_pattern_parametri(self, handler):
        result = handler._extract_missing_param_from_error("Nedostaje parametri: VehicleId")
        assert result == "VehicleId"

    def test_pattern_unesite(self, handler):
        result = handler._extract_missing_param_from_error("Molim unesite Vehicle Id:")
        assert result == "VehicleId"

    def test_context_param_match(self, handler):
        result = handler._extract_missing_param_from_error("Trebam PersonId za nastavak")
        assert result == "PersonId"

    def test_vozilo_keyword(self, handler):
        result = handler._extract_missing_param_from_error("Nedostaje vozilo za operaciju")
        assert result == "VehicleId"

    def test_osoba_keyword(self, handler):
        result = handler._extract_missing_param_from_error("Trebam podatke o osoba za nastavak")
        assert result == "PersonId"

    def test_vozac_keyword(self, handler):
        result = handler._extract_missing_param_from_error("Trebam podatke o vozacu")
        assert result == "PersonId"

    def test_no_match(self, handler):
        assert handler._extract_missing_param_from_error("Nepoznata greska") is None

    def test_none_message(self, handler):
        assert handler._extract_missing_param_from_error(None) is None

    def test_empty_message(self, handler):
        assert handler._extract_missing_param_from_error("") is None


# ===========================================================================
# _find_resolvable_value
# ===========================================================================

class TestFindResolvableValue:
    def test_vehicle_plate_param(self, handler):
        result = handler._find_resolvable_value(
            {"plate": "ZG-1234-AB"}, "VehicleId"
        )
        assert result == "ZG-1234-AB"

    def test_vehicle_name_param(self, handler):
        result = handler._find_resolvable_value(
            {"name": "Golf"}, "VehicleId"
        )
        assert result == "Golf"

    def test_vehicle_no_string_value(self, handler):
        result = handler._find_resolvable_value(
            {"plate": 123}, "VehicleId"
        )
        assert result is None

    def test_person_email(self, handler):
        result = handler._find_resolvable_value(
            {"email": "test@test.com"}, "PersonId"
        )
        assert result == "test@test.com"

    def test_driver_phone(self, handler):
        result = handler._find_resolvable_value(
            {"phone": "+385991234567"}, "DriverId"
        )
        assert result == "+385991234567"

    def test_no_match(self, handler):
        result = handler._find_resolvable_value(
            {"random": "value"}, "LocationId"
        )
        assert result is None

    def test_detect_value_type_match(self, handler):
        handler.dependency_resolver.detect_value_type.return_value = ("vehicleid", "LicencePlate")
        result = handler._find_resolvable_value(
            {"Filter": "ZG-1234-AB"}, "VehicleId"
        )
        assert result == "ZG-1234-AB"


# ===========================================================================
# _inject_person_filter
# ===========================================================================

class TestInjectPersonFilter:
    def test_non_get_skipped(self, handler):
        tool = MagicMock()
        tool.method = "POST"
        result = handler._inject_person_filter(tool, {"key": "val"}, _user_context())
        assert result == {"key": "val"}

    def test_no_person_id(self, handler):
        tool = MagicMock()
        tool.method = "GET"
        result = handler._inject_person_filter(tool, {}, {})
        assert result == {}

    def test_already_has_personid(self, handler):
        tool = MagicMock()
        tool.method = "GET"
        result = handler._inject_person_filter(
            tool, {"PersonId": "existing"}, _user_context()
        )
        assert result == {"PersonId": "existing"}

    def test_capability_registry_native(self, handler):
        tool = MagicMock()
        tool.method = "GET"
        tool.operation_id = "get_MasterData"
        tool.parameters = {}

        with patch("services.engine.tool_handler.get_capability_registry") as mock_cr:
            cap = MagicMock()
            cap.should_inject_person_id.return_value = (True, "PersonId", "native")
            mock_cr.return_value = cap

            result = handler._inject_person_filter(tool, {}, _user_context())
            assert result["PersonId"] == "00000000-0000-0000-0000-000000000001"

    def test_capability_registry_filter(self, handler):
        tool = MagicMock()
        tool.method = "GET"
        tool.operation_id = "get_Vehicles"
        tool.parameters = {}

        with patch("services.engine.tool_handler.get_capability_registry") as mock_cr:
            cap = MagicMock()
            cap.should_inject_person_id.return_value = (True, "PersonId", "filter")
            mock_cr.return_value = cap

            result = handler._inject_person_filter(tool, {}, _user_context())
            assert "PersonId" in result.get("Filter", "")

    def test_capability_registry_filter_appends(self, handler):
        tool = MagicMock()
        tool.method = "GET"
        tool.operation_id = "get_Vehicles"
        tool.parameters = {}

        with patch("services.engine.tool_handler.get_capability_registry") as mock_cr:
            cap = MagicMock()
            cap.should_inject_person_id.return_value = (True, "PersonId", "filter")
            mock_cr.return_value = cap

            result = handler._inject_person_filter(
                tool, {"Filter": "Status(=)Active"}, _user_context()
            )
            assert "Status(=)Active" in result["Filter"]
            assert "PersonId" in result["Filter"]

    def test_schema_fallback(self, handler):
        tool = MagicMock()
        tool.method = "GET"
        tool.operation_id = "get_Something"

        param_def = MagicMock()
        param_def.context_key = "person_id"
        tool.parameters = {"PersonId": param_def}

        with patch("services.engine.tool_handler.get_capability_registry", return_value=None):
            result = handler._inject_person_filter(tool, {}, _user_context())
            assert result["PersonId"] == "00000000-0000-0000-0000-000000000001"


# ===========================================================================
# execute_tool_call
# ===========================================================================

class TestExecuteToolCall:
    @pytest.mark.asyncio
    async def test_tool_not_found(self, handler):
        handler.registry.get_tool.return_value = None
        conv = _conv()

        result = await handler.execute_tool_call(
            {"tool": "missing_tool", "parameters": {}},
            _user_context(), conv, "sender"
        )
        assert result["success"] is False
        assert "pronaden" in result["final_response"].lower()

    @pytest.mark.asyncio
    async def test_success(self, handler):
        tool = MagicMock()
        tool.method = "GET"
        tool.operation_id = "get_MasterData"
        tool.parameters = {}
        handler.registry.get_tool.return_value = tool

        exec_result = MagicMock()
        exec_result.success = True
        exec_result.data = {"LastMileage": 50000}
        exec_result.output_values = {"LastMileage": 50000}
        handler.executor.execute = AsyncMock(return_value=exec_result)

        conv = _conv()

        with patch("services.engine.tool_handler.get_capability_registry") as mock_cr, \
             patch("services.engine.tool_handler.get_tool_evaluator") as mock_eval:
            mock_cr.return_value = MagicMock()
            mock_cr.return_value.record_success = MagicMock()
            mock_cr.return_value.should_inject_person_id.return_value = (False, None, None)
            mock_eval.return_value = MagicMock(record_success=AsyncMock(), record_failure=AsyncMock())

            result = await handler.execute_tool_call(
                {"tool": "get_MasterData", "parameters": {}},
                _user_context(), conv, "sender"
            )
        assert result["success"] is True
        assert result["final_response"] == "Formatted result"

    @pytest.mark.asyncio
    async def test_failure_records_error(self, handler):
        tool = MagicMock()
        tool.method = "GET"
        tool.operation_id = "get_MasterData"
        tool.parameters = {}
        handler.registry.get_tool.return_value = tool

        exec_result = MagicMock()
        exec_result.success = False
        exec_result.error_message = "Not found"
        exec_result.error_code = "NOT_FOUND"
        exec_result.missing_params = None
        exec_result.http_status = 404
        handler.executor.execute = AsyncMock(return_value=exec_result)

        conv = _conv()

        with patch("services.engine.tool_handler.get_capability_registry") as mock_cr, \
             patch("services.engine.tool_handler.get_tool_evaluator") as mock_eval, \
             patch("services.engine.tool_handler.get_error_translator") as mock_et:
            mock_cr.return_value = MagicMock()
            mock_cr.return_value.should_inject_person_id.return_value = (False, None, None)
            mock_eval.return_value = MagicMock(record_success=AsyncMock(), record_failure=AsyncMock())
            mock_et.return_value.get_ai_feedback.return_value = "Check params"
            mock_et.return_value.get_user_message.return_value = "Greška 404"

            result = await handler.execute_tool_call(
                {"tool": "get_MasterData", "parameters": {}},
                _user_context(), conv, "sender"
            )
        assert result["success"] is False
        assert "404" in result["final_response"]
        handler.error_learning.record_error.assert_called_once()

    @pytest.mark.asyncio
    async def test_auto_chain_on_missing_param(self, handler):
        tool = MagicMock()
        tool.method = "GET"
        tool.operation_id = "get_MasterData"
        tool.parameters = {}
        handler.registry.get_tool.return_value = tool

        # First call fails with missing param
        fail_result = MagicMock()
        fail_result.success = False
        fail_result.error_code = "PARAMETER_VALIDATION_ERROR"
        fail_result.error_message = "Nedostaje parametri: VehicleId"
        fail_result.missing_params = ["VehicleId"]

        # Second call succeeds
        success_result = MagicMock()
        success_result.success = True
        success_result.data = {"LastMileage": 50000}
        success_result.output_values = {}

        handler.executor.execute = AsyncMock(side_effect=[fail_result, success_result])

        # Dependency resolver succeeds
        resolution = MagicMock()
        resolution.success = True
        resolution.resolved_value = "vehicle-uuid"
        resolution.feedback = None
        handler.dependency_resolver.resolve_dependency = AsyncMock(return_value=resolution)

        conv = _conv()

        with patch("services.engine.tool_handler.get_capability_registry") as mock_cr, \
             patch("services.engine.tool_handler.get_tool_evaluator") as mock_eval:
            mock_cr.return_value = MagicMock()
            mock_cr.return_value.record_success = MagicMock()
            mock_cr.return_value.should_inject_person_id.return_value = (False, None, None)
            mock_eval.return_value = MagicMock(record_success=AsyncMock(), record_failure=AsyncMock())

            result = await handler.execute_tool_call(
                {"tool": "get_MasterData", "parameters": {}},
                _user_context(), conv, "sender"
            )
        assert result["success"] is True
        handler.dependency_resolver.resolve_dependency.assert_called_once()

    @pytest.mark.asyncio
    async def test_entity_feedback_prepended(self, handler):
        tool = MagicMock()
        tool.method = "GET"
        tool.operation_id = "get_MasterData"
        tool.parameters = {}
        handler.registry.get_tool.return_value = tool

        exec_result = MagicMock()
        exec_result.success = True
        exec_result.data = {"LastMileage": 50000}
        exec_result.output_values = {}
        handler.executor.execute = AsyncMock(return_value=exec_result)

        conv = _conv()
        conv.context.tool_outputs = {
            "_entity_feedback": {
                "resolved_to": "Golf",
                "plate": "ZG-123"
            }
        }

        with patch("services.engine.tool_handler.get_capability_registry") as mock_cr, \
             patch("services.engine.tool_handler.get_tool_evaluator") as mock_eval:
            mock_cr.return_value = MagicMock()
            mock_cr.return_value.record_success = MagicMock()
            mock_cr.return_value.should_inject_person_id.return_value = (False, None, None)
            mock_eval.return_value = MagicMock(record_success=AsyncMock(), record_failure=AsyncMock())

            result = await handler.execute_tool_call(
                {"tool": "get_MasterData", "parameters": {}},
                _user_context(), conv, "sender"
            )
        assert "Golf" in result["final_response"]
        assert "ZG-123" in result["final_response"]

    @pytest.mark.asyncio
    async def test_correction_hint_in_failure(self, handler):
        tool = MagicMock()
        tool.method = "GET"
        tool.operation_id = "get_MasterData"
        tool.parameters = {}
        handler.registry.get_tool.return_value = tool

        exec_result = MagicMock()
        exec_result.success = False
        exec_result.error_message = "Bad request"
        exec_result.error_code = "VALIDATION"
        exec_result.missing_params = None
        exec_result.http_status = None
        handler.executor.execute = AsyncMock(return_value=exec_result)

        handler.error_learning.suggest_correction = AsyncMock(return_value={
            "action": {"hint": "Try different params"}
        })

        conv = _conv()

        with patch("services.engine.tool_handler.get_capability_registry") as mock_cr, \
             patch("services.engine.tool_handler.get_tool_evaluator") as mock_eval, \
             patch("services.engine.tool_handler.get_error_translator") as mock_et:
            mock_cr.return_value = MagicMock()
            mock_cr.return_value.should_inject_person_id.return_value = (False, None, None)
            mock_eval.return_value = MagicMock(record_success=AsyncMock(), record_failure=AsyncMock())
            mock_et.return_value.get_ai_feedback.return_value = "Check params"
            mock_et.return_value.get_user_message.return_value = "Greška"

            result = await handler.execute_tool_call(
                {"tool": "get_MasterData", "parameters": {}},
                _user_context(), conv, "sender"
            )
        assert "Sugestija" in result["ai_feedback"] or "Try" in result["ai_feedback"]
