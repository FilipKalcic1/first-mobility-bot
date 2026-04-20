"""
Consolidated entity detection module.

Single source of truth for detecting which entity (vehicles, expenses, etc.)
a user query refers to. Used by both unified_search.py and faiss_vector_store.py.

Uses diacritic-normalized substring matching to handle Croatian declensions.

Two-tier approach:
1. CURATED_STEMS: Hand-tuned stems for primary entities (highest priority, ordering matters)
2. AUTO_STEMS: Auto-generated from tool_documentation.json synonyms_hr (covers all 75 entities)
"""
import json
import logging
import os
import re
from typing import Optional, Dict, List

from services.config_loader import load_json
from services.text_normalizer import normalize_diacritics

logger = logging.getLogger(__name__)


def _normalize(text: str) -> str:
    """Normalize text: lowercase + remove diacritics."""
    return normalize_diacritics(text.lower())


# Hand-curated entity stems — ordering matters, more specific entities first.
# Data in config/domain/entity_stems.json so linguists can edit without Python PR.
CURATED_STEMS: Dict[str, List[str]] = load_json("domain", "entity_stems.json")["curated_stems"]


# Minimum stem length to avoid false positives
_MIN_STEM_LEN = 4

# Croatian stopwords + generic stem prefixes that must never become entity stems.
# Data in config/linguistic/stopwords_hr.json.
_sw_cfg = load_json("linguistic", "stopwords_hr.json")
_CROATIAN_STOPWORDS = set(_sw_cfg["stopwords"])
_STOP_STEMS = set(_sw_cfg["stop_stems"])


def _is_stopword(word: str) -> bool:
    """Check if a Croatian word is a generic stopword that must not become a stem."""
    wl = word.lower()
    if wl in _CROATIAN_STOPWORDS:
        return True
    # Also check all prefixes of the word against stopwords
    # (handles declensions like "koji"/"kojeg"/"kojim" — if prefix is a stopword, skip)
    for length in (4, 5, 6):
        prefix = wl[:length]
        if prefix in _CROATIAN_STOPWORDS or prefix in _STOP_STEMS:
            return True
    return False


def _generate_stems_from_synonym(synonym: str) -> List[str]:
    """
    Generate substring stems from a synonym.

    Strategy: only emit multi-word phrase stems AND single nouns that survive
    a strict Croatian stopword filter. Avoids generating stems from generic
    verbs/pronouns/modals that would false-positive on nearly every query.
    """
    stems = []
    syn_norm = _normalize(synonym)

    # Single-word stem: only if the word is NOT a stopword and not a stopword
    # prefix. Croatian declensions handled by prefix matching in _is_stopword.
    words = syn_norm.split()
    for word in words:
        if len(word) < _MIN_STEM_LEN:
            continue
        if _is_stopword(word):
            continue
        stem = word[:min(len(word), 5)]
        # Double-check the truncated stem isn't a stop-stem or stopword
        if stem in _STOP_STEMS or stem in _CROATIAN_STOPWORDS:
            continue
        stems.append(stem)

    # Multi-word stem: use the full normalized synonym if 2+ words and short enough.
    # Multi-word phrases are specific ("leasing vozila", "centar troska") and safe.
    if len(words) >= 2 and len(syn_norm) <= 25:
        stems.append(syn_norm)

    return stems


def _phrase_starts_with_stopword(phrase: str) -> bool:
    """True if phrase's first word is a Croatian stopword."""
    first = phrase.split(" ", 1)[0] if phrase else ""
    return _is_stopword(first) if first else True


