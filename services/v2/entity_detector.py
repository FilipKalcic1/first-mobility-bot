"""Detect concrete entities (currently: registration plate) in a Croatian query.

Used to decide whether the user is asking about ONE specific vehicle, so the
engine can confirm ("specifično za vozilo DA533?") and then filter the API call.

HIGH-CONFIDENCE only — regex for registration plates. Vague references ("moj
auto", a person's name) are NOT detected here. False positives are low-harm
because the engine ALWAYS confirms before applying a filter (Filip 2026-05-23).
"""
from __future__ import annotations

import re
from typing import Optional

# Croatian registration plates incl. Damir's fleet format (DA533, DA053F):
# 2 letters + 2-4 digits + 0-2 trailing letters, optionally hyphen-separated.
# NOTE: no whitespace separator on purpose — "za 30500 km" must NOT match as a
# plate "ZA30500". The hyphen handles official "ZG-1234-AB" style.
_PLATE_RE = re.compile(r"\b([A-ZČŠŽ]{2})-?(\d{2,4})-?([A-Z]{0,2})\b")


def detect_registration(query: str) -> Optional[str]:
    """Return the normalized plate (upper, no separators, e.g. 'DA533') if the
    query clearly names one, else None.

    The engine confirms with the user before acting on this, so a rare false
    positive just produces a confirm the user declines."""
    if not query:
        return None
    m = _PLATE_RE.search(query.upper())
    if not m:
        return None
    return f"{m.group(1)}{m.group(2)}{m.group(3)}"
