"""
Tool Family Index — direct entity+queryType → tool_id lookup.

Bypasses FAISS entirely when both entity and query type are known.
Parses tool_id format: {method}_{Entity}_{suffix} to build family groups.

Example:
    "grupiraj troškove"
    → entity="expenses", query_type=AGGREGATION
    → family["Expenses"]["groupby"] = "get_Expenses_GroupBy"
    → Direct hit, 0 FAISS calls, 100% precision.
"""
import logging
from typing import Dict, Optional, List
import threading

from services.types.query_type import QueryType

logger = logging.getLogger(__name__)

# Map QueryType → variant key in the family dict
_QUERY_TYPE_TO_VARIANT = {
    QueryType.LIST: "list",
    QueryType.SINGLE_ENTITY: "id",
    QueryType.AGGREGATION: "groupby",
    QueryType.DOCUMENTS: "documents",
    QueryType.METADATA: "metadata",
    QueryType.TREE: "tree",
    QueryType.DELETE_CRITERIA: "deletebycriteria",
    QueryType.BULK_UPDATE: "multipatch",
    QueryType.PROJECTION: "projectto",
}

# Known suffixes and their variant keys (order matters: longer first)
_SUFFIX_MAP = [
    ("_deletebycriteria", "deletebycriteria"),
    ("_multipatch", "multipatch"),
    ("_setasdefault", "setasdefault"),
    ("_projectto", "projectto"),
    ("_documents", "documents"),
    ("_metadata", "metadata"),
    ("_groupby", "groupby"),
    ("_filter", "filter"),
    ("_thumb", "thumb"),
    ("_tree", "tree"),
    ("_agg", "agg"),
    ("_id", "id"),
]


class ToolFamilyIndex:
    """
    Index of tool families grouped by entity.

    Each family contains all variants (list, id, groupby, agg, etc.)
    for that entity across all HTTP methods.
    """

    def __init__(self):
        # {"expenses": {"list": "get_Expenses", "id": "get_Expenses_id", "groupby": "get_Expenses_GroupBy", ...}}
        self.families: Dict[str, Dict[str, str]] = {}
        # {"expenses": {"list": "get_Expenses", "delete_id": "delete_Expenses_id", ...}}
        self.families_with_method: Dict[str, Dict[str, str]] = {}

    def build(self, tool_ids: List[str]):
        """Build family index from tool IDs.

        Uses atomic swap: builds into new dicts, then replaces references.
        This prevents resolve() from seeing empty dicts during rebuild.
        """
        new_families: Dict[str, Dict[str, str]] = {}
        new_families_with_method: Dict[str, Dict[str, str]] = {}

        for tool_id in tool_ids:
            entity, variant, method = self._parse_tool_id(tool_id)
            if not entity:
                continue

            entity_lower = entity.lower()

            # Simple family (no method prefix — used for GET queries)
            # Prefer GET over other methods so get_Persons isn't overwritten by post_Persons
            existing = new_families.get(entity_lower, {}).get(variant)
            if existing is None or method == "get":
                new_families.setdefault(entity_lower, {})[variant] = tool_id

            # Method-qualified family (used for action-specific queries)
            method_variant = f"{method}_{variant}" if method != "get" else variant
            new_families_with_method.setdefault(entity_lower, {})[method_variant] = tool_id

        # Atomic swap — resolve() always sees a complete index
        self.families = new_families
        self.families_with_method = new_families_with_method

        logger.info(f"ToolFamilyIndex: Built {len(self.families)} entity families")

    def resolve(self, entity: str, query_type: QueryType, method: Optional[str] = None) -> Optional[str]:
        """Direct lookup: entity + query_type → tool_id."""
        entity_lower = entity.lower()
        variant = _QUERY_TYPE_TO_VARIANT.get(query_type)
        if not variant:
            return None

        family = self.families_with_method.get(entity_lower, {})

        # If method specified, look for method-qualified variant first
        if method and method != "get":
            method_key = f"{method}_{variant}"
            if method_key in family:
                return family[method_key]
            # For delete/post/put, the variant is usually "id"
            if variant == "list":
                # delete/post on the base entity
                method_key = f"{method}_list"
                if method_key in family:
                    return family[method_key]

        # Fallback to simple family (GET)
        simple_family = self.families.get(entity_lower, {})
        result = simple_family.get(variant)

        # AGGREGATION fallback: groupby ↔ agg
        if not result and variant in ("groupby", "agg"):
            alt = "agg" if variant == "groupby" else "groupby"
            result = simple_family.get(alt)

        return result

    def get_family_tools(self, entity: str) -> Dict[str, str]:
        """Get all tools in an entity family."""
        return self.families.get(entity.lower(), {})

    def get_all_entities(self) -> List[str]:
        """Get all entity keys."""
        return list(self.families.keys())

    def _parse_tool_id(self, tool_id: str):
        """
        Parse tool_id into (entity, variant, method).

        Examples:
            get_Expenses -> ("Expenses", "list", "get")
            get_Expenses_id -> ("Expenses", "id", "get")
            get_Expenses_GroupBy -> ("Expenses", "groupby", "get")
            delete_Expenses_id -> ("Expenses", "id", "delete")
            post_Trips -> ("Trips", "list", "post")
            get_Vehicles_id_Documents -> ("Vehicles", "id_documents", "get")
        """
        parts = tool_id.split("_")
        if len(parts) < 2:
            return None, None, None

        method = parts[0].lower()
        if method not in ("get", "post", "put", "patch", "delete"):
            return None, None, None

        entity = parts[1]

        # Determine variant from remaining parts
        remainder = "_".join(parts[2:]).lower() if len(parts) > 2 else ""

        if not remainder:
            variant = "list"
        else:
            # Check known suffixes
            variant = None
            remainder_check = f"_{remainder}"
            for suffix, var_key in _SUFFIX_MAP:
                if remainder_check.endswith(suffix):
                    # Handle nested resources like _id_documents
                    prefix = remainder_check[:len(remainder_check) - len(suffix)]
                    if prefix and prefix.lstrip("_"):
                        variant = f"{prefix.lstrip('_')}_{var_key}"
                    else:
                        variant = var_key
                    break
            if variant is None:
                variant = remainder

        return entity, variant, method


# Singleton
_family_index: Optional[ToolFamilyIndex] = None
_singleton_lock = threading.Lock()


def get_family_index() -> ToolFamilyIndex:
    """Get singleton ToolFamilyIndex."""
    global _family_index
    if _family_index is None:
        with _singleton_lock:
            if _family_index is None:
                _family_index = ToolFamilyIndex()
    return _family_index
