"""Negation handler — detects "ne, otkaži" / "nemoj rezervirati" mid-flow.

Real users sometimes change their mind:
  - "rezerviraj auto za sutra"
  - bot: confirm prompt
  - user: "ne, otkaži, nemoj rezervirati"  ← this is the issue

Without negation handling, "ne, otkaži, nemoj rezervirati" might be
interpreted by:
1. The pending mutation parser as "Ne" → cancel ✓ (works)
2. BUT THEN if no pending state, "nemoj rezervirati" goes to V3 router
   which might pick post_VehicleCalendar (because "rezerviraj" in text)
   even though user said NEMOJ.

This module flags strong negation in the query and either:
- Maps to "cancel pending state" intent
- Returns clarify message asking what user wants

Called as L0.85 between identity and special intents. Safe — only acts
on UNAMBIGUOUS Croatian negations to avoid false positives.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class NegationResult:
    detected: bool
    confidence: float = 0.0
    response: str = ""


# Strong negation phrases (Croatian + English)
_NEGATION_PATTERNS = [
    re.compile(r"\bne(?:moj|mojte)\s+(?:rezervir\w+|otkaž\w+|otkaz\w+|obriš\w+|obris\w+|izbriš\w+|izbris\w+|unes\w+|stav\w+|dodaj|šalji|salji|pošalji|posalji)", re.IGNORECASE),
    re.compile(r"\bnem(?:oj|ojte|ojmo)\s+(?:to|ovo|ono)\b", re.IGNORECASE),
    re.compile(r"\bne(?:,|!|\.)\s*(?:otkaži|otkazi|odustaj|odustani|stop|stoj)", re.IGNORECASE),
    re.compile(r"\bodustajem\b", re.IGNORECASE),
    re.compile(r"\bodustani\b", re.IGNORECASE),
    re.compile(r"\bzaboravi\s+(?:na\s+)?(?:to|ovo|ono|rezervaciju|km|prijavu)", re.IGNORECASE),
    re.compile(r"\bcancel\s+(?:that|this|it)\b", re.IGNORECASE),
    re.compile(r"\bnevermind\b", re.IGNORECASE),
    re.compile(r"\bnema\s+veze\b", re.IGNORECASE),
]


_RESPONSE = (
    "U redu, odustao sam. Ako trebaš nešto drugo, samo reci."
)


def detect(query: str) -> NegationResult:
    """Detect strong negation. Returns NegationResult."""
    if not query or not query.strip():
        return NegationResult(detected=False)

    text = query.strip()
    for pat in _NEGATION_PATTERNS:
        m = pat.search(text)
        if m:
            return NegationResult(
                detected=True,
                confidence=0.85,  # high — patterns are unambiguous
                response=_RESPONSE,
            )

    return NegationResult(detected=False)
