"""GDPR + handover audit log store — Redis-backed, per-tenant lists.

Why this exists: services/v2/special_intents.py builds SpecialIntentMatch
with side_effects=({"action": "audit_log_gdpr_delete"},) for GDPR_DELETE,
GDPR_EXPORT, and queue_human_handover for HANDOVER. The engine returns
the Croatian "queued in 48h" message to the user but historically never
processed the side_effects tuple — the audit promise was a lie.

This module is the consumer. The engine calls record() before returning
the user-facing message. Operators read pending entries via the
/admin/gdpr-requests endpoint and act manually within the 48h legal
window (GDPR Articles 17 & 20).

Storage:
  - GDPR delete/export → key `gdpr:requests:{tenant_id}` (LPUSH, 90d TTL)
  - Human handover     → key `handover:requests:{tenant_id}` (LPUSH, 30d TTL)
  - Both bounded to last 500 entries via LTRIM to prevent unbounded growth.

Failure mode: Redis transient failure logs a loud WARNING but does NOT
raise. The user still receives the queued-confirmation message, so they
don't believe the request silently failed. Operators will notice missing
audit entries via the admin endpoint health check.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)


GDPR_KEY_PREFIX = "gdpr:requests:"
HANDOVER_KEY_PREFIX = "handover:requests:"
GDPR_TTL_SECONDS = 90 * 24 * 3600   # legal retention window
HANDOVER_TTL_SECONDS = 30 * 24 * 3600
MAX_ENTRIES = 500  # LTRIM index — keeps Redis bounded


class GdprAuditStore:
    """Append-only audit log keyed by tenant. Never raises."""

    def __init__(self, redis_client):
        self._redis = redis_client

    async def record_gdpr_request(
        self,
        *,
        action: str,           # "audit_log_gdpr_delete" | "audit_log_gdpr_export"
        tenant_id: Optional[str],
        phone: str,
        person_id: Optional[str],
        correlation_id: str,
    ) -> bool:
        """LPUSH the request, LTRIM to MAX_ENTRIES, set TTL. Returns True on success."""
        if not tenant_id:
            # GDPR right-to-be-forgotten on an unknown phone is meaningless —
            # there's nothing to forget. Log and skip.
            logger.warning("gdpr: %s for unknown tenant (phone=%s) — skipping audit",
                           action, phone)
            return False
        entry = {
            "action": action,
            "phone": phone,
            "person_id": person_id,
            "tenant_id": tenant_id,
            "correlation_id": correlation_id,
            "requested_at": time.time(),
        }
        return await self._append(GDPR_KEY_PREFIX + tenant_id, entry, GDPR_TTL_SECONDS)

    async def record_handover_request(
        self,
        *,
        tenant_id: Optional[str],
        phone: str,
        person_id: Optional[str],
        correlation_id: str,
    ) -> bool:
        if not tenant_id:
            logger.warning("handover: unknown tenant (phone=%s) — skipping audit", phone)
            return False
        entry = {
            "action": "queue_human_handover",
            "phone": phone,
            "person_id": person_id,
            "tenant_id": tenant_id,
            "correlation_id": correlation_id,
            "requested_at": time.time(),
        }
        return await self._append(
            HANDOVER_KEY_PREFIX + tenant_id, entry, HANDOVER_TTL_SECONDS,
        )

    async def list_gdpr_requests(
        self, tenant_id: str, limit: int = 100,
    ) -> list[dict]:
        """Read latest N entries for a tenant. Returns [] on Redis miss/error."""
        return await self._read(GDPR_KEY_PREFIX + tenant_id, limit)

    async def list_handover_requests(
        self, tenant_id: str, limit: int = 100,
    ) -> list[dict]:
        return await self._read(HANDOVER_KEY_PREFIX + tenant_id, limit)

    async def _append(self, key: str, entry: dict, ttl: int) -> bool:
        try:
            payload = json.dumps(entry, ensure_ascii=False)
            await self._redis.lpush(key, payload)
            await self._redis.ltrim(key, 0, MAX_ENTRIES - 1)
            await self._redis.expire(key, ttl)
            return True
        except Exception as e:  # noqa: BLE001 — audit MUST NOT block user
            logger.warning("gdpr_audit append failed (%s): %s — entry=%s",
                           type(e).__name__, e, entry)
            return False

    async def _read(self, key: str, limit: int) -> list[dict]:
        try:
            raw = await self._redis.lrange(key, 0, limit - 1)
        except Exception as e:  # noqa: BLE001
            logger.warning("gdpr_audit read failed (%s): %s", type(e).__name__, e)
            return []
        out: list[dict] = []
        for r in raw or []:
            try:
                out.append(json.loads(r))
            except (ValueError, TypeError):
                continue
        return out