def build_auto_stems(tool_docs: dict) -> Dict[str, List[str]]:
    """
    Auto-generate entity→stems mapping from tool_documentation.json.

    Parses tool_id to extract entity, then collects stems from synonyms_hr.
    Only adds entities NOT already in CURATED_STEMS.

    Auto-stems are restricted to MULTI-WORD phrase stems only. Single-word
    stems for auto entities are unsafe because we can't manually validate
    distinctiveness — a single Croatian word can false-positive on many
    unrelated queries (e.g., "pomoc", "unos", "trosk"). Legitimate
    single-word entities (vozil, oprem, trosk, etc.) are in CURATED_STEMS.
    """
    entity_stems: Dict[str, set] = {}

    for tool_id, doc in tool_docs.items():
        parts = tool_id.split("_")
        if len(parts) < 2:
            continue

        entity = parts[1].lower()

        # Skip entities already fully covered by curated stems
        if entity in CURATED_STEMS:
            continue

        synonyms = doc.get("synonyms_hr", [])
        for syn in synonyms:
            if len(syn) < 3:
                continue
            syn_stripped = syn.strip()
            for stem in _generate_stems_from_synonym(syn_stripped):
                # Auto-path: keep only multi-word phrase stems that don't
                # start with a Croatian stopword. Single-word stems are
                # filtered out (see docstring).
                if " " not in stem:
                    continue
                if _phrase_starts_with_stopword(stem):
                    continue
                entity_stems.setdefault(entity, set()).add(stem)

    # Convert to sorted lists and filter out overly short stems
    result = {}
    for entity, stems in entity_stems.items():
        filtered = [s for s in stems if len(s) >= _MIN_STEM_LEN]
        if filtered:
            result[entity] = sorted(filtered)

    return result


# Combined stems: curated (priority) + auto-generated (fallback)
# Initialized lazily when tool_documentation.json is available
ENTITY_STEMS: Dict[str, List[str]] = dict(CURATED_STEMS)
_auto_stems_loaded = False


def load_auto_stems(tool_docs: Optional[dict] = None):
    """
    Load auto-generated stems from tool_documentation.json.
    Called during application startup when tool docs are available.
    """
    global _auto_stems_loaded

    if _auto_stems_loaded:
        return

    if tool_docs is None:
        # Try to load from file
        config_dir = os.path.join(os.path.dirname(__file__), '..', 'config')
        doc_path = os.path.join(config_dir, 'tool_documentation.json')
        if os.path.exists(doc_path):
            with open(doc_path, 'r', encoding='utf-8') as f:
                tool_docs = json.load(f)

    if tool_docs:
        auto = build_auto_stems(tool_docs)
        # Merge: curated stems take priority (they're in ENTITY_STEMS already)
        # Auto stems fill gaps for uncovered entities
        for entity, stems in auto.items():
            if entity not in ENTITY_STEMS:
                ENTITY_STEMS[entity] = stems

        _auto_stems_loaded = True
        logger.info(
            f"EntityDetector: {len(CURATED_STEMS)} curated + "
            f"{len(auto)} auto-generated entities = {len(ENTITY_STEMS)} total"
        )


_NORMALIZED_STEMS_CACHE: Dict[str, List[str]] = {}
_NORMALIZED_STEMS_FINGERPRINT: tuple = ()


def _get_normalized_stems() -> Dict[str, List[str]]:
    global _NORMALIZED_STEMS_FINGERPRINT, _NORMALIZED_STEMS_CACHE
    fp = tuple((k, len(v)) for k, v in ENTITY_STEMS.items())
    if fp != _NORMALIZED_STEMS_FINGERPRINT:
        _NORMALIZED_STEMS_CACHE = {
            entity: [_normalize(s) for s in stems]
            for entity, stems in ENTITY_STEMS.items()
        }
        _NORMALIZED_STEMS_FINGERPRINT = fp
    return _NORMALIZED_STEMS_CACHE


# Entities that describe an operation/view rather than a business object.
# When both a modifier and a concrete entity match, the concrete entity wins.
_MODIFIER_ENTITIES = frozenset({"stats", "metadata", "settings", "dashboarditems", "demo"})


def detect_entity(query: str) -> Optional[str]:
    """
    Detect which entity a query refers to.

    Uses diacritic-normalized substring matching.
    Returns entity key (e.g., 'vehicles', 'expenses') or None.

    More specific entities are checked first: stems are sorted longest-first
    so 'tipovi putovanj' (triptypes) matches before 'putovanj' (trips).

    Modifier entities (stats, metadata, settings) describe an operation, not a
    business entity.  When a modifier AND a concrete entity both match, the
    concrete entity wins.  E.g. "statistika vozila" → vehicles, not stats.
    """
    query_norm = _normalize(query)
    # Build (stem, entity) pairs sorted by stem length descending.
    # Longer stems are more specific and should match first.
    all_stems = []
    for entity, stems_norm in _get_normalized_stems().items():
        for stem_norm in stems_norm:
            all_stems.append((stem_norm, entity))
    all_stems.sort(key=lambda x: len(x[0]), reverse=True)

    first_match = None
    for stem_norm, entity in all_stems:
        if stem_norm in query_norm:
            if entity not in _MODIFIER_ENTITIES:
                return entity
            if first_match is None:
                first_match = entity

    # Only modifier entities matched (e.g. bare "statistika")
    return first_match


