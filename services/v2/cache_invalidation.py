"""Cache invalidation hook — bust identity cache on backend webhook.

Critique #8: admin updates driver vehicle in backend, bot has 5-min
stale cache. Without an invalidation channel, bot returns wrong data
until TTL expires.

This module owns:
1. The pure invalidation event flow (`InvalidationEvent`,
   `handle_invalidation_event`, `verify_signature`).
2. The framework-agnostic HTTP handler core (`process_request`)
   used by main.py's FastAPI binding. Lives here (not in main.py) so
   unit tests can exercise the security path without pulling FastAPI
   into the test environment.

Hardening:
  - 1KB payload cap BEFORE HMAC compute (hash-DoS defense)
  - In-memory IP rate limit (10 req / 60s / IP) — blocks brute-force
    HMAC discovery
  - Constant-time HMAC compare (`hmac.compare_digest`)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional, Any

logger = logging.getLogger(__name__)

# HTTP route hardening constants
PAYLOAD_CAP_BYTES = 1024
IP_RATE_LIMIT = 10
IP_RATE_WINDOW_S = 60

# Per-IP sliding-window bucket. Module-level so a single process shares
# state across all requests; cleared in tests via `reset_ip_bucket()`.
_IP_BUCKET: dict[str, deque] = {}


def reset_ip_bucket() -> None:
    """Wipe the per-IP rate-limit state. Tests use this between cases."""
    _IP_BUCKET.clear()


def _check_ip(ip: str, *, now: Optional[float] = None) -> bool:
    """Return True if request from `ip` is within budget; False if blocked.

    Side effect: records this request in the bucket on success.
    """
    now = time.time() if now is None else now
    cutoff = now - IP_RATE_WINDOW_S
    bucket = _IP_BUCKET.get(ip)
    if bucket is None:
        bucket = deque(maxlen=IP_RATE_LIMIT * 2)
        _IP_BUCKET[ip] = bucket
    while bucket and bucket[0] < cutoff:
        bucket.popleft()
    if len(bucket) >= IP_RATE_LIMIT:
        return False
    bucket.append(now)
    return True


@dataclass(frozen=True)
class InvalidationEvent:
    phone: str
    reasons: list[str]  # ["vehicle_change", "role_change", ...]
    timestamp: float
    source: str = "backend"


@dataclass(frozen=True)
class InvalidationResult:
    success: bool
    cleared_stores: list[str]
    errors: list[str]


def verify_signature(
    payload_bytes: bytes,
    signature: str,
    shared_secret: str,
) -> bool:
    """HMAC-SHA256 signature verification. Backend signs payload with
    pre-shared secret; bot verifies before clearing caches."""
    if not signature or not shared_secret:
        return False
    expected = hmac.new(
        shared_secret.encode("utf-8"),
        payload_bytes,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


async def handle_invalidation_event(
    event: InvalidationEvent,
    *,
    identity_context=None,
    conversation_history_store=None,
    pending_mut_store=None,
    pending_clarify_store=None,
    fact_snapshot_store=None,
) -> InvalidationResult:
    """Process invalidation event by clearing relevant stores.

    Each store optional — None means engine doesn't have it wired.
    Returns InvalidationResult with per-store outcome.
    """
    cleared: list[str] = []
    errors: list[str] = []

    if identity_context is not None:
        try:
            if hasattr(identity_context, "clear"):
                await identity_context.clear(event.phone)
                cleared.append("identity_cache")
        except Exception as e:  # noqa: BLE001
            errors.append(f"identity:{type(e).__name__}")

    # Conversation history: clear only if role-change reason present
    # (otherwise keep — user benefits from continuity)
    if (
        conversation_history_store is not None
        and any(r in event.reasons for r in ("role_change", "termination"))
    ):
        try:
            await conversation_history_store.clear(event.phone)
            cleared.append("conversation_history")
        except Exception as e:  # noqa: BLE001
            errors.append(f"conv_hist:{type(e).__name__}")

    # Pending mut/clarify: ALWAYS clear on any invalidation event
    # (stale state could mutate using outdated identity)
    for name, store in [
        ("pending_mutation", pending_mut_store),
        ("pending_clarify", pending_clarify_store),
        ("fact_snapshot", fact_snapshot_store),
    ]:
        if store is None:
            continue
        try:
            await store.clear(event.phone)
            cleared.append(name)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}:{type(e).__name__}")

    logger.info(
        "cache invalidation phone=%s reasons=%s cleared=%s errors=%s",
        event.phone[-4:], event.reasons, cleared, errors,
    )
    return InvalidationResult(
        success=not errors,
        cleared_stores=cleared,
        errors=errors,
    )


async def process_request(
    *,
    body: bytes,
    signature: str,
    declared_content_length: Optional[str],
    client_ip: str,
    secret: str,
    stores: dict,
) -> tuple[int, dict]:
    """Framework-agnostic HTTP handler core for /admin/cache-invalidate.

    Returns `(status_code, body_dict)`. main.py's FastAPI route is a
    thin wrapper over this. Tests call it directly — no FastAPI needed.
    """
    if not _check_ip(client_ip):
        return 429, {"detail": "rate limit"}

    if declared_content_length is not None:
        try:
            if int(declared_content_length) > PAYLOAD_CAP_BYTES:
                return 413, {"detail": "payload too large"}
        except (ValueError, OverflowError):
            return 400, {"detail": "invalid content-length"}

    if len(body) > PAYLOAD_CAP_BYTES:
        return 413, {"detail": "payload too large"}

    if not secret:
        return 503, {"detail": "cache invalidation not configured"}

    if not verify_signature(body, signature, secret):
        return 401, {"detail": "invalid signature"}

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return 400, {"detail": "invalid json"}

    phone = (payload.get("phone") or "").strip()
    reasons = payload.get("reasons") or []
    if not phone or not isinstance(reasons, list):
        return 400, {"detail": "missing phone or reasons"}

    event = InvalidationEvent(
        phone=phone,
        reasons=[str(r) for r in reasons[:10]],
        timestamp=float(payload.get("timestamp") or time.time()),
    )
    result = await handle_invalidation_event(event, **stores)
    return 200, {
        "ok": result.success,
        "cleared": result.cleared_stores,
        "errors": result.errors,
    }
