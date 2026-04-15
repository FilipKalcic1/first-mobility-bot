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

BOOST_ENTITY_MATCH: Final[float] = 0.08        # Tool entity == detected entity
BOOST_ENTITY_MISMATCH: Final[float] = -0.05    # Tool entity ≠ detected entity
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
BOOST_FAMILY_MATCH: Final[float] = 0.09        # Tool Family Index direct match
BOOST_POSSESSIVE_ID: Final[float] = 0.04       # "moj auto" → get_Vehicles_id
BOOST_POSSESSIVE_PROFILE: Final[float] = 0.04  # "moj broj" → get_PersonData
BOOST_POSSESSIVE_LIST_PENALTY: Final[float] = -0.03  # Possessive penalizes list tools
BM25_WEIGHT: Final[float] = 0.07               # Additive BM25 boost weight
BOOST_METHOD_MISMATCH: Final[float] = -0.05    # Verb↔HTTP method mismatch

# Additive cap: prevent extreme swings
MAX_TOTAL_BOOST: Final[float] = 0.15
MIN_TOTAL_BOOST: Final[float] = -0.10


# ---------------------------------------------------------------------------
# Suffix-Intent Boost — reward when query's analytical/structural
# intent matches the tool suffix. Keys are suffix substrings tested via
# `tool_id.lower().endswith(key)` (except `_lookup` handled separately as prefix).
# ---------------------------------------------------------------------------

SUFFIX_INTENT_BOOST: Final[Dict[str, Tuple[List[str], float]]] = {
    "_agg":          (["agregiran", "zbirn", "statisticki", "prosjek", "zbroj", "suma", "count", "broj "], 0.06),
    "_groupby":      (["grupiran", "razvrstan", "po polju", "klasifikacij", "segmentiran", "group by"], 0.06),
    "_projectto":    (["projekcij", "odabrane kolone", "odabrana polja", "samo polja", "parcijaln"], 0.05),
    "_thumb":        (["slicic", "thumbnail", "minijatur", "mala slika"], 0.05),
    "_setasdefault": (["postavi zadan", "kao zadan", "default", "primarn"], 0.05),
    "_multipatch":   (["grupno", "bulk", "masovno", "vise zapisa", "batch"], 0.05),
}

# Lookup tools matched by prefix `get_lookup_`.
LOOKUP_INTENT_BOOST: Final[Tuple[List[str], float]] = (
    ["sifrarnik", "dropdown", "izbornik", "skraceni popis", "popis id"], 0.05
)


# ---------------------------------------------------------------------------
# Tool classification constants
# ---------------------------------------------------------------------------

# Primary entities — main business objects (highest entity boost)
PRIMARY_ENTITIES: Final[FrozenSet[str]] = frozenset([
    'companies', 'vehicles', 'persons', 'expenses', 'cases',
    'teams', 'trips', 'partners', 'tenants', 'roles', 'tags',
    'pools', 'orgunits', 'costcenters', 'equipment', 'booking',
])

# Secondary entities — types, groups, calendars (smaller boost)
SECONDARY_ENTITIES: Final[FrozenSet[str]] = frozenset([
    'vehicletypes', 'persontypes', 'expensetypes', 'casetypes',
    'equipmenttypes', 'triptypes', 'documenttypes', 'expensegroups',
    'vehiclecontracts', 'vehiclecalendar', 'equipmentcalendar',
    'periodicactivities', 'mileagereports', 'schedulingmodels',
])

# Complex suffixes that indicate nested/specialized tools
COMPLEX_SUFFIXES: Final[FrozenSet[str]] = frozenset([
    '_id', '_documents', '_metadata', '_thumb', '_agg', '_groupby',
    '_projectto', '_tree', '_deletebycriteria', 'lookup', 'helper',
    'input', 'stats', '_on', '_from', '_to',
])

# Penalty patterns for list queries (lookup/helper tools)
PENALTY_PATTERNS: Final[List[str]] = [
    'lookup', 'helper', 'input', 'available', 'latest', 'monthly',
    'dashboard', 'stats', '_agg', '_groupby', '_projectto',
    'historicalentries', 'assigned', 'fileids', 'distinctbrands',
]

# Generic CRUD keywords — prevent specific misrouting patterns
GENERIC_CRUD_KEYWORDS: Final[Dict[str, List[str]]] = {
    "post_cases": ["steta", "kvar", "udario", "ogrebao"],
    "post_vehicles": ["rezerv", "booking", "trebam"],
    "post_vehicleshistoricalentries": ["rezerv", "booking", "trebam"],
    "delete_triptypes_deletebycriteria": ["booking", "rezerv"],
    "get_monthlymileages_agg": ["koliko", "stanje", "imam"],
    "get_monthlymileagesassigned": ["koliko", "moja"],
}


# ---------------------------------------------------------------------------
# Verb-based method detection
# ---------------------------------------------------------------------------
# More reliable than ML for common Croatian verbs. Applied before ML intent.

VERB_METHOD_MAP: Final[Dict[str, str]] = {
    "obrisi": "delete", "izbrisi": "delete", "makni": "delete", "izbaci": "delete",
    "azuriraj": "put", "promijeni": "put", "izmijeni": "put", "izmjeni": "put",
    "dodaj": "post", "kreiraj": "post", "napravi": "post", "unesi": "post",
    "upisi": "post",
}


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
