"""Multi-intent detector — flags queries that contain >1 distinct intent.

Real users sometimes pack 2 intents in one message:
  - "pokaži km i rezerviraj sutra"
  - "kolika km, i koja registracija"
  - "obriši rezervaciju i unesi 30000 km"

V3 router and Unified responder are designed for ONE intent. Without
detection, second intent silently drops or LLM picks the wrong tool.

Heuristic detection: Croatian conjunction ("i", "te", "pa", "plus")
+ multiple action verbs (pokaži/obriši/rezerviraj/unesi/...) within the
same query. False positive on "pokaži km i registraciju" (single read of
multiple fields) — handled by allowlist of fields-of-same-tool.

Returns MultiIntentResult; engine renders clarify message if detected
("Jednu po jednu — što prvo?").
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class MultiIntentResult:
    detected: bool
    parts: list[str] = field(default_factory=list)
    clarify_message: str = ""


# Action verbs (Croatian — covers GET/POST/PUT/PATCH/DELETE intents)
_ACTION_VERBS = {
    "read": [
        r"\bpokaži\b", r"\bpokazi\b", r"\bprikaži\b", r"\bprikazi\b",
        r"\bdaj\b", r"\breci\b", r"\bvidim\b", r"\bvidjet\b",
    ],
    "list": [
        r"\blist[ai]\b", r"\bpopis\b", r"\bsve\b",
    ],
    "create_reservation": [
        r"\brezerviraj\b", r"\brezervirat\b", r"\bbukiraj\b",
        r"\btrebam\s+(?:auto|vozilo)\b",
    ],
    "delete_cancel": [
        r"\botkaži\b", r"\botkazi\b", r"\bobriši\b", r"\bobrisi\b",
        r"\bukloni\b", r"\bizbriši\b", r"\bizbrisi\b", r"\bmakni\b",
    ],
    "create_mileage": [
        r"\bunesi\b", r"\bupiši\b", r"\bupisi\b", r"\bdodaj\b",
        r"\bevidentir\w*\b", r"\bstavi\b",
    ],
    "create_case": [
        r"\bprijavi\b", r"\budario\s+sam\b", r"\bpokvario\b",
    ],
    "send_email": [
        r"\bpošalji\s+email\b", r"\bposalji\s+mail\b", r"\bemail\s+\w+\b",
    ],
}

# Conjunction patterns indicating list of actions
_CONJUNCTION_PATTERNS = [
    re.compile(r"\b\s*,\s*(?:te|pa|plus|i)\s+", re.IGNORECASE),
    re.compile(r"\s+(?:te|pa|plus)\s+", re.IGNORECASE),
    # " i " is too broad — only flag when followed by an action-verb-shaped token
    re.compile(r"\s+i\s+(?=\w{4,})", re.IGNORECASE),
]


def detect(query: str) -> MultiIntentResult:
    """Detect multi-intent query. Returns MultiIntentResult.

    Algorithm:
      1. Find all action-verb matches in the query
      2. If >= 2 DIFFERENT verb categories AND a conjunction connects them
         → flag as multi-intent
      3. If only 1 category (e.g. "pokaži km i registraciju") → single intent
    """
    if not query or not query.strip():
        return MultiIntentResult(detected=False)

    text = query.strip()
    matched_categories: set[str] = set()
    for category, patterns in _ACTION_VERBS.items():
        for pat in patterns:
            if re.search(pat, text, re.IGNORECASE):
                matched_categories.add(category)
                break

    if len(matched_categories) < 2:
        return MultiIntentResult(detected=False)

    # Conjunction must connect them
    has_conjunction = any(p.search(text) for p in _CONJUNCTION_PATTERNS)
    if not has_conjunction:
        return MultiIntentResult(detected=False)

    # Try to split — best effort
    parts = []
    for sep in [r",\s*(?:te|pa|plus|i)\s+", r"\s+(?:te|pa|plus|i)\s+"]:
        split = re.split(sep, text, flags=re.IGNORECASE)
        if len(split) >= 2:
            parts = [p.strip() for p in split if p.strip()]
            break

    clarify = (
        "Razumio sam dvije stvari u poruci. Jednu po jednu — što prvo?\n"
    )
    if parts:
        for i, p in enumerate(parts[:3], 1):
            clarify += f"{i}. {p[:60]}\n"
    clarify += "\nReci samo prvo, drugo radimo nakon."

    return MultiIntentResult(
        detected=True,
        parts=parts,
        clarify_message=clarify,
    )
