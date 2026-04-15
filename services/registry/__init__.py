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
import threading
from typing import Dict, List, Any, Optional, Set

from services.tool_contracts import UnifiedToolDefinition, DependencyGraph

from .tool_store import ToolStore
from .swagger_parser import SwaggerParser
from .embedding_engine import EmbeddingEngine


def _read_json_file(path: str) -> Any:
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


logger = logging.getLogger(__name__)

# Module-level cache — kept as a module var so tests can reset it to None.
_documentation_cache: Optional[Dict[str, Any]] = None
_documentation_cache_lock = threading.Lock()

__all__ = ['ToolRegistry', 'ToolStore', 'SwaggerParser', 'EmbeddingEngine']


class ToolRegistry:
    """
    Facade that loads the pre-processed tool registry and merges documentation.

    Coordinates:
      - ToolStore: in-memory tool + dependency index
      - EmbeddingEngine / SwaggerParser: offline build helpers (reused by scripts/sync_tools.py)
      - tool_documentation.json: Croatian descriptions + origin guides
    """

    def __init__(self, redis_client=None) -> None:
        self.redis = redis_client

        self._store = ToolStore()
        self._parser = SwaggerParser()
        self._embedding = EmbeddingEngine()

        self._tool_documentation: Dict[str, Any] = {}

        self.is_ready = False
        self._load_lock = asyncio.Lock()

        self._load_documentation()

        logger.info("ToolRegistry initialized")

    def _load_documentation(self) -> None:
        global _documentation_cache

        if _documentation_cache is not None:
            self._tool_documentation = _documentation_cache
            return

        with _documentation_cache_lock:
            if _documentation_cache is not None:
                self._tool_documentation = _documentation_cache
                return

            base_path = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
            doc_path = os.path.join(base_path, "config", "tool_documentation.json")

            if not os.path.exists(doc_path):
                logger.info("No tool_documentation.json found - run generate_documentation.py first")
                return

            try:
                self._tool_documentation = _read_json_file(doc_path)
                _documentation_cache = self._tool_documentation
                logger.info(f"Loaded documentation for {len(self._tool_documentation)} tools")
            except Exception as e:
                logger.warning(f"Could not load tool documentation: {e}")
                self._tool_documentation = {}

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

                for tool_data in registry_data.get("tools", []):
                    tool = UnifiedToolDefinition(**tool_data)
                    self._store.add_tool(tool)

                for dep_data in registry_data.get("dependency_graph", []):
                    dep = DependencyGraph(**dep_data)
                    self._store.add_dependency(dep)

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

                await self._initialize_faiss()

                return True

            except Exception as e:
                logger.error(f"Initialization failed: {e}", exc_info=True)
                return False

    async def _initialize_faiss(self) -> None:
        try:
            from services.faiss_vector_store import initialize_faiss_store

            if not self._tool_documentation:
                logger.warning("FAISS: tool_documentation empty — skipping init")
                return

            faiss_store = await initialize_faiss_store(
                tool_documentation=self._tool_documentation,
                tool_registry_tools=self._store.tools
            )

            logger.info(f"FAISS initialized: {faiss_store.get_stats()['total_tools']} tools indexed")

        except ImportError as e:
            logger.warning(f"FAISS not available: {e}")
        except Exception as e:
            logger.warning(f"FAISS initialization failed: {e}")

    # ---
    # TOOL ACCESS
    # ---

    def get_tool(self, operation_id: str) -> Optional[UnifiedToolDefinition]:
        return self._store.get_tool(operation_id)

    def list_tools(self) -> List[str]:
        return self._store.list_tools()

    # ---
    # DOCUMENTATION ACCESS
    # ---

    def get_tool_documentation(self, operation_id: str) -> Optional[Dict[str, Any]]:
        return self._tool_documentation.get(operation_id)

    def get_parameter_origin_guide(self, operation_id: str) -> Dict[str, str]:
        doc = self._tool_documentation.get(operation_id, {})
        return doc.get("parameter_origin_guide", {})

    def get_tool_with_documentation(self, operation_id: str) -> Optional[Dict[str, Any]]:
        tool = self._store.get_tool(operation_id)
        if not tool:
            return None

        return {
            "tool": tool.to_openai_function(),
            "documentation": self._tool_documentation.get(operation_id, {})
        }

    def is_context_param(self, operation_id: str, param_name: str) -> bool:
        origin_guide = self.get_parameter_origin_guide(operation_id)
        origin = origin_guide.get(param_name, "")
        return "CONTEXT" in origin.upper()

    def is_user_param(self, operation_id: str, param_name: str) -> bool:
        origin_guide = self.get_parameter_origin_guide(operation_id)
        origin = origin_guide.get(param_name, "USER")
        return "USER" in origin.upper()

    # ---
    # HIDDEN DEFAULTS
    # ---

    _HIDDEN_DEFAULTS: Dict[str, Dict[str, Any]] = {
        "post_VehicleCalendar": {
            "EntryType": 0,
            "AssigneeType": 1,
        },
        "post_AddCase": {
            "EntryType": "WhatsApp",
        },
    }

    def get_hidden_defaults(self, operation_id: str) -> Dict[str, Any]:
        return self._HIDDEN_DEFAULTS.get(operation_id, {})

    def get_merged_params(
        self,
        operation_id: str,
        user_params: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Merge hidden defaults with user params (user wins, None filtered)."""
        merged = self.get_hidden_defaults(operation_id).copy()

        if user_params:
            clean_user_params = {k: v for k, v in user_params.items() if v is not None}
            merged.update(clean_user_params)

        return merged
