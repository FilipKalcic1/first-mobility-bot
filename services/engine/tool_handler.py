"""
Tool Handler - Tool execution and dependency chaining.

Single responsibility: Execute tools with automatic dependency resolution.
"""

import json
import logging
import re
from typing import Dict, Any, Optional, List

from services.api_capabilities import get_capability_registry
from services.error_translator import get_error_translator
from services.tool_evaluator import get_tool_evaluator
from services.context import UserContextManager
from services.tool_contracts import ToolExecutionContext

logger = logging.getLogger(__name__)


_MUTATION_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class ToolHandler:
    """
    Handles tool execution with automatic dependency chaining.

    Responsibilities:
    - Execute tool calls
    - Resolve missing parameters automatically
    - Inject PersonId filters
    - Handle chaining for dependent tools
    """

    MAX_CHAIN_DEPTH = 3

    def __init__(
        self,
        registry,
        executor,
        dependency_resolver,
        error_learning,
        formatter
    ):
        """Initialize tool handler."""
        self.registry = registry
        self.executor = executor
        self.dependency_resolver = dependency_resolver
        self.error_learning = error_learning
        self.formatter = formatter
        self._context_param_examples = self._load_context_param_examples()

    def _load_context_param_examples(self) -> List[str]:
        """Load parameter name variations from registry config."""
        if not self.registry or not hasattr(self.registry, 'CONTEXT_PARAM_FALLBACK'):
            return ['VehicleId', 'PersonId', 'BookingId', 'LocationId', 'DriverId']
        examples = list(self.registry.CONTEXT_PARAM_FALLBACK.keys())
        if not examples:
            examples = ['VehicleId', 'PersonId', 'BookingId', 'LocationId', 'DriverId']
        return examples

    async def execute_tool_call(
        self,
        result: Dict[str, Any],
        user_context: Dict[str, Any],
        conv_manager,
        sender: str,
        chain_depth: int = 0,
        user_query: Optional[str] = None  # For intent-aware formatting
    ) -> Dict[str, Any]:
        """
        Execute tool call with automatic dependency chaining.

        Args:
            result: Tool call from AI (tool name, parameters, tool_call_id)
            user_context: User context
            conv_manager: Conversation manager
            sender: User phone
            chain_depth: Current chain depth
            user_query: Original user query for intent-aware response formatting

        Returns:
            Dict with success, data, final_response, needs_input, ai_feedback
        """
        tool_name = result["tool"]
        parameters = result["parameters"]

        logger.info(
            f"Executing: {tool_name} with params: {list(parameters.keys())} "
            f"(chain_depth={chain_depth})"
        )

        tool = self.registry.get_tool(tool_name)
        if not tool:
            logger.error(f"Tool not found: {tool_name}")
            return {
                "success": False,
                "data": {"error": f"Tool {tool_name} not found"},
                "ai_feedback": f"Tool '{tool_name}' does not exist.",
                "final_response": f"Tehnicki problem - alat '{tool_name}' nije pronaden."
            }

        # Create execution context
        execution_context = ToolExecutionContext.from_conv_manager(user_context, conv_manager)

        # Inject PersonId filter for GET methods
        parameters = self._inject_person_filter(tool, parameters, user_context)

        # Execute
        exec_result = await self.executor.execute(tool, parameters, execution_context)

        # Handle missing parameter - try auto-chaining
        if (
            not exec_result.success and
            exec_result.error_code == "PARAMETER_VALIDATION_ERROR"
        ):
            missing_param = None
            if exec_result.missing_params:
                missing_param = exec_result.missing_params[0]
            else:
                missing_param = self._extract_missing_param_from_error(
                    exec_result.error_message
                )

            if missing_param and chain_depth < self.MAX_CHAIN_DEPTH:
                logger.info(f"Attempting auto-chain for: {missing_param}")

                user_value = self._find_resolvable_value(parameters, missing_param)

                resolution = await self.dependency_resolver.resolve_dependency(
                    missing_param=missing_param,
                    user_value=user_value,
                    user_context=user_context,
                    executor=self.executor
                )

                if resolution.success:
                    parameters[missing_param] = resolution.resolved_value
                    logger.info(
                        f"Auto-resolved {missing_param} = {resolution.resolved_value}"
                    )

                    if hasattr(conv_manager.context, 'tool_outputs'):
                        conv_manager.context.tool_outputs[missing_param] = (
                            resolution.resolved_value
                        )
                        if resolution.feedback:
                            conv_manager.context.tool_outputs["_entity_feedback"] = (
                                resolution.feedback
                            )
                    await conv_manager.save()

                    return await self.execute_tool_call(
                        result={
                            "tool": tool_name,
                            "parameters": parameters,
                            "tool_call_id": result.get("tool_call_id")
                        },
                        user_context=user_context,
                        conv_manager=conv_manager,
                        sender=sender,
                        chain_depth=chain_depth + 1,
                        user_query=user_query  # Pass through for intent-aware formatting
                    )

        # Handle result
        if exec_result.success:
            return await self._handle_success(
                exec_result, tool, tool_name, parameters, conv_manager, user_query
            )
        else:
            return await self._handle_failure(
                exec_result, tool_name, parameters, user_context, chain_depth
            )

    async def _handle_success(
        self,
        exec_result,
        tool,
        tool_name: str,
        parameters: Dict[str, Any],
        conv_manager,
        user_query: Optional[str] = None  # For intent-aware formatting
    ) -> Dict[str, Any]:
        """Handle successful tool execution."""
        result_dict = {
            "success": True,
            "data": exec_result.data,
            "operation": tool_name
        }

        # DEBUG: Log raw API response for debugging data extraction issues
        try:
            raw_json = json.dumps(exec_result.data, default=str, ensure_ascii=False)
            logger.debug(f"RAW API RESPONSE [{tool_name}]: {raw_json[:1500]}")
        except Exception as e:
            logger.warning(f"Could not serialize API response: {e}")

        # Pass user_query for intent-aware formatting
        response = self.formatter.format_result(result_dict, tool, user_query=user_query)

        # Prepend entity feedback if available
        entity_feedback = conv_manager.context.tool_outputs.get("_entity_feedback")
        if entity_feedback:
            vehicle_name = entity_feedback.get("resolved_to")
            plate = entity_feedback.get("plate")
            if vehicle_name and plate:
                confirmation = f"Razumijem: {vehicle_name} ({plate})\n\n"
                response = confirmation + response
            conv_manager.context.tool_outputs.pop("_entity_feedback", None)

        # Store output values for chaining
        if exec_result.output_values:
            conv_manager.context.tool_outputs.update(exec_result.output_values)
            await conv_manager.save()

        # Record success for learning
        capability_registry = get_capability_registry()
        if capability_registry:
            capability_registry.record_success(tool_name, parameters)

        evaluator = get_tool_evaluator()
        await evaluator.record_success(tool_name, params_used=parameters)

        return {
            "success": True,
            "data": exec_result.data,
            "final_response": response
        }

    async def _handle_failure(
        self,
        exec_result,
        tool_name: str,
        parameters: Dict[str, Any],
        user_context: Dict[str, Any],
        chain_depth: int
    ) -> Dict[str, Any]:
        """Handle failed tool execution."""
        error = exec_result.error_message or "Nepoznata greska"
        error_code = exec_result.error_code or "UNKNOWN"

        # Record failure for learning
        capability_registry = get_capability_registry()
        if capability_registry:
            capability_registry.record_failure(tool_name, error, parameters)

        evaluator = get_tool_evaluator()
        await evaluator.record_failure(
            tool_name, error,
            error_type=error_code,
            params_used=parameters
        )

        # Use UserContextManager for validated access
        ctx = UserContextManager(user_context)
        await self.error_learning.record_error(
            error_code=error_code,
            operation_id=tool_name,
            error_message=error,
            context={
                "parameters": list(parameters.keys()),
                "user_id": ctx.person_id,
                "chain_depth": chain_depth
            },
            was_corrected=False
        )

        correction = await self.error_learning.suggest_correction(
            error_code=error_code,
            operation_id=tool_name,
            error_message=error,
            current_params=parameters
        )

        translator = get_error_translator()
        ai_feedback = translator.get_ai_feedback(error, tool_name)

        if correction:
            correction_hint = correction.get("action", {}).get("hint", "")
            if correction_hint:
                ai_feedback = f"{ai_feedback}. Sugestija: {correction_hint}"

        http_status = exec_result.http_status
        if http_status and http_status >= 400:
            error_with_status = f"HTTP {http_status}: {error}"
        else:
            error_with_status = error

        human_error = translator.get_user_message(error_with_status, tool_name)

        return {
            "success": False,
            "data": {"error": error},
            "error": error,
            "ai_feedback": ai_feedback,
            "final_response": human_error
        }

    def _inject_person_filter(
        self,
        tool,
        parameters: Dict[str, Any],
        user_context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Inject PersonId filter for GET methods using capability registry."""
        if tool.method != "GET":
            return parameters

        # Use UserContextManager for validated access
        person_id = UserContextManager(user_context).person_id
        if not person_id:
            return parameters

        param_keys_lower = [k.lower() for k in parameters.keys()]
        if "personid" in param_keys_lower or "driverid" in param_keys_lower:
            return parameters

        capability_registry = get_capability_registry()

        if capability_registry:
            should_inject, param_name, method = capability_registry.should_inject_person_id(
                tool.operation_id, person_id
            )

            if should_inject and param_name:
                modified_params = parameters.copy()

                if method == "native":
                    modified_params[param_name] = person_id
                    logger.info(f"INJECT: {param_name}={person_id}")
                elif method == "filter":
                    existing_filter = modified_params.get("Filter", "")
                    if existing_filter:
                        modified_params["Filter"] = (
                            f"{existing_filter};PersonId(=){person_id}"
                        )
                    else:
                        modified_params["Filter"] = f"PersonId(=){person_id}"
                    logger.info(f"FILTER: PersonId={person_id}")

                return modified_params

        # Fallback: schema-based detection
        for param_name, param_def in tool.parameters.items():
            if param_def.context_key == "person_id":
                modified_params = parameters.copy()
                modified_params[param_name] = person_id
                logger.info(f"SCHEMA INJECT: {param_name}={person_id}")
                return modified_params

        return parameters

    def _extract_missing_param_from_error(self, error_message: str) -> Optional[str]:
        """Extract missing parameter name from validation error."""
        if not error_message:
            return None

        match = re.search(r'parametri?:\s*(\w+)', error_message, re.IGNORECASE)
        if match:
            return match.group(1)

        match = re.search(r'unesite\s+(\w+(?:\s+\w+)?)\s*:', error_message, re.IGNORECASE)
        if match:
            parts = match.group(1).split()
            return ''.join(word.capitalize() for word in parts)

        for pattern in self._context_param_examples:
            if pattern.lower() in error_message.lower():
                return pattern

        if 'vozilo' in error_message.lower():
            return 'VehicleId'
        if 'osoba' in error_message.lower() or 'vozac' in error_message.lower():
            return 'PersonId'

        return None

    def _find_resolvable_value(
        self,
        parameters: Dict[str, Any],
        missing_param: str
    ) -> Optional[str]:
        """Find user-provided value that could resolve missing param."""
        missing_lower = missing_param.lower()

        if 'vehicle' in missing_lower:
            for key, value in parameters.items():
                if not value or not isinstance(value, str):
                    continue
                key_lower = key.lower()
                if any(x in key_lower for x in ['plate', 'registration', 'name', 'vehicle']):
                    return value
                if self.dependency_resolver.detect_value_type(value):
                    return value

        if 'person' in missing_lower or 'driver' in missing_lower:
            for key, value in parameters.items():
                if not value or not isinstance(value, str):
                    continue
                key_lower = key.lower()
                if any(x in key_lower for x in ['email', 'phone', 'name', 'person', 'driver']):
                    return value

        return None

    def requires_confirmation(self, tool_name: str, method: Optional[str] = None) -> bool:
        """
        Check if tool requires user confirmation.

        NEW: ALL mutation operations (POST/PUT/PATCH/DELETE) require confirmation.
        This ensures user explicitly approves any data-changing operation.
        """
        # Method 1: Check HTTP method (most reliable)
        if method and method.upper() in _MUTATION_METHODS:
            return True

        # Method 2: Try to get method from registry
        if self.registry:
            tool = self.registry.get_tool(tool_name)
            if tool and hasattr(tool, 'method'):
                if tool.method.upper() in _MUTATION_METHODS:
                    return True

        # Method 3: Infer from tool name prefix (fallback)
        tool_lower = tool_name.lower()
        if tool_lower.startswith(('post_', 'put_', 'patch_', 'delete_')):
            return True

        # Method 4: Pattern matching for known mutation tools (legacy support)
        confirm_patterns = ["calendar", "booking", "case", "addmileage", "addcase"]
        return any(p in tool_lower for p in confirm_patterns)
