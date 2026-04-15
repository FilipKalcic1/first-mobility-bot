"""Shared LLM JSON-response parser.

Tolerates plain JSON, fenced blocks (```json ... ``` or ``` ... ```), and
prose surrounding a single JSON object.
"""
import json
import re
from typing import Dict, Optional

_JSON_OBJECT_RE = re.compile(r"\{[\s\S]*\}")


def parse_llm_json(text: str) -> Optional[Dict]:
    """Extract and parse the first JSON object in an LLM response.

    Returns None if no parseable object is found.
    """
    candidate = text.strip()
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass

    match = _JSON_OBJECT_RE.search(candidate)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