def get_entity_keywords(entity: str) -> list:
    """Get the keyword stems for a given entity."""
    return ENTITY_STEMS.get(entity, [])


def get_all_entities() -> list:
    """Get all entity keys."""
    return list(ENTITY_STEMS.keys())


# --- Possessive signal detection ---

# All Croatian declension forms of "moj" (my) — F1 audit fix
# Nom: moj/moja/moje/moji, Gen: mojeg/mojega/mojih, Dat/Lok: mojem/mojemu/mojoj/mojim/mojima
# Akuz: moju, Inst: mojim/mojom/mojima. Contracted forms (mom/mome/momu) handled separately.
_MOJ = r'(?:moj[eai]?|mojeg|mojega|mojem|mojemu|mojoj|mojih|mojim|mojima|mojom|moju)'

_POSSESSIVE_PATTERNS = [
    # Vehicle possessives — \b prevents "nemoj vozilo" false positive (D2)
    (re.compile(r'\b' + _MOJ + r'\s+vozil'), 'vehicles'),
    (re.compile(r'\b' + _MOJ + r'\s+aut'), 'vehicles'),
    (re.compile(r'\bmom(?:e|u|em)?\s+vozil'), 'vehicles'),
    (re.compile(r'\bmom(?:e|u|em)?\s+aut'), 'vehicles'),
    (re.compile(r'\bmoj\s+aut[oi]'), 'vehicles'),
    # Expense possessives
    (re.compile(r'\b' + _MOJ + r'\s+trosk'), 'expenses'),
    (re.compile(r'\b' + _MOJ + r'\s+trosak'), 'expenses'),
    # Trip possessives
    (re.compile(r'\b' + _MOJ + r'\s+putovanj'), 'trips'),
    (re.compile(r'\b' + _MOJ + r'\s+voznj'), 'trips'),
    # Case possessives
    (re.compile(r'\b' + _MOJ + r'\s+slucaj'), 'cases'),
    (re.compile(r'\b' + _MOJ + r'\s+stet'), 'cases'),
    # Calendar/reservation possessives
    (re.compile(r'\b' + _MOJ + r'\s+rezervacij'), 'vehiclecalendar'),
    # Equipment possessives
    (re.compile(r'\b' + _MOJ + r'\s+oprem'), 'equipment'),
    # Generic "my" without entity — still possessive
    (re.compile(r'\b' + _MOJ + r'\s+podac'), None),  # "moji podaci" — possessive but no specific entity
    (re.compile(r'\bpodaci\s+o\s+meni'), None),
]


def detect_possessive(query: str) -> tuple:
    """
    Detect possessive patterns in query.

    Returns:
        (is_possessive: bool, entity_hint: Optional[str])
        entity_hint is the detected entity or None if possessive but generic.
    """
    q = _normalize(query)
    for pattern, entity in _POSSESSIVE_PATTERNS:
        if pattern.search(q):
            return True, entity
    return False, None


# --- User profile query detection ---

_USER_PROFILE_STEMS = [
    "telefon", "mobitel", "gsm", "broj telefon",
    "email", "e-mail", "mail adres",
    "person id", "moj id", "moj profil",
    "tenant id", "tko sam", "koji sam",
    "moj broj", "moj email", "moja adres",
]


def detect_user_profile_query(query: str) -> bool:
    """
    Detect queries about user's own profile data.

    These queries should route to get_PersonData or direct_response,
    not to entity list endpoints like get_Vehicles.
    """
    q = _normalize(query)
    return any(stem in q for stem in _USER_PROFILE_STEMS)
