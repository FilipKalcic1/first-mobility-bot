"""L-1 — Rate Limiter (per-phone Redis token bucket).

First gate before any LLM cost. Three thresholds:
  - 30 queries / minute → normal
  - 100 queries / minute → cooldown response, no LLM call
  - 500 queries / hour → suspicion log for Damir review

The token bucket is stored in Redis as two counters with TTL:
  v2:rl:m:{phone} — minute bucket, TTL 60s
  v2:rl:h:{phone} — hour bucket, TTL 3600s

Both INCR + EXPIRE in pipeline. Race-safe enough for single-region
deployment (production WhatsApp is single Infobip endpoint anyway).

API:
    rl = RateLimiter(redis_client)
    decision = await rl.check("385955087196")
    if not decision.allowed:
        return decision.user_message  # send to user, skip everything else
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# Thresholds.
NORMAL_PER_MIN = 30
COOLDOWN_PER_MIN = 100
SUSPICION_PER_HOUR = 500


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    suspicious: bool = False
    user_message: str = ""


class RateLimiter:
    """Per-phone Redis token bucket. Never raises on Redis errors —
    when Redis is down, falls open (allows the query) so a Redis
    outage doesn't block the bot. Trade-off: brief abuse window
    during Redis incident."""

    def __init__(self, redis_client):
        self._redis = redis_client

    @staticmethod
    def _min_key(phone: str) -> str:
        return f"v2:rl:m:{phone}"

    @staticmethod
    def _hour_key(phone: str) -> str:
        return f"v2:rl:h:{phone}"

    async def check(self, phone: str) -> RateLimitDecision:
        if not phone or not phone.strip():
            # No phone → allow (fallback). Caller will likely fail elsewhere.
            return RateLimitDecision(allowed=True)

        try:
            min_count = await self._incr_with_ttl(self._min_key(phone), 60)
            hour_count = await self._incr_with_ttl(self._hour_key(phone), 3600)
        except Exception as e:  # noqa: BLE001
            # Fail-open on Redis incident.
            logger.warning("rate limiter Redis error (fail-open): %s", e)
            return RateLimitDecision(allowed=True)

        if min_count > COOLDOWN_PER_MIN:
            return RateLimitDecision(
                allowed=False,
                user_message=(
                    "Previše brzih poruka. Pričekaj minutu pa pokušaj ponovo."
                ),
            )

        suspicious = hour_count > SUSPICION_PER_HOUR
        if suspicious:
            logger.warning(
                "rate limiter: suspicious volume phone=%s hour_count=%d",
                phone[-4:], hour_count,
            )

        return RateLimitDecision(
            allowed=True,
            suspicious=suspicious,
        )

    async def _incr_with_ttl(self, key: str, ttl_seconds: int) -> int:
        """INCR + EXPIRE atomic-ish via two ops. Acceptable race window
        (~ms) for a rate-limit primitive."""
        # Some Redis clients support pipeline; use plain ops for portability.
        new_value = await self._redis.incr(key)
        if new_value == 1:
            await self._redis.expire(key, ttl_seconds)
        return int(new_value)
