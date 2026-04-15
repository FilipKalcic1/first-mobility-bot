"""Shared LLM JSON-response parser.

Tolerates plain JSON, fenced blocks (```json ... ``` or ``` ... ```), and
prose surrounding a single JSON object.
"""
import json
from typing import Dict, Optional


def _extract_first_object(text: str) -> Optional[str]:
    """Return the substring spanning the first balanced {...} object, or None.

    Counts braces while skipping over string literals (and their escapes) so
    braces inside JSON strings don't perturb the depth.
    """
    depth = 0
    start = -1
    i = 0
    n = len(text)
    in_string = False
    escape = False
    while i < n:
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and start != -1:
                    return text[start:i + 1]
        i += 1
    return None


def parse_llm_json(text: str) -> Optional[Dict]:
    """Extract and parse the first JSON object in an LLM response.

    Returns None if no parseable object is found.
    """
    candidate = text.strip()
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass

    extracted = _extract_first_object(candidate)
    if extracted is None:
        return None
    try:
        return json.loads(extracted)
    except json.JSONDecodeError:
        return None
