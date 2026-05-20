"""
API Capabilities Discovery

DYNAMIC API CAPABILITY DETECTION - No hardcoding!

This module discovers API capabilities from Swagger/OpenAPI metadata,
eliminating the need for hardcoded lists of which endpoints support
which parameters.

Key Features:
1. Auto-detect PersonId support from tool parameters
2. Auto-detect Filter syntax from tool descriptions
3. Learn API patterns from successful/failed calls
4. Provide intelligent suggestions for parameter injection

This replaces hardcoded lists like:
- exclusions = ["get_vehicles", "get_availablevehicles"]
- supported_tools = ["get_masterdata", "get_vehiclecalendar"]

With DYNAMIC detection:
- If tool has "personId" parameter → supports PersonId injection
- If tool returns 500 with "Unknown filter field" → doesn't support that filter
"""

import asyncio
import logging
import json
from typing import Dict, Optional, Any, Tuple
from pathlib import Path
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)

# Anchored to repo root (not Path.cwd()) so the path is stable regardless
# of where the worker is launched from. services/api_capabilities.py →
# parents[1] = repo root. The .cache/ directory must be writable; docker
# compose mounts cache_data volume here, K8s must do the same.
CAPABILITIES_CACHE_FILE = Path(__file__).resolve().parents[1] / ".cache" / "api_capabilities.json"

class ParameterSupport(Enum):
    """How a tool supports a parameter."""
    NATIVE = "native"           # Has direct parameter (e.g., personId in query)
    FILTER = "filter"           # Supports via Filter parameter (e.g., Filter=PersonId(=)xxx)
    NOT_SUPPORTED = "not_supported"  # Doesn't support this parameter
    UNKNOWN = "unknown"         # Not yet tested

