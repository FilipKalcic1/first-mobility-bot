"""Reference resolver — handles follow-up queries with anaphora.

Real users send follow-up messages like:
  - "a što s onom drugom?"  (after seeing list of reservations)
  - "isto i za prošli mjesec"  (after seeing current month km)
  - "to obriši"  (after viewing single reservation)
  - "ono prvo"  (referring to top of previous list)

Without resolution, V3/Unified treats each turn fresh → loses context.
ConversationHistoryStore already persists prior turns; this module
inspects the CURRENT query for reference markers and rewrites it to be
self-contained using the previous turn's context.

Approach: deterministic Croatian reference markers + last-turn lookup.
No LLM call — runs as L0.9 lightweight pre-routing layer. If reference
is detected but cannot be resolved cleanly, returns clarify_question
asking user to restate.

Returns ReferenceResolution; engine substitutes the rewritten query
before passing to V3/Unified.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class ReferenceResolution:
    detected: bool
    rewritten_query: str = ""
    clarify_question: Optional[str] = None
    reason: str = ""


# Croatian reference markers in user msg
_REFERENCE_PATTERNS = [
    re.compile(r"\b(?:onaj|ona|ono|onu|oni|one|onima)\s+(?:prv[aoiu]|drug[aoiu]|treć[aoiu])\b", re.IGNORECASE),
    re.compile(r"\b(?:onaj|ona|ono|onu)\b\s*(?:[.,!?]|$)", re.IGNORECASE),
    re.compile(r"\b(?:to|toga|tome|time)\b\s+(?:obriši|otkaži|izbriši|ukloni|stavi)", re.IGNORECASE),
    re.compile(r"\b(?:isto|isto\s+to|isto\s+tako)\s+(?:i\s+)?za\b", re.IGNORECASE),
    re.compile(r"\bisto\s+(?:i\s+)?(?:za|s|sa)\b", re.IGNORECASE),
    re.compile(r"\ba\s+što\s+s(?:a)?\s+", re.IGNORECASE),
    re.compile(r"\ba\s+sto\s+s(?:a)?\s+", re.IGNORECASE),
    re.compile(r"\b(?:još|jos)\s+jedn[aou]\b", re.IGNORECASE),
    re.compile(r"\bi\s+to\s+isto\b", re.IGNORECASE),
]


def detect_reference(query: str) -> bool:
    """Quick check: does the query contain anaphoric markers?"""
    if not query:
        return False
    return any(p.search(query) for p in _REFERENCE_PATTERNS)


def resolve(query: str, recent_turns: list[dict]) -> ReferenceResolution:
    """Try to resolve anaphoric reference using prior bot turn.

    Strategy:
    - If no reference markers → ReferenceResolution(detected=False)
    - If reference detected but no recent_turns → ask clarification
    - If reference + last bot turn mentioned a tool/entity → rewrite
    - Otherwise → ask clarification
    """
    if not query or not query.strip():
        return ReferenceResolution(detected=False)

    if not detect_reference(query):
        return ReferenceResolution(detected=False)

    if not recent_turns:
        return ReferenceResolution(
            detected=True,
            clarify_question=(
                "Nisam siguran na što se 'to' / 'ono' odnosi — prva si "
                "poruka u ovoj sesiji. Možeš li detaljnije reći što ti treba?"
            ),
            reason="no_history",
        )

    # Find last bot turn with content
    last_bot = None
    last_user = None
    for t in reversed(recent_turns):
        if last_bot is None and t.get("bot"):
            last_bot = t["bot"]
        if last_user is None and t.get("user"):
            last_user = t["user"]
        if last_bot and last_user:
            break

    if not last_bot:
        return ReferenceResolution(
            detected=True,
            clarify_question=(
                "Možeš li detaljnije reći što ti treba? Nisam zapamtio "
                "prethodno o čemu smo pričali."
            ),
            reason="no_bot_turn",
        )

    # Heuristic rewrite — append the prior user query as context
    # so V3/Unified can re-resolve. This is intentionally conservative —
    # we don't try to be clever about substitution; we just give the
    # LLM both turns.
    rewritten = (
        f"{query.strip()} "
        f"(kontekst prethodne poruke: '{last_user[:140] if last_user else last_bot[:140]}')"
    )
    return ReferenceResolution(
        detected=True,
        rewritten_query=rewritten,
        reason="resolved_with_context",
    )
