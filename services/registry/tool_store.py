"""
Tool Store - In-memory storage for tools and dependencies.

Single responsibility: Store and retrieve UnifiedToolDefinition objects.
"""

import logging
from typing import Dict, List, Optional, Set

from services.tool_contracts import UnifiedToolDefinition, DependencyGraph

logger = logging.getLogger(__name__)


class ToolStore:
    """
    In-memory storage for tools and their metadata.

    Responsibilities:
    - Store UnifiedToolDefinition objects by operation_id
    - Maintain categorization (retrieval vs mutation)
    - Store dependency graph
    - Provide lookup methods
    """

    def __init__(self) -> None:
        """Initialize empty store."""
        self.tools: Dict[str, UnifiedToolDefinition] = {}
        self.dependency_graph: Dict[str, DependencyGraph] = {}

        # Categorization sets
        self.retrieval_tools: Set[str] = set()
        self.mutation_tools: Set[str] = set()

        # Case-folded index → canonical operation_id, for O(1) case-insensitive
        # lookup without scanning 950+ tools on every router validation.
        self._lower_index: Dict[str, str] = {}

        logger.debug("ToolStore initialized")

    def add_tool(self, tool: UnifiedToolDefinition) -> None:
        """
        Add tool to store and categorize it.

        Args:
            tool: UnifiedToolDefinition to add
        """
        self.tools[tool.operation_id] = tool
        self._lower_index[tool.operation_id.lower()] = tool.operation_id

        if tool.is_retrieval:
            self.retrieval_tools.add(tool.operation_id)
        if tool.is_mutation:
            self.mutation_tools.add(tool.operation_id)

    def get_tool(self, operation_id: str) -> Optional[UnifiedToolDefinition]:
        """Get tool by operation ID (exact match)."""
        return self.tools.get(operation_id)

    def resolve_tool_id(self, operation_id: str) -> Optional[str]:
        """Return canonical operation_id for an exact or case-folded match."""
        if operation_id in self.tools:
            return operation_id
        return self._lower_index.get(operation_id.lower())

    def has_tool(self, operation_id: str) -> bool:
        """Check if tool exists."""
        return operation_id in self.tools

    def list_tools(self) -> List[str]:
        """List all tool operation IDs."""
        return list(self.tools.keys())

    def get_all_tools(self) -> Dict[str, UnifiedToolDefinition]:
        """Get all tools."""
        return self.tools

    def count(self) -> int:
        """Get total number of tools."""
        return len(self.tools)

    def add_dependency(self, dep: DependencyGraph) -> None:
        """Add dependency graph entry."""
        self.dependency_graph[dep.tool_id] = dep

    def get_dependency(self, tool_id: str) -> Optional[DependencyGraph]:
        """Get dependency graph for a tool."""
        return self.dependency_graph.get(tool_id)

    def clear(self) -> None:
        """Clear all stored data."""
        self.tools.clear()
        self.dependency_graph.clear()
        self.retrieval_tools.clear()
        self.mutation_tools.clear()
        self._lower_index.clear()
        logger.debug("ToolStore cleared")

    def get_stats(self) -> Dict[str, int]:
        """Get store statistics."""
        return {
            "total_tools": len(self.tools),
            "retrieval_tools": len(self.retrieval_tools),
            "mutation_tools": len(self.mutation_tools),
            "dependencies": len(self.dependency_graph)
        }