@dataclass
class ToolCapability:
    """Discovered capabilities for a single tool."""
    operation_id: str
    method: str
    path: str

    # Parameter support (discovered from Swagger + runtime learning)
    supports_person_id: ParameterSupport = ParameterSupport.UNKNOWN
    supports_vehicle_id: ParameterSupport = ParameterSupport.UNKNOWN
    supports_tenant_id: ParameterSupport = ParameterSupport.UNKNOWN
    supports_filter: bool = False

    # Output analysis
    returns_list: bool = False
    returns_user_specific_data: bool = False

    # Runtime learning
    successful_calls: int = 0
    failed_calls: int = 0
    last_error: Optional[str] = None
    learned_patterns: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return {
            "operation_id": self.operation_id,
            "method": self.method,
            "path": self.path,
            "supports_person_id": self.supports_person_id.value,
            "supports_vehicle_id": self.supports_vehicle_id.value,
            "supports_tenant_id": self.supports_tenant_id.value,
            "supports_filter": self.supports_filter,
            "returns_list": self.returns_list,
            "returns_user_specific_data": self.returns_user_specific_data,
            "successful_calls": self.successful_calls,
            "failed_calls": self.failed_calls,
            "last_error": self.last_error,
            "learned_patterns": self.learned_patterns
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "ToolCapability":
        return cls(
            operation_id=data["operation_id"],
            method=data["method"],
            path=data["path"],
            supports_person_id=ParameterSupport(data.get("supports_person_id", "unknown")),
            supports_vehicle_id=ParameterSupport(data.get("supports_vehicle_id", "unknown")),
            supports_tenant_id=ParameterSupport(data.get("supports_tenant_id", "unknown")),
            supports_filter=data.get("supports_filter", False),
            returns_list=data.get("returns_list", False),
            returns_user_specific_data=data.get("returns_user_specific_data", False),
            successful_calls=data.get("successful_calls", 0),
            failed_calls=data.get("failed_calls", 0),
            last_error=data.get("last_error"),
            learned_patterns=data.get("learned_patterns", {})
        )

class APICapabilityRegistry:
    """
    Dynamic API capability discovery and learning.

    Replaces hardcoded logic with intelligent detection:
    1. Initial discovery from Swagger metadata (delegates to ToolRegistry)
    2. Runtime learning from API responses
    3. Persistent caching for fast startup

    NOTE: Parameter classification is delegated to ToolRegistry which uses
    schema-based classification (config/context_param_schemas.json).
    This class only analyzes tool-level capabilities.
    """

    def __init__(self, tool_registry=None):
        """
        Initialize capability registry.

        Args:
            tool_registry: ToolRegistry instance for accessing tool definitions
        """
        self.tool_registry = tool_registry
        self.capabilities: Dict[str, ToolCapability] = {}
        self._loaded = False

    async def initialize(self, tool_registry=None) -> bool:
        """
        Initialize capabilities from tool registry.

        This is the SMART replacement for hardcoded lists:
        Instead of maintaining lists of which tools support PersonId,
        we DISCOVER it from the Swagger metadata.

        Returns:
            True if successful
        """
        if tool_registry:
            self.tool_registry = tool_registry

        if not self.tool_registry:
            logger.error("No tool registry provided")
            return False

        # Try to load from cache first
        if await self._load_cache():
            self._loaded = True
            logger.info(f"Loaded {len(self.capabilities)} tool capabilities from cache")
            return True

        # Discover capabilities from Swagger metadata
        await self._discover_from_registry()

        # Save to cache
        await self._save_cache()

        self._loaded = True
        logger.info(f"Discovered capabilities for {len(self.capabilities)} tools")

        return True

    async def _discover_from_registry(self) -> None:
        """
        Discover capabilities from tool registry.

        This delegates parameter classification to ToolRegistry (schema-based)
        and analyzes tool-level capabilities:
        1. Parameters (does it have person_id/vehicle_id? Filter?)
        2. Output schema (does it return a list? user-specific data?)
        3. Path patterns (contains /person/ or /vehicle/?)
        """
        for op_id, tool in self.tool_registry.tools.items():
            capability = ToolCapability(
                operation_id=op_id,
                method=tool.method,
                path=tool.path
            )

            # NEW: Use schema-based classification from ToolRegistry
            # Check which parameters this tool has (already classified by ToolRegistry)
            has_person_id = any(
                param.context_key == "person_id"
                for param in tool.parameters.values()
            )
            has_vehicle_id = any(
                param.context_key == "vehicle_id"
                for param in tool.parameters.values()
            )
            has_filter = "filter" in (p.lower() for p in tool.parameters.keys())

            # Determine support level
            if has_person_id:
                capability.supports_person_id = ParameterSupport.NATIVE
                logger.debug(f"{op_id}: Native PersonId support detected")
            elif has_filter:
                capability.supports_person_id = ParameterSupport.UNKNOWN
                capability.supports_filter = True
            else:
                capability.supports_person_id = ParameterSupport.NOT_SUPPORTED

            if has_vehicle_id:
                capability.supports_vehicle_id = ParameterSupport.NATIVE
            elif has_filter:
                capability.supports_vehicle_id = ParameterSupport.UNKNOWN
            else:
                capability.supports_vehicle_id = ParameterSupport.NOT_SUPPORTED

            # Check if returns list (from output_keys or path pattern)
            if tool.output_keys:
                capability.returns_list = len(tool.output_keys) > 3

            # Check if returns user-specific data (heuristic)
            path_lower = tool.path.lower()
            user_specific_paths = ["/my", "/user", "/person", "/driver"]
            capability.returns_user_specific_data = any(
                x in path_lower for x in user_specific_paths
            )

            # Check output keys for user-specific indicators
            if tool.output_keys:
                output_lower = " ".join(tool.output_keys).lower()
                user_specific_keywords = ["assigned", "driver", "owner"]
                if any(x in output_lower for x in user_specific_keywords):
                    capability.returns_user_specific_data = True

            self.capabilities[op_id] = capability

    def get_capability(self, operation_id: str) -> Optional[ToolCapability]:
        """Get capability for a tool."""
        return self.capabilities.get(operation_id)

    def should_inject_person_id(
        self,
        operation_id: str,
        person_id: str
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Determine if and how to inject PersonId for a tool.

        This REPLACES the hardcoded logic in message_engine._inject_person_filter()

        Args:
            operation_id: Tool operation ID
            person_id: PersonId to inject

        Returns:
            Tuple of (should_inject, parameter_name, injection_method)
            - should_inject: True if we should inject PersonId
            - parameter_name: Name of parameter to use (e.g., "personId", "Filter")
            - injection_method: "native" or "filter"
        """
        cap = self.capabilities.get(operation_id)

        if not cap:
            # Unknown tool - don't inject to be safe
            return (False, None, None)

        # Check native support first
        if cap.supports_person_id == ParameterSupport.NATIVE:
            # Find the actual parameter name (already classified by ToolRegistry)
            tool = self.tool_registry.get_tool(operation_id)
            if tool:
                for param_name, param_def in tool.parameters.items():
                    if param_def.context_key == "person_id":
                        return (True, param_name, "native")

        # Check filter support
        if cap.supports_person_id == ParameterSupport.FILTER and cap.supports_filter:
            return (True, "Filter", "filter")

        # Not supported
        if cap.supports_person_id == ParameterSupport.NOT_SUPPORTED:
            return (False, None, None)

        # Unknown - we haven't learned yet, so don't inject
        # (will learn from runtime errors)
        return (False, None, None)

    def record_success(self, operation_id: str, params_used: Dict[str, Any]) -> None:
        """
        Record successful API call - learn what works.

        Args:
            operation_id: Tool that succeeded
            params_used: Parameters that were used
        """
        cap = self.capabilities.get(operation_id)
        if not cap:
            return

        cap.successful_calls += 1

        # Learn from success: check if any parameter is classified as person_id
        tool = self.tool_registry.get_tool(operation_id) if self.tool_registry else None
        if tool:
            for param_name in params_used.keys():
                param_def = tool.parameters.get(param_name)
                if param_def and param_def.context_key == "person_id":
                    if cap.supports_person_id == ParameterSupport.UNKNOWN:
                        cap.supports_person_id = ParameterSupport.NATIVE
                        logger.info(f"Learned: {operation_id} supports native PersonId")
                        break

        # Check if Filter with PersonId was used
        params_lower = {k.lower(): k for k in params_used.keys()}
        if "filter" in params_lower:
            filter_value = params_used.get(params_lower["filter"], "")
            if "personid" in filter_value.lower():
                if cap.supports_person_id == ParameterSupport.UNKNOWN:
                    cap.supports_person_id = ParameterSupport.FILTER
                    logger.info(f"Learned: {operation_id} supports PersonId via Filter")

    def record_failure(
        self,
        operation_id: str,
        error_message: str,
        params_used: Dict[str, Any]
    ) -> None:
        """
        Record failed API call - learn what doesn't work.

        This is KEY for learning: if we get "Unknown filter field: PersonId",
        we learn that this endpoint doesn't support PersonId filter.

        Args:
            operation_id: Tool that failed
            error_message: Error message from API
            params_used: Parameters that were used
        """
        cap = self.capabilities.get(operation_id)
        if not cap:
            return

        cap.failed_calls += 1
        cap.last_error = error_message

        error_lower = error_message.lower()

        # Learn from specific error patterns
        if "unknown filter field" in error_lower:
            # Extract which field was unknown
            if "personid" in error_lower:
                cap.supports_person_id = ParameterSupport.NOT_SUPPORTED
                logger.info(f"Learned: {operation_id} does NOT support PersonId filter")
            elif "vehicleid" in error_lower:
                cap.supports_vehicle_id = ParameterSupport.NOT_SUPPORTED
                logger.info(f"Learned: {operation_id} does NOT support VehicleId filter")

        # Learn from 400 errors with filter
        if "filter" in params_used and "400" in error_lower:
            # Filter syntax might be wrong for this endpoint
            cap.learned_patterns["filter_syntax_error"] = True

    async def _load_cache(self) -> bool:
        """Load capabilities from cache file."""
        if not CAPABILITIES_CACHE_FILE.exists():
            return False

        def _read():
            with open(CAPABILITIES_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)

        try:
            data = await asyncio.to_thread(_read)

            for cap_dict in data.get("capabilities", []):
                cap = ToolCapability.from_dict(cap_dict)
                self.capabilities[cap.operation_id] = cap

            return True
        except Exception as e:
            logger.warning(f"Failed to load capabilities cache: {e}")
            return False

    async def _save_cache(self) -> None:
        """Save capabilities to cache file."""
        try:
            CAPABILITIES_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)

            data = {
                "version": "1.0",
                "capabilities": [cap.to_dict() for cap in self.capabilities.values()]
            }

            def _write():
                with open(CAPABILITIES_CACHE_FILE, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)

            await asyncio.to_thread(_write)

            logger.info(f"Saved {len(self.capabilities)} capabilities to cache")
        except Exception as e:
            logger.error(f"Failed to save capabilities cache: {e}")

    async def save(self) -> None:
        """Public method to persist learned capabilities."""
        await self._save_cache()

# Global instance (initialized by worker)
_capability_registry: Optional[APICapabilityRegistry] = None

def get_capability_registry() -> Optional[APICapabilityRegistry]:
    """Get global capability registry instance."""
    return _capability_registry

async def initialize_capability_registry(tool_registry) -> APICapabilityRegistry:
    """
    Initialize global capability registry.

    Call this during worker startup, after ToolRegistry is loaded.
    """
    global _capability_registry
    _capability_registry = APICapabilityRegistry(tool_registry)
    await _capability_registry.initialize()
    return _capability_registry
