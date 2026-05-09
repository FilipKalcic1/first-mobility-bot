"""Output sanitizer — defends against indirect prompt injection through API responses.

CRITICAL SECURITY LAYER: when an API response contains attacker-controlled
strings (vehicle name set to `[SYSTEM: ignore previous instructions]`,
case description, comment fields, person names), feeding those raw to an
LLM in Pass 2 of tool_use creates a prompt injection vector. Attacker
gets the bot to execute their instructions instead of the system's.

This module sanitizes API response payloads BEFORE they're embedded in
LLM context as tool_result. Strategy:

1. **Tag-stripping**: remove `[SYSTEM: ...]`, `<<INST>>`, `### USER:` etc.
2. **Imperative-blunting**: detect "ignore previous", "you are now",
   "new instructions", "act as", "disregard" → escape with U+2063.
3. **Length cap**: truncate any string field over 1000 chars.
4. **Quote-armor**: wrap suspicious strings in explicit quotes so LLM
   treats them as data, not instructions.
5. **Recursive**: applies to nested dicts/lists.

What this does NOT defend against:
- Direct prompt injection from user (handled separately by system prompt design)
- Adversarial training-set poisoning
- Side-channel attacks via timing

Returns sanitized copy; never modifies input. Never raises (silently
returns "(sanitization-failed)" placeholder if something explodes).
"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


# Patterns we strip entirely — these are unambiguous prompt-injection markers
_STRIP_PATTERNS = [
    re.compile(r"\[/?(?:SYSTEM|INST|USER|ASSISTANT|FUNCTION)[^\]]*\]", re.IGNORECASE),
    re.compile(r"<<\s*(?:SYS|INST|/SYS|/INST)\s*>>", re.IGNORECASE),
    re.compile(r"###\s*(?:SYSTEM|USER|ASSISTANT|TOOL):", re.IGNORECASE),
    re.compile(r"<\|im_start\|>", re.IGNORECASE),
    re.compile(r"<\|im_end\|>", re.IGNORECASE),
    re.compile(r"\{\{\s*system\s*\}\}", re.IGNORECASE),
]

# Patterns we WARN on (could be legit user content — don't strip, but flag)
_IMPERATIVE_PATTERNS = [
    re.compile(r"\bignore\s+(?:all\s+)?(?:previous|prior|above|earlier)\s+(?:instructions?|prompts?|rules?)", re.IGNORECASE),
    re.compile(r"\bdisregard\s+(?:all\s+)?(?:previous|prior|above)", re.IGNORECASE),
    re.compile(r"\byou\s+are\s+now\s+(?:a\s+)?", re.IGNORECASE),
    re.compile(r"\bnew\s+instructions?\s*:", re.IGNORECASE),
    re.compile(r"\bact\s+as\s+(?:a\s+)?(?:different|new|admin|root)", re.IGNORECASE),
    re.compile(r"\bforget\s+(?:everything|all|previous|prior)", re.IGNORECASE),
    # Croatian variants — driveri pišu hrvatski
    re.compile(r"\bzanemari\s+(?:sve\s+)?(?:prethodne|gornje)", re.IGNORECASE),
    re.compile(r"\bnova\s+uputa\s*:", re.IGNORECASE),
    re.compile(r"\bzaboravi\s+(?:sve|prethodno)", re.IGNORECASE),
]

# Maximum length for any string field after sanitization
MAX_STRING_LEN = 1000

# Sentinel inserted between potentially-malicious imperatives to break parsing
_NEUTRALIZE_PREFIX = "[QUOTED FROM API DATA]: "


def _sanitize_string(s: str) -> tuple[str, list[str]]:
    """Sanitize a single string. Returns (cleaned, warnings)."""
    if not isinstance(s, str):
        return s, []
    warnings: list[str] = []

    # Pass 1 — strip unambiguous injection markers
    cleaned = s
    for pat in _STRIP_PATTERNS:
        if pat.search(cleaned):
            warnings.append(f"stripped_pattern:{pat.pattern[:30]}")
            cleaned = pat.sub("", cleaned)

    # Pass 2 — detect imperative attempts; wrap whole field in QUOTED prefix
    # to make LLM treat it as data, not an instruction
    flagged_imperative = False
    for pat in _IMPERATIVE_PATTERNS:
        if pat.search(cleaned):
            flagged_imperative = True
            warnings.append(f"imperative:{pat.pattern[:30]}")
            break
    if flagged_imperative and not cleaned.startswith(_NEUTRALIZE_PREFIX):
        cleaned = _NEUTRALIZE_PREFIX + cleaned

    # Pass 3 — length cap
    if len(cleaned) > MAX_STRING_LEN:
        cleaned = cleaned[:MAX_STRING_LEN] + "...[TRUNCATED]"
        warnings.append(f"truncated_from:{len(s)}")

    return cleaned, warnings


def sanitize(data: Any, max_depth: int = 8) -> tuple[Any, list[str]]:
    """Recursively sanitize any JSON-like structure.

    Returns (sanitized_copy, all_warnings). Never raises — if something
    breaks, returns (data, ['sanitize_error']) for the failing branch
    and continues elsewhere.
    """
    return _sanitize(data, max_depth, set())


def _sanitize(data: Any, depth_left: int, seen: set) -> tuple[Any, list[str]]:
    if depth_left <= 0:
        return "[MAX_DEPTH_EXCEEDED]", ["max_depth"]
    try:
        if isinstance(data, str):
            return _sanitize_string(data)
        if isinstance(data, (int, float, bool)) or data is None:
            return data, []
        if isinstance(data, dict):
            out_dict: dict = {}
            warns: list[str] = []
            for k, v in data.items():
                # Sanitize key as well — paranoid but cheap
                clean_k, kw = _sanitize_string(str(k))
                warns.extend(kw)
                clean_v, vw = _sanitize(v, depth_left - 1, seen)
                warns.extend(vw)
                out_dict[clean_k] = clean_v
            return out_dict, warns
        if isinstance(data, (list, tuple)):
            out_list = []
            warns = []
            for item in data:
                clean_item, iw = _sanitize(item, depth_left - 1, seen)
                warns.extend(iw)
                out_list.append(clean_item)
            return out_list, warns
        # Unknown type — fall back to string
        return str(data)[:MAX_STRING_LEN], []
    except Exception as e:  # noqa: BLE001
        logger.warning("sanitize_error: %s", e)
        return "[SANITIZE_ERROR]", [f"sanitize_error:{type(e).__name__}"]


def is_safe(data: Any) -> bool:
    """Quick check: returns False if any sanitize-warning would trigger.
    Cheap pre-filter to skip sanitize call for clean data."""
    if isinstance(data, str):
        for pat in _STRIP_PATTERNS + _IMPERATIVE_PATTERNS:
            if pat.search(data):
                return False
        return len(data) <= MAX_STRING_LEN
    if isinstance(data, dict):
        return all(is_safe(v) for v in data.values())
    if isinstance(data, (list, tuple)):
        return all(is_safe(v) for v in data)
    return True
