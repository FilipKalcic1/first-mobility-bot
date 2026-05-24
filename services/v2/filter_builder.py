"""Build MobilityOne `Filter` query strings from detected entities, safely.

MobilityOne list endpoints (get_Vehicles, get_Master, ...) filter via a single
`Filter` query param with syntax `Field(operator)Value`, multiple joined by
" and " (e.g. `LicencePlate(=)DA533 and Status(=)aktivan`).

Values are sanitized (allowlist + SQL-keyword blocklist + length cap) to prevent
injection — adapted from the pre-simplify services/filter_builder.py.
"""
from __future__ import annotations

import re

# Allowlist: alphanumerics (incl. Croatian), space, hyphen, underscore, dot, @, +.
_ALLOWED_CHARS = re.compile(r"[^a-zA-Z0-9čćžšđČĆŽŠĐ\s\-_.@+]")

# Blocklist: SQL comment sequence + dangerous keywords (defense in depth).
_DANGEROUS = re.compile(
    r"(--|\b(exec|execute|insert|update|delete|drop|truncate|union|select|"
    r"from|where|alter|create|grant|revoke|xp_)\b)",
    re.IGNORECASE,
)


def sanitize_filter_value(value) -> str:
    """Strip anything outside the allowlist + remove SQL keywords + cap length."""
    if not isinstance(value, str):
        value = str(value)
    v = _ALLOWED_CHARS.sub("", value)
    v = _DANGEROUS.sub("", v)
    return v.strip()[:500]


def build_filter_clause(field: str, value, operator: str = "(=)") -> str:
    """One `Field(operator)Value` clause with the value sanitized.

    Returns "" if the field is empty or the value sanitizes to nothing (caller
    should treat "" as "no filter")."""
    if not field:
        return ""
    safe = sanitize_filter_value(value)
    if not safe:
        return ""
    return f"{field}{operator}{safe}"


def combine_filters(*clauses: str) -> str:
    """Join non-empty clauses with ' and ' (MobilityOne multi-filter syntax)."""
    return " and ".join(c for c in clauses if c)
