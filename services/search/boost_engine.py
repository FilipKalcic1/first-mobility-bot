"""
Additive Boost Engine — applies ranking boosts to FAISS search results.

Extracted from unified_search.py UnifiedSearch._apply_boosts().
Stateless: all state (tool docs, categories) is passed in via BoostContext.

Additive scoring keeps FAISS signal dominant while nudging ranking.
A tool with FAISS 0.85 + entity(+0.30) = 1.15 beats
a tool with FAISS 0.30 + entity(+0.30) = 0.60.

Usage:
    from services.search.boost_engine import apply_boosts, BoostContext

    ctx = BoostContext(
        tool_documentation=tool_docs,
        tool_categories=tool_cats,
        primary_action_tools=PRIMARY_ACTION_TOOLS,
    )
    boosted = apply_boosts(query, results, query_type_result, ctx, ...)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from services.faiss_vector_store import SearchResult
from services.search.config import (
    BOOST_BASE_LIST,
    BOOST_CATEGORY,
    BOOST_COMPLEX_SUFFIX_PENALTY,
    BOOST_DOC,
    BOOST_ENTITY_MATCH,
    BOOST_ENTITY_MISMATCH,
    BOOST_FAMILY_MATCH,
    BOOST_GENERIC_CRUD_PENALTY,
    BOOST_HELPER_PENALTY,
    BOOST_LOOKUP_PENALTY,
    BOOST_METHOD_MISMATCH,
    BOOST_POSSESSIVE_ID,
    BOOST_POSSESSIVE_LIST_PENALTY,
    BOOST_POSSESSIVE_PROFILE,
    BOOST_PRIMARY_ACTION,
    BOOST_PRIMARY_ENTITY,
    BOOST_SECONDARY_ENTITY,
    COMPLEX_SUFFIXES,
    GENERIC_CRUD_KEYWORDS,
    LOOKUP_INTENT_BOOST,
    MAX_TOTAL_BOOST,
    MIN_TOTAL_BOOST,
    PENALTY_PATTERNS,
    PRIMARY_ENTITIES,
    SECONDARY_ENTITIES,
    SUFFIX_INTENT_BOOST,
)
from services.text_normalizer import normalize_diacritics

if TYPE_CHECKING:
    from services.types.query_type import QueryType

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Boost context — carries all state needed by the engine
# ---------------------------------------------------------------------------

@dataclass
class BoostContext:
    """Immutable context for boost computation.

    Passed into apply_boosts() so the engine remains stateless.
    """
    tool_documentation: Dict[str, Any] = field(default_factory=dict)
    tool_categories: Dict[str, Any] = field(default_factory=dict)
    primary_action_tools: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Tool classification helpers
# ---------------------------------------------------------------------------

def is_base_list_tool(tool_lower: str) -> bool:
    """Check if tool is a base list endpoint (e.g., get_Companies, get_Vehicles).

    Must start with 'get_' and NOT contain complex suffixes.
    """
    if not tool_lower.startswith('get_'):
        return False
    return not any(suffix in tool_lower for suffix in COMPLEX_SUFFIXES)


def is_pure_entity_tool(tool_lower: str) -> bool:
    """Check if tool is a PURE entity list endpoint (e.g., get_Vehicles).

    Not get_AvailableVehicles, get_LatestX, etc.
    """
    if not tool_lower.startswith('get_'):
        return False
    entity_part = tool_lower[4:]  # Remove "get_"
    return entity_part in PRIMARY_ENTITIES


def is_secondary_entity_tool(tool_lower: str) -> bool:
    """Check if tool is a secondary entity (Types, Groups, etc.)."""
    if not tool_lower.startswith('get_'):
        return False
    entity_part = tool_lower[4:]
    return entity_part in SECONDARY_ENTITIES


def is_simple_id_tool(tool_lower: str) -> bool:
    """Check if tool is a simple _id endpoint (e.g., get_Companies_id).

    Must end with _id (not _id_something), no nested patterns.
    """
    if not tool_lower.startswith('get_'):
        return False
    if not tool_lower.endswith('_id'):
        return False
    if '_id_' in tool_lower:  # e.g., _id_documents
        return False
    if 'lookup' in tool_lower:
        return False
    return True


# ---------------------------------------------------------------------------
# Category matching
# ---------------------------------------------------------------------------

def match_categories(query: str, tool_categories: Dict[str, Any]) -> List[str]:
    """Match query to categories based on keywords."""
    if not tool_categories:
        return []

    matched: List[str] = []
    categories = tool_categories.get("categories", {})
    for cat_name, cat_data in categories.items():
        keywords = cat_data.get("keywords", [])
        if any(kw.lower() in query for kw in keywords):
            matched.append(cat_name)
    return matched


def get_tool_categories(tool_id: str, tool_categories: Dict[str, Any]) -> List[str]:
    """Get categories for a tool."""
    if not tool_categories:
        return []
    tool_map = tool_categories.get("tool_to_categories", {})
    return tool_map.get(tool_id, [])


# ---------------------------------------------------------------------------
# Main boost function
# ---------------------------------------------------------------------------

def compute_suffix_intent_boost(tool_lower: str, query_norm: str) -> float:
    """Reward when tool suffix matches query's analytical intent.

    Returns the largest applicable boost (prevents stacking of overlapping
    suffixes). `tool_lower` and `query_norm` must already be diacritic-normalized.
    """
    best = 0.0
    # Suffix-based (endswith)
    for suffix, (stems, boost) in SUFFIX_INTENT_BOOST.items():
        if tool_lower.endswith(suffix) and any(s in query_norm for s in stems):
            if boost > best:
                best = boost
    # Lookup prefix
    stems, lboost = LOOKUP_INTENT_BOOST
    if tool_lower.startswith("get_lookup_") and any(s in query_norm for s in stems):
        if lboost > best:
            best = lboost
    return best


def apply_boosts(
    query: str,
    results: List[SearchResult],
    ctx: BoostContext,
    *,
    detected_entity: Optional[str] = None,
    effective_query_type: Optional["QueryType"] = None,
    is_possessive: bool = False,
    family_match_tool: Optional[str] = None,
    detected_method: Optional[str] = None,
) -> List[SearchResult]:
    """Apply additive boosts to FAISS results. Mutates and returns `results`."""
    from services.entity_detector import detect_entity
    from services.types.query_type import QueryType

    query_lower = normalize_diacritics(query.lower())

    matched_categories = match_categories(query_lower, ctx.tool_categories)

    if detected_entity is None:
        detected_entity = detect_entity(query)

    if effective_query_type is None:
        effective_query_type = QueryType.UNKNOWN

    query_words = [w for w in query_lower.split() if len(w) > 3]

    for result in results:
        boosts: List[tuple] = []
        base_score = result.score
        tool_lower = result.tool_id.lower()
        total_boost = 0.0

        # TOOL FAMILY DIRECT MATCH — strongest signal
        if family_match_tool and tool_lower == family_match_tool.lower():
            total_boost += BOOST_FAMILY_MATCH
            boosts.append(("family_match", BOOST_FAMILY_MATCH, 0))

        # PRIMARY ACTION TOOL BOOST
        if tool_lower in ctx.primary_action_tools:
            tool_config = ctx.primary_action_tools[tool_lower]
            if any(kw in query_lower for kw in tool_config["keywords"]):
                total_boost += BOOST_PRIMARY_ACTION
                boosts.append(("primary_action", BOOST_PRIMARY_ACTION, 0))

        # ENTITY MATCH/MISMATCH
        if detected_entity:
            tool_parts = result.tool_id.split("_")
            if len(tool_parts) >= 2:
                tool_entity = tool_parts[1].lower()
                # For `get_Lookup_<Entity>` tools, parts[1] is literally "Lookup" —
                # fall through to parts[2] so the real entity matches.
                if tool_entity == "lookup" and len(tool_parts) >= 3:
                    tool_entity = tool_parts[2].lower()
                # Trailing-'s' strip handles plural/singular: "vehicles" → "vehicle"
                # matches "vehiclecalendar"; "expenses" → "expense" matches "expensegroups".
                entity_stem = detected_entity[:-1] if len(detected_entity) > 3 and detected_entity.endswith('s') else detected_entity
                entity_matches = (
                    tool_entity == detected_entity
                    or detected_entity in tool_entity
                    or tool_entity in detected_entity
                    or tool_entity.startswith(entity_stem)
                )
                if entity_matches:
                    total_boost += BOOST_ENTITY_MATCH
                    boosts.append(("entity_match", BOOST_ENTITY_MATCH, 0))
                else:
                    total_boost += BOOST_ENTITY_MISMATCH
                    boosts.append(("entity_mismatch", BOOST_ENTITY_MISMATCH, 0))

        # SUFFIX INTENT BOOST — reward correct suffix semantics
        suffix_boost = compute_suffix_intent_boost(tool_lower, query_lower)
        if suffix_boost > 0:
            total_boost += suffix_boost
            boosts.append(("suffix_intent", suffix_boost, 0))

        # VERB ↔ HTTP METHOD MISMATCH PENALTY
        if detected_method:
            tool_method = (result.method or "").lower()
            if tool_method and tool_method != detected_method:
                total_boost += BOOST_METHOD_MISMATCH
                boosts.append(("method_mismatch", BOOST_METHOD_MISMATCH, 0))

        # GENERIC CRUD PENALTY
        if tool_lower in GENERIC_CRUD_KEYWORDS:
            if any(kw in query_lower for kw in GENERIC_CRUD_KEYWORDS[tool_lower]):
                total_boost += BOOST_GENERIC_CRUD_PENALTY
                boosts.append(("generic_crud_penalty", BOOST_GENERIC_CRUD_PENALTY, 0))

        # CATEGORY BOOST
        if matched_categories:
            tool_cats = get_tool_categories(result.tool_id, ctx.tool_categories)
            if any(cat in tool_cats for cat in matched_categories):
                total_boost += BOOST_CATEGORY
                boosts.append(("category", BOOST_CATEGORY, 0))

        # DOCUMENTATION BOOST
        if ctx.tool_documentation and result.tool_id in ctx.tool_documentation:
            doc = ctx.tool_documentation[result.tool_id]
            doc_text = " ".join([
                doc.get("purpose", ""),
                " ".join(doc.get("when_to_use", [])),
                " ".join(doc.get("example_queries_hr", []))
            ]).lower()
            if any(word in doc_text for word in query_words):
                total_boost += BOOST_DOC
                boosts.append(("doc", BOOST_DOC, 0))

        # STRUCTURAL BOOST by QueryType
        if effective_query_type == QueryType.LIST:
            if is_base_list_tool(tool_lower):
                if is_pure_entity_tool(tool_lower):
                    total_boost += BOOST_PRIMARY_ENTITY
                    boosts.append(("primary_entity", BOOST_PRIMARY_ENTITY, 0))
                elif is_secondary_entity_tool(tool_lower):
                    total_boost += BOOST_SECONDARY_ENTITY
                    boosts.append(("secondary_entity", BOOST_SECONDARY_ENTITY, 0))
                else:
                    total_boost += BOOST_BASE_LIST
                    boosts.append(("base_list", BOOST_BASE_LIST, 0))

            if any(x in tool_lower for x in PENALTY_PATTERNS):
                total_boost += BOOST_HELPER_PENALTY
                boosts.append(("helper_penalty", BOOST_HELPER_PENALTY, 0))

        elif effective_query_type == QueryType.SINGLE_ENTITY:
            if is_simple_id_tool(tool_lower):
                entity_name = tool_lower[4:].replace('_id', '')
                if entity_name in PRIMARY_ENTITIES:
                    total_boost += BOOST_PRIMARY_ENTITY
                    boosts.append(("primary_id", BOOST_PRIMARY_ENTITY, 0))
                else:
                    total_boost += BOOST_BASE_LIST
                    boosts.append(("simple_id", BOOST_BASE_LIST, 0))
            if any(s in tool_lower for s in ['_documents', '_metadata', '_thumb', '_agg', '_groupby']):
                total_boost += BOOST_COMPLEX_SUFFIX_PENALTY
                boosts.append(("complex_suffix_penalty", BOOST_COMPLEX_SUFFIX_PENALTY, 0))
            if 'lookup' in tool_lower:
                total_boost += BOOST_LOOKUP_PENALTY
                boosts.append(("lookup_penalty", BOOST_LOOKUP_PENALTY, 0))

        # POSSESSIVE BOOST — "moj auto" → prefer _id tools
        if is_possessive:
            if '_id' in tool_lower and '_id_' not in tool_lower:
                total_boost += BOOST_POSSESSIVE_ID
                boosts.append(("possessive_id", BOOST_POSSESSIVE_ID, 0))
            elif 'masterdata' in tool_lower or 'persondata' in tool_lower:
                total_boost += BOOST_POSSESSIVE_PROFILE
                boosts.append(("possessive_profile", BOOST_POSSESSIVE_PROFILE, 0))
            elif is_base_list_tool(tool_lower) and '_id' not in tool_lower:
                total_boost += BOOST_POSSESSIVE_LIST_PENALTY
                boosts.append(("possessive_list_penalty", BOOST_POSSESSIVE_LIST_PENALTY, 0))

        # Cap additive boost so FAISS signal stays dominant.
        total_boost = max(MIN_TOTAL_BOOST, min(MAX_TOTAL_BOOST, total_boost))
        result.score = base_score + total_boost

        # Store boosts for debugging
        result.boosts_applied = [(name, val, result.score) for name, val, _ in boosts]
        result.base_score = base_score

    # Log top-3 boost breakdown
    top3 = sorted(results, key=lambda r: r.score, reverse=True)[:3]
    for r in top3:
        if r.boosts_applied:
            chain = " + ".join(
                f"{name}({val:+.2f})"
                for name, val, _ in r.boosts_applied
            )
            logger.debug(
                "Boost: %s base=%.3f + [%s] = %.3f",
                r.tool_id, r.base_score, chain, r.score
            )

    return results
