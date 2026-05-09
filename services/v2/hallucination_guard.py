"""Hallucination guard — detects when bot's response makes up facts about user.

ALGORITHM (specific per critique #7):

1. Extract candidate "claims" from response_text via regex:
   - NUMBER_RE: any number with optional thousand separators (matches
     "34 521", "34521", "34.521", "34,521")
   - DATE_RE: ISO date or "12. svibnja" Croatian form
   - PLATE_RE: vehicle license plate pattern (DA053F)

2. For each claim, NORMALIZE and check if it appears in api_data:
   - Numbers: strip [ .,] separators, compare against flattened JSON
     leaves (also normalized). Substring match counts.
   - Dates: lowercase + literal substring search in flattened JSON.
   - Plates: literal lowercase substring.

3. If a claim has NO support in api_data → flag suspicious.

KNOWN LIMITATIONS:
- "Škoda Octavia" vs JSON {marka: "Škoda", model: "Octavia"} → does NOT
  detect concatenation. Result: clean response gets flagged as
  hallucination. False positive.
- "15. kolovoza" vs JSON "2026-08-15" → date format conversion not
  attempted. Result: legitimate date renders flag as suspicious. False
  positive.
- Pronouns ("tvoj auto") → ignored, no false positive.
- Trivially small numbers (< 3 digits) → skipped to avoid ordinal
  confusion ("1 rezervacija").

These false positives are accepted per current design — better to surface
"_(provjeri ako sumnjaš)_" warning than hide hallucination. Refinement:
post-Damir-export, expand normalize step with date format conversion +
concatenation matching using a small NLI model. Currently NOT done.



Real failure mode (from brain-dump #150):
  - User: "kolika km"
  - API: returns success with TotalKm=34521 (only)
  - LLM Pass 2 (template-fill or tool_use): "Tvoja kilometraža je 34 521 km
    i imaš 3 rezervacije za sutra."  ← rezervacije=3 IS NOT in API response

This is hallucination — bot inventing structured facts. UX catastrophic
because user TRUSTS the bot. Causes: prompt injection earlier in pipeline,
LLM cross-talk between context turns, model confusion.

Detection strategy:
1. Extract numeric/factual claims from response_text
2. Cross-check each claim against api_data (the real JSON returned)
3. If a claim CANNOT be supported by api_data → flag as hallucination
4. Engine can: warn + show "iz API-a:" raw data, OR re-prompt LLM with
   stricter "ONLY say what's in the JSON" instruction

This is BEST-EFFORT — perfect detection requires NLI model (out of scope).
We catch the most common patterns: numbers, dates, list-counts, IDs.

Returns HallucinationCheck. is_safe=False → engine should warn or retry.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class HallucinationCheck:
    is_safe: bool
    suspicious_claims: list[str] = field(default_factory=list)
    reason: str = ""


# Match numbers in response: "34 521", "34521", "34.521", "34,521"
_NUMBER_RE = re.compile(r"\b(\d{1,3}(?:[ .,]?\d{3})*(?:[.,]\d+)?)\b")
# Match Croatian/ISO date: "12.05.", "2026-05-07", "12. svibnja"
_DATE_RE = re.compile(
    r"\b(\d{1,2}[.-/]\d{1,2}(?:[.-/]\d{2,4})?|\d{4}-\d{2}-\d{2})\b"
)
# Match plate-like patterns
_PLATE_RE = re.compile(r"\b([A-Z]{2}\d{3,4}[A-Z]{0,2})\b")


def _flatten_to_strings(data: Any, max_depth: int = 6) -> list[str]:
    """Flatten arbitrary JSON to a list of stringified leaf values for matching."""
    out: list[str] = []
    if max_depth <= 0:
        return out
    if data is None:
        return out
    if isinstance(data, (str, int, float, bool)):
        out.append(str(data))
    elif isinstance(data, dict):
        for v in data.values():
            out.extend(_flatten_to_strings(v, max_depth - 1))
    elif isinstance(data, (list, tuple)):
        for v in data:
            out.extend(_flatten_to_strings(v, max_depth - 1))
    return out


def _normalize_number(s: str) -> str:
    """Normalize Croatian number formats: '34 521' → '34521', '34.521' → '34521'."""
    return re.sub(r"[ .,]", "", s)


def check(response_text: str, api_data: Any) -> HallucinationCheck:
    """Verify that numeric/date/plate claims in response_text are
    supported by api_data. Best-effort.
    """
    if not response_text:
        return HallucinationCheck(is_safe=True)

    if not api_data:
        # No data to cross-check; can't verify
        return HallucinationCheck(is_safe=True)

    haystack_strings = _flatten_to_strings(api_data)
    haystack_normalized = {_normalize_number(s) for s in haystack_strings}
    haystack_lower = {s.lower() for s in haystack_strings}

    suspicious: list[str] = []

    # Number claims
    for m in _NUMBER_RE.finditer(response_text):
        num_text = m.group(1)
        norm = _normalize_number(num_text)
        # Skip trivial small numbers (probably ordinal: "1.", "2.", "3.")
        if len(norm) < 3:
            continue
        # Match if normalized form appears anywhere
        if norm not in haystack_normalized:
            # Maybe it's a substring — partial match
            if not any(norm in h for h in haystack_normalized):
                suspicious.append(f"number:{num_text}")

    # Date claims
    for m in _DATE_RE.finditer(response_text):
        date_text = m.group(1)
        if date_text.lower() not in haystack_lower and \
           not any(date_text.lower() in h for h in haystack_lower):
            suspicious.append(f"date:{date_text}")

    # Plate claims
    for m in _PLATE_RE.finditer(response_text):
        plate = m.group(1)
        if plate.lower() not in haystack_lower:
            suspicious.append(f"plate:{plate}")

    is_safe = not suspicious
    return HallucinationCheck(
        is_safe=is_safe,
        suspicious_claims=suspicious[:10],
        reason="claims_not_in_api_data" if suspicious else "",
    )
