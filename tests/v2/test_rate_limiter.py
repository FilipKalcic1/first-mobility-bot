"""Tests for L-1 RateLimiter."""
from __future__ import annotations

import pytest

from services.v2.rate_limiter import (
    COOLDOWN_PER_MIN,
    NORMAL_PER_MIN,
    SUSPICION_PER_HOUR,
    RateLimitDecision,
    RateLimiter,
)


class FakeRedis:
    def __init__(self):
        self.counts = {}
        self.expirations = {}
        self.broken = False

    async def incr(self, key):
        if self.broken:
            raise ConnectionError("redis down")
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    async def expire(self, key, ttl):
        if self.broken:
            raise ConnectionError("redis down")
        self.expirations[key] = ttl


@pytest.mark.asyncio
async def test_first_query_allowed():
    rl = RateLimiter(FakeRedis())
    decision = await rl.check("385955087196")
    assert decision.allowed is True
    assert decision.suspicious is False


@pytest.mark.asyncio
async def test_within_normal_threshold_allowed():
    redis = FakeRedis()
    rl = RateLimiter(redis)
    for _ in range(NORMAL_PER_MIN):
        decision = await rl.check("385955087196")
        assert decision.allowed is True


@pytest.mark.asyncio
async def test_above_cooldown_blocked():
    redis = FakeRedis()
    rl = RateLimiter(redis)
    last = None
    for _ in range(COOLDOWN_PER_MIN + 1):
        last = await rl.check("385955087196")
    assert last.allowed is False
    assert "minutu" in last.user_message.lower()


@pytest.mark.asyncio
async def test_above_hour_suspicion_flag_but_allowed():
    redis = FakeRedis()
    rl = RateLimiter(redis)
    # Pre-populate hour count above threshold
    redis.counts["v2:rl:h:385955087196"] = SUSPICION_PER_HOUR
    decision = await rl.check("385955087196")
    # First minute, so minute count = 1 → allowed
    # But hour now 501 → suspicious
    assert decision.allowed is True
    assert decision.suspicious is True


@pytest.mark.asyncio
async def test_redis_error_fails_open():
    """Redis outage must not block bot."""
    redis = FakeRedis()
    redis.broken = True
    rl = RateLimiter(redis)
    decision = await rl.check("385955087196")
    assert decision.allowed is True


@pytest.mark.asyncio
async def test_empty_phone_allowed():
    rl = RateLimiter(FakeRedis())
    decision = await rl.check("")
    assert decision.allowed is True


@pytest.mark.asyncio
async def test_separate_phones_separate_buckets():
    redis = FakeRedis()
    rl = RateLimiter(redis)
    for _ in range(COOLDOWN_PER_MIN + 1):
        await rl.check("phone-1")
    # Phone-1 blocked, phone-2 still fresh
    blocked = await rl.check("phone-1")
    fresh = await rl.check("phone-2")
    assert blocked.allowed is False
    assert fresh.allowed is True


@pytest.mark.asyncio
async def test_first_incr_sets_expire():
    redis = FakeRedis()
    rl = RateLimiter(redis)
    await rl.check("385955087196")
    assert "v2:rl:m:385955087196" in redis.expirations
    assert redis.expirations["v2:rl:m:385955087196"] == 60
    assert "v2:rl:h:385955087196" in redis.expirations
    assert redis.expirations["v2:rl:h:385955087196"] == 3600


def test_threshold_values_documented():
    assert NORMAL_PER_MIN < COOLDOWN_PER_MIN
    assert COOLDOWN_PER_MIN < SUSPICION_PER_HOUR
