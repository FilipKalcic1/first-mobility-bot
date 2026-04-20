"""
Search Pipeline Configuration — constants, boost values, entity keywords.

All tunable parameters for the 6-step unified search pipeline are centralized
here. Changing boost values or entity keywords only requires editing this file.

Design:
  - Additive boost model (v4.0): FAISS score stays dominant, boosts only nudge.
  - Cap enforced: total boost ∈ [MIN_TOTAL_BOOST, MAX_TOTAL_BOOST].
  - Entity keywords are stem-based (Croatian diacritics removed).
"""

from __future__ import annotations

from typing import Dict, FrozenSet, List, Final, Tuple


# ---------------------------------------------------------------------------
# Additive boost values — kept small so FAISS cosine stays dominant.
# Boosts are tie-breakers between top-5 candidates (FAISS spread ~0.01-0.05).
# Total post-boost capped to [MIN_TOTAL_BOOST, MAX_TOTAL_BOOST].
# ---------------------------------------------------------------------------

BOOST_ENTITY_MATCH: Final[float] = 0.05        # Tool entity == detected entity
BOOST_ENTITY_MISMATCH: Final[float] = 0.0      # Tool entity ≠ detected entity (penalty removed — reranker handles)
BOOST_QUERY_TYPE_MATCH: Final[float] = 0.06    # Suffix matches QueryType preference
BOOST_QUERY_TYPE_EXCLUDED: Final[float] = -0.04  # Suffix excluded for this QueryType
BOOST_PRIMARY_ENTITY: Final[float] = 0.05      # Primary entity tool (get_Vehicles)
BOOST_SECONDARY_ENTITY: Final[float] = 0.03    # Secondary entity (Types, Groups)
BOOST_BASE_LIST: Final[float] = 0.02           # Any base list tool
BOOST_PRIMARY_ACTION: Final[float] = 0.04      # PRIMARY_ACTION_TOOLS keyword match
BOOST_CATEGORY: Final[float] = 0.02            # Category match
BOOST_DOC: Final[float] = 0.02                 # Query words in tool docs
BOOST_HELPER_PENALTY: Final[float] = -0.04     # Lookup/helper/stats for list query
BOOST_COMPLEX_SUFFIX_PENALTY: Final[float] = -0.03  # Complex suffix for entity query
BOOST_LOOKUP_PENALTY: Final[float] = -0.03     # Lookup tool for single entity query
BOOST_GENERIC_CRUD_PENALTY: Final[float] = -0.05  # Hardcoded misrouting prevention
BOOST_FAMILY_MATCH: Final[float] = 0.06        # Tool Family Index direct match
BOOST_POSSESSIVE_ID: Final[float] = 0.04       # "moj auto" → get_Vehicles_id
BOOST_POSSESSIVE_PROFILE: Final[float] = 0.04  # "moj broj" → get_PersonData
BOOST_POSSESSIVE_LIST_PENALTY: Final[float] = -0.03  # Possessive penalizes list tools
BM25_WEIGHT: Final[float] = 0.07               # Additive BM25 boost weight
BOOST_METHOD_MISMATCH: Final[float] = -0.05    # Verb↔HTTP method mismatch

# Additive cap: prevent extreme swings
MAX_TOTAL_BOOST: Final[float] = 0.12
MIN_TOTAL_BOOST: Final[float] = -0.08


# ---------------------------------------------------------------------------
# Suffix-Intent Boost — reward when query's analytical/structural
# intent matches the tool suffix. Keys are suffix substrings tested via
# `tool_id.lower().endswith(key)` (except `_lookup` handled separately as prefix).
# ---------------------------------------------------------------------------

from services.config_loader import load_json as _load_json_boost
_sfx = _load_json_boost("per_tool", "suffix_boosts.json")
SUFFIX_INTENT_BOOST: Final[Dict[str, Tuple[List[str], float]]] = {
    k: (v["keywords"], v["boost"]) for k, v in _sfx["by_suffix"].items()
}
LOOKUP_INTENT_BOOST: Final[Tuple[List[str], float]] = (
    _sfx["lookup_prefix"]["keywords"], _sfx["lookup_prefix"]["boost"]
)


# ---------------------------------------------------------------------------
# Tool classification constants
# ---------------------------------------------------------------------------

# Entity tier classifications (boost weights applied downstream).
# Data in config/per_tool/entity_tiers.json, config/per_tool/crud_penalties.json.
_tiers = _load_json_boost("per_tool", "entity_tiers.json")
PRIMARY_ENTITIES: Final[FrozenSet[str]] = frozenset(_tiers["primary"])
SECONDARY_ENTITIES: Final[FrozenSet[str]] = frozenset(_tiers["secondary"])
COMPLEX_SUFFIXES: Final[FrozenSet[str]] = frozenset(_tiers["complex_suffixes"])
PENALTY_PATTERNS: Final[List[str]] = _tiers["penalty_patterns"]

GENERIC_CRUD_KEYWORDS: Final[Dict[str, List[str]]] = _load_json_boost(
    "per_tool", "crud_penalties.json"
)["generic_crud_keywords"]


# ---------------------------------------------------------------------------
# Verb-based method detection
# ---------------------------------------------------------------------------
# More reliable than ML for common Croatian verbs. Applied before ML intent.

from services.config_loader import load_json as _load_json
VERB_METHOD_MAP: Final[Dict[str, str]] = _load_json("domain", "http_verbs_hr.json")["verb_to_method"]


# ---------------------------------------------------------------------------
# Method verbs and suffix descriptions (for LLM entity descriptions)
# ---------------------------------------------------------------------------

METHOD_VERBS: Final[Dict[str, str]] = {
    "GET": "Dohvati", "POST": "Kreiraj", "PUT": "Ažuriraj",
    "PATCH": "Djelom. ažuriraj", "DELETE": "Obriši",
}

SUFFIX_DESCRIPTIONS: Final[Dict[str, str]] = {
    "_id": " po ID-u", "_deletebycriteria": " prema kriterijima",
    "_groupby": " grupirano", "_agg": " agregirano",
    "_documents": " dokumente", "_metadata": " metapodatke",
    "_multipatch": " bulk ažuriranje", "_projectto": " projekcija",
    "_setasdefault": " postavi zadano", "_filter": " filtrirano",
    "_thumb": " thumbnail",
}
