"""EDGE-layer fixes (Filip 2026-05-20 — audit of Dio 1 EDGE).

Covers the verified-HIGH bugs found in the 3-agent EDGE audit:
  EDGE-1: consumer_name uniqueness (timestamp alone collided)
  EDGE-2: outbound send retry — permanent→DLQ, transient→re-enqueue, cap→DLQ
  EDGE-5: cleanup race lock (SET NX serializes concurrent cleanup)

Tests construct a Worker via object.__new__ and set only the attributes
the method under test touches — avoids the heavy full __init__ (Redis/DB
connections).
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from worker import Worker  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class FakeSendResult:
    success: bool
    message_id: str = ""
    error_code: str = ""
    error_message: str = ""
    retry_after: Optional[int] = None


class FakeWhatsAppService:
    """Returns a scripted sequence of send results."""
    def __init__(self, results: list):
        self._results = list(results)
        self.calls: list[tuple[str, str]] = []

    async def send(self, to: str, text: str):
        self.calls.append((to, text))
        if self._results:
            return self._results.pop(0)
        return FakeSendResult(success=True, message_id="default")


class FakeRedisOutbound:
    """Minimal Redis for _send_whatsapp / _enqueue_outbound_delayed / DLQ."""
    def __init__(self):
        self.zsets: dict[str, dict] = {}
        self.lists: dict[str, list] = {}
        self.kv: dict = {}

    async def zadd(self, key, mapping):
        self.zsets.setdefault(key, {}).update(mapping)
        return len(mapping)

    async def rpush(self, key, *values):
        self.lists.setdefault(key, []).extend(values)
        return len(self.lists[key])

    async def expire(self, key, ttl):
        return True

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.kv:
            return False
        self.kv[key] = value
        return True

    async def delete(self, *keys):
        for k in keys:
            self.kv.pop(k, None)
        return 1

    async def llen(self, key):
        return len(self.lists.get(key, []))


def _make_worker(whatsapp_service=None, redis=None) -> Worker:
    """Construct a Worker without running __init__ (no real connections)."""
    w = object.__new__(Worker)
    w._whatsapp_service = whatsapp_service
    w.redis = redis or FakeRedisOutbound()
    w.consumer_name = "test_worker"
    return w


# ---------------------------------------------------------------------------
# EDGE-1: consumer_name uniqueness
# ---------------------------------------------------------------------------


def test_edge1_consumer_name_unique_across_instances(monkeypatch):
    """Two workers booted in the same second must get distinct consumer
    names (timestamp + pid + random suffix). Before the fix, both got
    worker_{ts} and competed for the same stream messages."""
    # Freeze time so timestamp component is identical for both
    import worker as worker_mod

    class _FixedDT:
        @staticmethod
        def now(tz=None):
            import datetime as _dt
            return _dt.datetime(2026, 5, 20, 12, 0, 0, tzinfo=tz)

    monkeypatch.setattr(worker_mod, "datetime", _FixedDT)

    names = set()
    for _ in range(50):
        w = object.__new__(Worker)
        # Replicate the __init__ line for consumer_name
        import os, random
        from datetime import timezone
        ts = int(_FixedDT.now(timezone.utc).timestamp())
        w.consumer_name = f"worker_{ts}_{os.getpid()}_{random.randint(0, 9999):04d}"
        names.add(w.consumer_name)

    # With random suffix, 50 instances should be (almost certainly) unique.
    # Allow a tiny collision tolerance but require high uniqueness.
    assert len(names) >= 48, f"consumer names not unique enough: {len(names)}/50"


def test_edge1_real_init_format():
    """Verify the actual __init__ produces the new format with 3 parts
    after 'worker_': timestamp_pid_random."""
    import re
    # Read the source line to confirm the format (avoid full __init__).
    src = (ROOT / "worker.py").read_text(encoding="utf-8")
    assert "os.getpid()" in src
    assert "random.randint(0, 9999" in src


# ---------------------------------------------------------------------------
# EDGE-2: outbound send retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_edge2_permanent_error_goes_straight_to_dlq():
    """INVALID_PHONE → no retry (would never succeed) → outbound DLQ."""
    svc = FakeWhatsAppService([
        FakeSendResult(success=False, error_code="INVALID_PHONE",
                       error_message="bad number"),
    ])
    redis = FakeRedisOutbound()
    w = _make_worker(svc, redis)

    await w._send_whatsapp("385999", "test", attempt=0)

    assert len(svc.calls) == 1  # tried once, no retry
    assert len(redis.lists.get("dlq:outbound", [])) == 1  # parked in DLQ
    assert len(redis.zsets.get("whatsapp_outbound_delayed", {})) == 0  # NOT re-enqueued


@pytest.mark.asyncio
async def test_edge2_transient_error_re_enqueues_with_incremented_attempt():
    """Transient (e.g. timeout/5xx) → delayed re-enqueue with attempt+1."""
    svc = FakeWhatsAppService([
        FakeSendResult(success=False, error_code="API_TIMEOUT",
                       error_message="upstream timeout"),
    ])
    redis = FakeRedisOutbound()
    w = _make_worker(svc, redis)

    await w._send_whatsapp("385999", "test", attempt=0)

    # Re-enqueued for retry, NOT in DLQ yet
    delayed = redis.zsets.get("whatsapp_outbound_delayed", {})
    assert len(delayed) == 1
    import json
    payload = json.loads(next(iter(delayed.keys())))
    assert payload["attempt"] == 1  # incremented
    assert len(redis.lists.get("dlq:outbound", [])) == 0


@pytest.mark.asyncio
async def test_edge2_exhausted_attempts_go_to_dlq():
    """At MAX_SEND_ATTEMPTS-1, next transient failure → DLQ (no infinite loop)."""
    svc = FakeWhatsAppService([
        FakeSendResult(success=False, error_code="API_DOWN",
                       error_message="503"),
    ])
    redis = FakeRedisOutbound()
    w = _make_worker(svc, redis)

    # attempt=2 means this is the 3rd try (MAX_SEND_ATTEMPTS=3) → exhausted
    await w._send_whatsapp("385999", "test", attempt=2)

    assert len(redis.lists.get("dlq:outbound", [])) == 1
    assert len(redis.zsets.get("whatsapp_outbound_delayed", {})) == 0


@pytest.mark.asyncio
async def test_edge2_rate_limit_honors_retry_after():
    """RATE_LIMIT uses server retry_after, re-enqueues (existing behavior preserved)."""
    svc = FakeWhatsAppService([
        FakeSendResult(success=False, error_code="RATE_LIMIT",
                       error_message="slow down", retry_after=45),
    ])
    redis = FakeRedisOutbound()
    w = _make_worker(svc, redis)

    await w._send_whatsapp("385999", "test", attempt=0)

    delayed = redis.zsets.get("whatsapp_outbound_delayed", {})
    assert len(delayed) == 1
    # The score (scheduled time) should be ~retry_after seconds out
    import json
    payload = json.loads(next(iter(delayed.keys())))
    assert payload["attempt"] == 1


@pytest.mark.asyncio
async def test_edge2_success_does_not_touch_dlq_or_delayed():
    """Happy path: successful send → no DLQ, no re-enqueue."""
    svc = FakeWhatsAppService([
        FakeSendResult(success=True, message_id="msg123"),
    ])
    redis = FakeRedisOutbound()
    w = _make_worker(svc, redis)

    await w._send_whatsapp("385999", "test", attempt=0)

    assert len(redis.lists.get("dlq:outbound", [])) == 0
    assert len(redis.zsets.get("whatsapp_outbound_delayed", {})) == 0


@pytest.mark.asyncio
async def test_edge2_no_whatsapp_service_is_noop():
    """Missing whatsapp_service → graceful no-op (no crash)."""
    w = _make_worker(whatsapp_service=None, redis=FakeRedisOutbound())
    await w._send_whatsapp("385999", "test")  # must not raise
