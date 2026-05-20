"""
Tool Registry - Public API facade.

Loads pre-processed tool registry from config/processed_tool_registry.json
(rebuilt offline via scripts/sync_tools.py) and exposes tool access +
documentation merge to the rest of the system.

Semantic search itself lives in services/faiss_vector_store.py — this registry
no longer owns the search path.
"""

import asyncio
import json
import logging
import os
from typing import Any, Dict, List, Optional, Set

from services.tool_contracts import UnifiedToolDefinition, DependencyGraph

from .tool_store import ToolStore
from .swagger_parser import SwaggerParser


def _read_json_file(path: str) -> Any:
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


logger = logging.getLogger(__name__)

__all__ = ['ToolRegistry', 'ToolStore', 'SwaggerParser']


class ToolRegistry:
    """
    Facade that loads the pre-processed tool registry and merges documentation.

    Coordinates:
      - ToolStore: in-memory tool + dependency index
      - SwaggerParser: offline build helper (reused by scripts/sync_tools.py)
      - tool_documentation.json: Croatian descriptions + origin guides
    """

    def __init__(self, redis_client=None) -> None:
        self.redis = redis_client

        self._store = ToolStore()
        self._parser = SwaggerParser()

        self.is_ready = False
        self._load_lock = asyncio.Lock()

        logger.info("ToolRegistry initialized")

    # ---
    # BACKWARD COMPATIBILITY PROPERTIES
    # ---

    @property
    def tools(self) -> Dict[str, UnifiedToolDefinition]:
        return self._store.tools

    @property
    def dependency_graph(self) -> Dict[str, DependencyGraph]:
        return self._store.dependency_graph

    @property
    def retrieval_tools(self) -> Set[str]:
        return self._store.retrieval_tools

    @property
    def mutation_tools(self) -> Set[str]:
        return self._store.mutation_tools

    @property
    def CONTEXT_PARAM_FALLBACK(self) -> Dict[str, str]:
        return self._parser.context_param_fallback

    # ---
    # INITIALIZATION
    # ---

    async def initialize(self, swagger_sources: Optional[List[str]] = None) -> bool:
        """
        Load tools + dependency graph from config/processed_tool_registry.json.

        The pre-processed file is rebuilt offline by scripts/sync_tools.py;
        at runtime we only load it. If missing, initialization fails — there is
        no live-Swagger fallback in the runtime path.
        """
        async with self._load_lock:
            try:
                base_path = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
                registry_path = os.path.join(base_path, "config", "processed_tool_registry.json")

                if not os.path.exists(registry_path):
                    logger.error(
                        f"Pre-processed registry not found at {registry_path}. "
                        f"Run scripts/sync_tools.py to build it."
                    )
                    return False

                logger.info(f"Loading pre-processed registry from {registry_path}")
                registry_data = await asyncio.to_thread(_read_json_file, registry_path)

                skipped = 0
                for tool_data in registry_data.get("tools", []):
                    try:
                        self._store.add_tool(UnifiedToolDefinition(**tool_data))
                    except Exception as e:  # noqa: BLE001 — one bad tool must not kill the whole registry
                        skipped += 1
                        op_id = tool_data.get("operation_id", "?") if isinstance(tool_data, dict) else "?"
                        logger.warning("skipping malformed tool %s: %s", op_id, e)

                for dep_data in registry_data.get("dependency_graph", []):
                    try:
                        self._store.add_dependency(DependencyGraph(**dep_data))
                    except Exception as e:  # noqa: BLE001
                        logger.warning("skipping malformed dependency: %s", e)

                if skipped:
                    logger.warning(
                        "registry loaded with %d malformed tool(s) skipped", skipped
                    )

                logger.info(
                    f"Loaded {self._store.count()} tools and "
                    f"{len(self._store.dependency_graph)} dependencies."
                )

                self.is_ready = True
                logger.info(
                    f"Initialized {self._store.count()} tools "
                    f"({len(self._store.retrieval_tools)} retrieval, "
                    f"{len(self._store.mutation_tools)} mutation)"
                )

                return True

            except Exception as e:
                logger.error(f"Initialization failed: {e}", exc_info=True)
                return False

    # ---
    # TOOL ACCESS
    # ---

    def get_tool(self, operation_id: str) -> Optional[UnifiedToolDefinition]:
        return self._store.get_tool(operation_id)

    def resolve_tool_id(self, operation_id: str) -> Optional[str]:
        """O(1) case-insensitive operation_id resolution."""
        return self._store.resolve_tool_id(operation_id)

    def list_tools(self) -> List[str]:
        return self._store.list_tools()

    # NOTE: tool_documentation.json was deleted in Phase 0 cleanup. The
    # legacy `get_tool_documentation` / `get_parameter_origin_guide` /
    # `is_context_param` / `is_user_param` / `get_tool_with_documentation`
    # methods were defensive no-ops and have been removed.

    # NOTE — `_HIDDEN_DEFAULTS` removed in 11.0.4.
    # Tool-specific constants violate the agnostic-registry doctrine. The
    # values previously baked here (post_VehicleCalendar EntryType=0,
    # AssigneeType=1, post_AddCase EntryType="WhatsApp") are now applied
    # explicitly by the matching flow handler with named enums from
    # services/booking_contracts.py — protocol constants from the API spec,
    # not hidden registry-level magic.
