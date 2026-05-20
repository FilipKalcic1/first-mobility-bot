"""
Admin token verification — single source of truth for ADMIN_TOKEN_1..4 checks.

Used by /webhook/whatsapp/debug, /webhook/whatsapp/routing-log, and any
other admin-protected surface. Uses constant-time comparison and
iterates the full token set so timing reveals nothing about which token
matched.

Tokens come from environment variables ADMIN_TOKEN_1..4 (whatever subset
is configured). The endpoint must be denied if zero tokens are configured —
silently allowing access in that case would defeat the auth boundary.

Each token slot has an optional sibling ADMIN_TOKEN_<N>_USER env that
labels who holds that token. `verify_admin_token` returns the label on
match so callers can log per-admin audit trails. When the label is unset
the slot identifier (e.g. "slot_1") is returned instead — never the token.
"""
from __future__ import annotations

import hmac
import os
from typing import Optional

# Token slot count — kept aligned with the sealed-secrets template.
_TOKEN_SLOTS = 4


def load_admin_tokens() -> list[tuple[bytes, str]]:
    """Read every configured (token, user-label) pair.

    Returns an empty list when nothing is configured; the caller MUST treat
    that as "deny all" rather than as "anything goes".
    """
    pairs: list[tuple[bytes, str]] = []
    for i in range(1, _TOKEN_SLOTS + 1):
        env_token = os.environ.get(f"ADMIN_TOKEN_{i}")
        if env_token:
            user = os.environ.get(f"ADMIN_TOKEN_{i}_USER") or f"slot_{i}"
            pairs.append((env_token.encode(), user))
    return pairs


def verify_admin_token(presented: Optional[str]) -> Optional[str]:
    """Return user label on match, None on miss.

    Constant-time comparison and full-set iteration: timing of a positive
    match is independent of which slot held the matching token, and a deny
    decision takes the same path as an allow decision.

    `presented=None` and `presented=""` both fail. The truthy/falsy contract
    keeps existing `if not verify_admin_token(...)` callers working.
    """
    candidate = (presented or "").encode()
    stored = load_admin_tokens()
    if not stored:
        return None

    matched: Optional[str] = None
    for token, user in stored:
        # Run all comparisons; capture match without early exit so timing
        # is independent of which slot matched.
        if hmac.compare_digest(candidate, token):
            matched = user
    return matched


def extract_bearer_or_query_token(request) -> str:
    """Pull a token from `?token=` or `Authorization: Bearer ...`.

    Helper for FastAPI request objects. Returns empty string if neither is
    present so callers can pass the result straight to verify_admin_token().
    """
    token = request.query_params.get("token", "") if hasattr(request, "query_params") else ""
    if not token and hasattr(request, "headers"):
        auth = request.headers.get("authorization", "") or ""
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
    return token
