"""
Text Normalization — Croatian diacritics, synonyms, and query preprocessing.

Extracted from intent_classifier.py. Single source of truth for all text
normalization in the routing pipeline.

Performance: O(n) for diacritics (character map), O(n) for synonyms (word split).

Usage:
    from services.text_normalizer import (
        normalize_query, normalize_diacritics, normalize_synonyms,
        DIACRITIC_MAP, SYNONYM_MAP,
        CROATIAN_STOPWORDS, extract_tool_from_text, extract_query_patterns,
    )

    normalized = normalize_query("Pokaži mi sva auta čija je registracija...")
    # → "pokazi mi sva vozila cija je registracija..."
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from typing import Dict, Final, FrozenSet, List, Optional

from services.config_loader import load_json as _load_json

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Croatian diacritic mapping (O(1) lookup per character)
# ---------------------------------------------------------------------------

DIACRITIC_MAP: Final[Dict[str, str]] = {
    'č': 'c', 'ć': 'c', 'đ': 'd', 'š': 's', 'ž': 'z',
    'Č': 'C', 'Ć': 'C', 'Đ': 'D', 'Š': 'S', 'Ž': 'Z',
}

# str.translate() table — 5x faster than character-by-character loop
_DIACRITIC_TABLE: Final = str.maketrans(DIACRITIC_MAP)


# ---------------------------------------------------------------------------
# Synonym map (canonical forms for common Croatian variations)
#
# Lazy-loaded (not module-level) so a missing/malformed
# config/linguistic/typo_synonyms.json degrades to "no synonym replacement"
# instead of crashing the worker at import time.
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _get_synonym_map() -> Dict[str, str]:
    try:
        data = _load_json("linguistic", "typo_synonyms.json")
        m = data.get("synonym_map") if isinstance(data, dict) else None
        if not isinstance(m, dict):
            logger.error("typo_synonyms.json: expected {'synonym_map': {...}}, got %r", type(m))
            return {}
        return m
    except (FileNotFoundError, ValueError, KeyError) as e:
        logger.error(
            "typo_synonyms.json unloadable, slang normalization disabled: %s", e,
        )
        return {}


@lru_cache(maxsize=1)
def _get_synonym_pattern() -> re.Pattern[str]:
    """Pre-compiled, longest-first to match 'automobili' before 'auto'."""
    m = _get_synonym_map()
    if not m:
        # Never-matching pattern so .sub() is a no-op without an exception
        return re.compile(r"(?!)")
    return re.compile(
        r'\b(' + '|'.join(re.escape(k) for k in sorted(m, key=len, reverse=True)) + r')\b',
        flags=re.IGNORECASE,
    )


# Backwards-compat shim — old code imported SYNONYM_MAP as a constant. Keep
# a module-level proxy that resolves on each access so missing-file scenarios
# still don't crash at import.
class _SynonymMapProxy(dict):
    def __getitem__(self, key):  # noqa: D401
        return _get_synonym_map()[key]
    def __contains__(self, key):
        return key in _get_synonym_map()
    def get(self, key, default=None):
        return _get_synonym_map().get(key, default)
    def __iter__(self):
        return iter(_get_synonym_map())
    def __len__(self):
        return len(_get_synonym_map())


SYNONYM_MAP: Final[Dict[str, str]] = _SynonymMapProxy()  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Croatian stopwords — single source of truth (union of all 3 prior sets)
# Used by: feedback_analyzer, query_pattern_learner, feedback_learning_service
# ---------------------------------------------------------------------------

CROATIAN_STOPWORDS: Final[FrozenSet[str]] = frozenset([
    # Pronouns & particles
    "a", "ako", "ali", "bi", "bez", "bilo", "bih", "da", "do",
    "dok", "ga", "gdje", "i", "ili", "iz", "ja", "je", "jedan", "jedna",
    "jedno", "jer", "još", "kada", "kako", "kao", "koja", "koje", "koji",
    "koju", "li", "me", "mi", "može", "mogu", "na", "ne", "nego", "neka",
    "neki", "nešto", "ni", "nije", "no", "o", "od", "ona", "one", "oni",
    "ono", "pa", "po", "pod", "prema", "pri", "s", "sa", "sam", "samo",
    "se", "smo", "su", "sve", "svi", "ta", "te", "ti", "to", "toga",
    "tu", "u", "uz", "va", "već", "vi", "za", "što", "će", "ću", "tom",
    "mu", "ju", "ih", "njoj", "njemu", "njima", "nje", "njega", "svoj",
    "moj", "tvoj", "naš", "vaš", "njihov", "ovaj", "taj", "onaj",
    # Verbs / auxiliaries
    "biti", "bio", "bila", "bilo", "smo", "ste", "si",
    # Bot-specific action words (should not be part of tool patterns)
    "molim", "hvala", "hej", "bok", "daj", "dajte",
    "pokaži", "prikaži", "reci", "kaži", "dohvati", "pronađi",
    "trebam", "treba", "želim", "hoću", "mogu", "možeš",
    "vas", "te",
])


# ---------------------------------------------------------------------------
# Tool name extraction — single source of truth
# Used by: feedback_analyzer, query_pattern_learner, feedback_learning_service
# ---------------------------------------------------------------------------

_TOOL_PATTERNS: Final = [
    re.compile(r"(get_\w+)"),
    re.compile(r"(post_\w+)"),
    re.compile(r"(put_\w+)"),
    re.compile(r"(patch_\w+)"),
    re.compile(r"(delete_\w+)"),
]

_TOOL_VERB_PATTERNS: Final = [
    re.compile(r"koristiti\s+(\w+)"),
    re.compile(r"trebao?\s+(\w+)"),
]

_TOOL_FALLBACK_RE: Final = re.compile(
    r"(?:get|post|put|patch|delete)_\w+"
)


def extract_tool_from_text(text: str) -> Optional[str]:
    """Extract tool name (e.g. get_vehicles) from free text.

    Searches for CRUD-prefixed tool identifiers and Croatian verb patterns.
    Returns the first match, normalized to lowercase.

    >>> extract_tool_from_text("koristi get_vehicles za popis")
    'get_vehicles'
    >>> extract_tool_from_text("trebao post_mileage_entry")
    'post_mileage_entry'
    >>> extract_tool_from_text("nema alata ovdje")
    """
    if not text:
        return None

    text_lower = text.lower()

    # 1. Try CRUD-prefixed patterns (most reliable)
    for pattern in _TOOL_PATTERNS:
        match = pattern.search(text_lower)
        if match:
            return match.group(1)

    # 2. Try Croatian verb patterns ("koristiti X", "trebao X")
    for pattern in _TOOL_VERB_PATTERNS:
        match = pattern.search(text_lower)
        if match:
            candidate = match.group(1)
            # If candidate isn't already a CRUD-prefixed name, try to find full name
            if not candidate.startswith(("get_", "post_", "put_", "patch_", "delete_")):
                full_match = re.search(
                    r"(get|post|put|patch|delete)_\w*" + re.escape(candidate),
                    text_lower,
                )
                if full_match:
                    return full_match.group(0)
            return candidate

    # 3. Fallback: find any CRUD-prefixed identifier anywhere
    fallback = _TOOL_FALLBACK_RE.findall(text_lower)
    if fallback:
        return fallback[0] if isinstance(fallback[0], str) else fallback[0][0]

    return None


# ---------------------------------------------------------------------------
# N-gram pattern extraction — single source of truth
# Used by: feedback_analyzer, query_pattern_learner, feedback_learning_service
# ---------------------------------------------------------------------------

_WORD_RE: Final = re.compile(r"\b\w+\b")


def extract_query_patterns(
    query: str,
    *,
    stopwords: FrozenSet[str] = CROATIAN_STOPWORDS,
    min_word_len: int = 3,
    min_pattern_len: int = 4,
    max_patterns: int = 5,
) -> List[str]:
    """Extract meaningful n-gram patterns from a query.

    Removes stopwords, then generates 2-gram, 3-gram, and 1-gram patterns
    (preferring longer patterns). Returns up to ``max_patterns`` results.

    >>> extract_query_patterns("pokaži mi sva vozila u Zagrebu")
    ['vozila zagrebu', 'sva vozila zagrebu', 'vozila', 'zagrebu']
    """
    if not query:
        return []

    words = [
        w for w in _WORD_RE.findall(query.lower())
        if w not in stopwords and len(w) >= min_word_len
    ]

    patterns: List[str] = []
    for n in [2, 3, 1]:  # Prefer 2-3 word patterns
        for i in range(len(words) - n + 1):
            pattern = " ".join(words[i:i + n])
            if len(pattern) >= min_pattern_len:
                patterns.append(pattern)

    return patterns[:max_patterns]


# ---------------------------------------------------------------------------
# Public API — original functions
# ---------------------------------------------------------------------------

def normalize_diacritics(text: str) -> str:
    """Remove Croatian diacritics from text.

    Uses str.translate() for optimal performance (~5x faster than loop).

    >>> normalize_diacritics("čćđšž ČĆĐŠŽ")
    'ccdszz CCDSZ'
    """
    return text.translate(_DIACRITIC_TABLE)


def normalize_synonyms(text: str) -> str:
    """Replace common synonyms with canonical forms.

    Uses word-boundary regex so punctuation adjacent to words
    (e.g. ``"auto."`` or ``"auta,""``) is handled correctly. When
    typo_synonyms.json is missing or malformed, falls back to identity
    (no replacement) — never raises.

    >>> normalize_synonyms("prikaži mi sva auta")
    'prikaži mi sva vozila'
    >>> normalize_synonyms("trebam auto.")
    'trebam vozilo.'
    """
    m = _get_synonym_map()
    if not m:
        return text
    return _get_synonym_pattern().sub(
        lambda match: m[match.group(1).lower()], text,
    )


def normalize_query(text: str) -> str:
    """Full normalization pipeline for ML classification input.

    Steps (in order):
    1. Lowercase
    2. Strip whitespace
    3. Remove diacritics (ž→z, č→c, etc.)
    4. Replace synonyms (auto→vozilo, etc.)

    >>> normalize_query("  Pokaži mi AUTA s Čistoćom  ")
    'pokazi mi vozila s cistocom'
    """
    text = text.lower().strip()
    text = normalize_diacritics(text)
    text = normalize_synonyms(text)
    return text


_LLM_CONTROL_RE = re.compile(
    r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]'  # ASCII/Latin-1 control chars
    r'|[\u200b-\u200f\u2028-\u202f\ufeff]'        # Zero-width, directional overrides, BOM
)


def sanitize_for_llm(text: str, max_len: int = 500) -> str:
    """Sanitize user input before embedding in LLM prompts.

    Truncates to max_len, strips control/invisible chars, replaces
    brackets and double quotes to prevent injection-style formatting issues.
    """
    if not text:
        return ""
    text = text[:max_len]
    text = _LLM_CONTROL_RE.sub('', text)
    text = text.replace("\r", " ").replace("\n", " ")
    text = text.replace('"', "'")
    text = text.replace("{", "(").replace("}", ")")
    text = text.replace("[", "(").replace("]", ")")
    return text.strip()


# Pre-compiled pattern for European number format cleaning
_EURO_NUM_RE = re.compile(r'(\d)[.,](\d{3})\b')
_DIGITS_RE = re.compile(r'\d+')


def clean_european_number(text: str) -> Optional[int]:
    """
    Parse European number format (45.000 or 1.234.567) → int (45000 / 1234567).

    Strips thousands separators (dots/commas before exactly 3 digits),
    then extracts the first integer found.

    Returns None if no number found.
    """
    cleaned = text
    while True:
        new = _EURO_NUM_RE.sub(r'\1\2', cleaned)
        if new == cleaned:
            break
        cleaned = new
    numbers = _DIGITS_RE.findall(cleaned)
    return int(numbers[0]) if numbers else None
