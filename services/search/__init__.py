"""
Search Package — decomposed from unified_search.py.

Re-exports all public symbols so existing imports continue to work:
    from services.search import UnifiedSearch, get_unified_search, ...

Internal structure:
    search/config.py         — boost constants, entity keywords, verb maps
    search/boost_engine.py   — additive boost logic + tool classification helpers
"""

from services.search.config import (  # noqa: F401
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
    BOOST_POSSESSIVE_ID,
    BOOST_POSSESSIVE_LIST_PENALTY,
    BOOST_POSSESSIVE_PROFILE,
    BOOST_PRIMARY_ACTION,
    BOOST_PRIMARY_ENTITY,
    BOOST_QUERY_TYPE_EXCLUDED,
    BOOST_QUERY_TYPE_MATCH,
    BOOST_SECONDARY_ENTITY,
    MAX_TOTAL_BOOST,
    MIN_TOTAL_BOOST,
    PRIMARY_ENTITIES,
    VERB_METHOD_MAP,
)

from services.search.boost_engine import (  # noqa: F401
    BoostContext,
    apply_boosts,
    is_base_list_tool,
    is_pure_entity_tool,
    is_secondary_entity_tool,
    is_simple_id_tool,
    match_categories,
    get_tool_categories,
)
