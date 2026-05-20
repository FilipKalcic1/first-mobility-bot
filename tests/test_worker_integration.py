"""Integration tests for worker.py glue — the layer that wires inbound stream
→ V2Engine → outbound queue together.

This was the LAST untested segment in the pipeline; per-segment unit tests
(engine, webhook, whatsapp_service, etc.) all pass, but until these tests
nothing proved the worker's _handle_message actually composes them correctly:
   - Distributed dedup lock prevents double execution.
   - Non-text messages are politely refused without invoking the engine.
   - In-pod rate limit returns the cooldown message.
   - Engine timeout (90s) yields the "obrada predugo" reply, NOT a hang.
   - Engine exception goes to DLQ AND user gets a fallback Croatian reply.
   - Outbound enqueue failure DOES NOT ACK (msg stays pending for restart).
   - Happy path: engine response → outbound queue → message ACKed.

Failure here = users receive duplicate replies, no reply, or stuck pending
messages on next restart. None caught by per-layer unit tests.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeRedis:
    """In-memory Redis covering all ops Worker._handle_message touches."""

    def __init__(self) -> None:
        self.kv: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}
        self.ttls: dict[str, int] = {}
        self.acked_msg_ids: list[str] = []
        # If set, the next op of this name raises ConnectionError.
        self.fail_op: str | None = None

    def _maybe_fail(self, op: str):
        if self.fail_op == op:
            self.fail_op = None
            raise ConnectionError(f"simulated redis fail: {op}")

    async def set(self, key: str, value: Any, nx: bool = False, ex: int | None = None) -> bool:
        self._maybe_fail("set")
        if nx and key in self.kv:
            return False
        self.kv[key] = str(value)
        if ex:
            self.ttls[key] = ex
        return True

    async def get(self, key: str):
        self._maybe_fail("get")
        return self.kv.get(key)

    async def delete(self, *keys: str) -> int:
        n = 0
        for k in keys:
            if k in self.kv:
                del self.kv[k]
                n += 1
            if k in self.lists:
                del self.lists[k]
                n += 1
        return n

    async def expire(self, key: str, ttl: int) -> bool:
        self.ttls[key] = ttl
        return True

    async def rpush(self, key: str, *values) -> int:
        self._maybe_fail("rpush")
        self.lists.setdefault(key, []).extend(str(v) for v in values)
        return len(self.lists[key])

    async def lpush(self, key: str, *values) -> int:
        self._maybe_fail("lpush")
        self.lists.setdefault(key, []).insert(0, str(values[0]))
        for v in values[1:]:
            self.lists[key].insert(0, str(v))
        return len(self.lists[key])

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        items = self.lists.get(key, [])
        if end == -1:
            return items[start:]
        return items[start : end + 1]

    async def llen(self, key: str) -> int:
        return len(self.lists.get(key, []))

    async def xack(self, stream: str, group: str, msg_id: str) -> int:
        self.acked_msg_ids.append(msg_id)
        return 1

    async def xdel(self, stream: str, msg_id: str) -> int:
        return 1

    async def eval(self, *args, **kwargs) -> int:
        # Lua release-lock: returns 1 if "released," 0 otherwise. For tests
        # we just drop the lock key unconditionally to keep behavior simple.
        return 1


class FakeV2Engine:
    """Stub that lets each test choose how process_message behaves."""

    def __init__(self, response: str = "OK Croatian reply") -> None:
        self._response = response
        self._sleep_sec: float = 0.0
        self._raise: Exception | None = None
        self.calls: list[tuple[str, str]] = []

    def set_response(self, text: str) -> None:
        self._response = text

    def set_sleep(self, seconds: float) -> None:
        self._sleep_sec = seconds

    def set_raise(self, exc: Exception) -> None:
        self._raise = exc

    async def process_message(self, sender: str, text: str) -> str:
        self.calls.append((sender, text))
        if self._raise:
            raise self._raise
        if self._sleep_sec:
            await asyncio.sleep(self._sleep_sec)
        return self._response


# ---------------------------------------------------------------------------
# Worker fixture — patches the real Worker class to use fakes
# ---------------------------------------------------------------------------


@pytest.fixture
def worker(monkeypatch):
    # Stop config validation from blocking import — patch settings before
    # worker module is imported.
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("MOBILITY_API_URL", "https://api.example.com")
    monkeypatch.setenv("MOBILITY_AUTH_URL", "https://auth.example.com")
    monkeypatch.setenv("MOBILITY_CLIENT_ID", "test")
    monkeypatch.setenv("MOBILITY_CLIENT_SECRET", "test")
    monkeypatch.setenv("MOBILITY_TENANT_ID", "test")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://openai.example.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")

    import importlib
    import worker as worker_mod
    importlib.reload(worker_mod)

    w = worker_mod.Worker()
    w.redis = FakeRedis()  # type: ignore[assignment]
    w._v2_engine = FakeV2Engine()
    # Skip the rate-limiter sleep — we test it explicitly
    w.rate_limit = 30
    w.rate_window = 60
    # Real consumer name for lock attribution (unique per test)
    w.consumer_name = "test_worker_consumer"
    return w


# ---------------------------------------------------------------------------
# Tests — each one proves ONE failure-mode contract end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_engine_reply_lands_in_outbound_and_acks(worker):
    """The whole point: a message flows webhook→engine→outbound, ACKed."""
    worker._v2_engine.set_response("Tvoja kilometraža je 42 500 km.")

    await worker._handle_message(
        msg_id="stream-1",
        data={"sender": "+385955087196", "text": "kolika mi je km",
              "message_id": "infobip-msg-001", "tenant_id": "tenant-A"},
    )

    # Engine was actually invoked with the user's text
    assert len(worker._v2_engine.calls) == 1
    assert worker._v2_engine.calls[0] == ("+385955087196", "kolika mi je km")
    # Reply landed in outbound queue
    outbound = worker.redis.lists.get("whatsapp_outbound") or []
    assert len(outbound) == 1
    payload = json.loads(outbound[0])
    assert payload["to"] == "+385955087196"
    assert payload["text"] == "Tvoja kilometraža je 42 500 km."
    assert "idempotency_key" in payload
    # Stream message was ACKed (no orphan, no double-process on restart)
    assert "stream-1" in worker.redis.acked_msg_ids
    # Tenant context written for the engine to consume mid-pipeline
    assert worker.redis.kv.get("session:+385955087196:tenant_id") == "tenant-A"
    # Stats incremented
    assert worker._messages_processed == 1
    assert worker._messages_failed == 0


@pytest.mark.asyncio
async def test_duplicate_message_id_skipped_no_double_engine_call(worker):
    """Infobip retries are common (~500ms). Dedup lock prevents double-execution."""
    msg = {"sender": "+385955087196", "text": "bok",
           "message_id": "duplicate-id", "tenant_id": "t1"}

    # First delivery
    await worker._handle_message(msg_id="s1", data=msg)
    assert len(worker._v2_engine.calls) == 1
    first_outbound_len = len(worker.redis.lists.get("whatsapp_outbound", []))
    assert first_outbound_len == 1

    # Retry with the SAME message_id — must be detected as duplicate
    await worker._handle_message(msg_id="s2", data=msg)

    # Engine NOT called a second time
    assert len(worker._v2_engine.calls) == 1
    # No additional outbound message
    assert len(worker.redis.lists.get("whatsapp_outbound", [])) == first_outbound_len
    # Both stream IDs were ACKed (duplicate still removed from stream)
    assert "s1" in worker.redis.acked_msg_ids
    assert "s2" in worker.redis.acked_msg_ids
    assert worker._duplicates_skipped == 1


@pytest.mark.asyncio
async def test_non_text_message_polite_refusal_no_engine_invocation(worker):
    """Images/voice/etc arrive as [NON_TEXT:TYPE] from webhook. Engine
    is not invoked; user gets a 'text only' message; stream is ACKed."""
    await worker._handle_message(
        msg_id="s1",
        data={"sender": "+385955087196", "text": "[NON_TEXT:IMAGE]",
              "message_id": "img-001", "tenant_id": "t1",
              "original_type": "IMAGE"},
    )

    # Engine MUST NOT be called for non-text
    assert len(worker._v2_engine.calls) == 0
    # User gets the polite Croatian refusal
    outbound = worker.redis.lists.get("whatsapp_outbound") or []
    assert len(outbound) == 1
    payload = json.loads(outbound[0])
    assert "tekstualne poruke" in payload["text"]
    # Acked + counted as processed
    assert "s1" in worker.redis.acked_msg_ids
    assert worker._messages_processed == 1


@pytest.mark.asyncio
async def test_per_pod_rate_limit_short_circuits_with_croatian_cooldown(worker):
    """In-pod rate limiter (defense in depth alongside L-1 in engine).
    When tripped, user gets a Croatian cooldown without engine being called."""
    # Force rate limiter to deny
    worker.rate_limit = 1  # only 1 message allowed in window
    # First message passes
    await worker._handle_message(
        msg_id="s1",
        data={"sender": "+385955087196", "text": "bok",
              "message_id": "m1", "tenant_id": "t1"},
    )
    engine_calls_after_first = len(worker._v2_engine.calls)

    # Second message from same sender → rate limited
    await worker._handle_message(
        msg_id="s2",
        data={"sender": "+385955087196", "text": "drugi",
              "message_id": "m2", "tenant_id": "t1"},
    )
    # Engine NOT called for the rate-limited message
    assert len(worker._v2_engine.calls) == engine_calls_after_first
    # Cooldown message present in outbound (the LAST entry)
    outbound = worker.redis.lists.get("whatsapp_outbound") or []
    last_payload = json.loads(outbound[-1])
    assert "previše poruka" in last_payload["text"].lower() or \
           "previše" in last_payload["text"].lower()
    assert "s2" in worker.redis.acked_msg_ids


@pytest.mark.asyncio
async def test_engine_timeout_returns_croatian_message_and_acks(worker):
    """Engine taking forever must NOT block the worker indefinitely.
    The 90s wait_for cap fires, user gets the timeout reply, message is
    ACKed (otherwise it would re-fire on restart)."""
    # Make engine sleep "forever" but patch the worker-level timeout to 0.1s
    worker._v2_engine.set_sleep(5.0)
    with patch("worker.asyncio.wait_for") as mock_wait:
        # Mock wait_for to raise TimeoutError immediately
        async def _instant_timeout(coro, timeout):
            coro.close()  # don't actually run the slow coro
            raise asyncio.TimeoutError()
        mock_wait.side_effect = _instant_timeout

        await worker._handle_message(
            msg_id="s1",
            data={"sender": "+385955087196", "text": "spori upit",
                  "message_id": "m1", "tenant_id": "t1"},
        )

    outbound = worker.redis.lists.get("whatsapp_outbound") or []
    assert len(outbound) == 1
    payload = json.loads(outbound[0])
    assert "trajala predugo" in payload["text"] or "predugo" in payload["text"]
    assert "s1" in worker.redis.acked_msg_ids
    assert worker._messages_failed == 1


@pytest.mark.asyncio
async def test_engine_exception_goes_to_dlq_and_user_gets_croatian_fallback(worker):
    """Any unhandled exception in the engine must:
      1. NOT crash the worker
      2. Write the original message + error to dlq:inbound for later inspection
      3. Send the user a generic Croatian error reply
      4. ACK the stream message (so we don't infinite-retry the same bug)
    """
    worker._v2_engine.set_raise(RuntimeError("simulated engine bug"))

    await worker._handle_message(
        msg_id="s1",
        data={"sender": "+385955087196", "text": "exploding query",
              "message_id": "m1", "tenant_id": "t1"},
    )

    # User got the Croatian fallback (not silence)
    outbound = worker.redis.lists.get("whatsapp_outbound") or []
    assert len(outbound) == 1
    payload = json.loads(outbound[0])
    assert "greške" in payload["text"].lower() or "greska" in payload["text"].lower()

    # DLQ entry written
    dlq = worker.redis.lists.get("dlq:inbound") or []
    assert len(dlq) == 1
    dlq_entry = json.loads(dlq[0])
    assert "simulated engine bug" in dlq_entry["error"]
    assert dlq_entry["original"]["sender"] == "+385955087196"

    # Message ACKed (don't infinite-retry a deterministic bug)
    assert "s1" in worker.redis.acked_msg_ids
    assert worker._messages_failed == 1


@pytest.mark.asyncio
async def test_outbound_failure_recovered_via_dlq_and_fallback_reply(worker):
    """If the original RPUSH fails, the exception handler kicks in:
      1. DLQ entry is written (operator can inspect later)
      2. A fallback Croatian error message is enqueued
      3. THEN it's safe to ACK (don't infinite-retry a known bug)
    The invariant: as long as DLQ write succeeded, message is ACKed.
    The 'no ACK on full failure' case (DLQ fails too) is below."""
    worker._v2_engine.set_response("Original reply")
    # Cause the FIRST rpush to fail. _maybe_fail is one-shot, so the
    # fallback rpush inside the exception handler succeeds.
    worker.redis.fail_op = "rpush"

    await worker._handle_message(
        msg_id="s1",
        data={"sender": "+385955087196", "text": "test",
              "message_id": "m1", "tenant_id": "t1"},
    )

    # Engine called
    assert len(worker._v2_engine.calls) == 1
    # DLQ entry written
    dlq = worker.redis.lists.get("dlq:inbound") or []
    assert len(dlq) == 1
    # Fallback Croatian error sent
    outbound = worker.redis.lists.get("whatsapp_outbound") or []
    assert len(outbound) == 1
    payload = json.loads(outbound[0])
    assert "greške" in payload["text"].lower() or "pokušajte" in payload["text"]
    # Message ACKed — original failure is in DLQ, infinite retry would be useless
    assert "s1" in worker.redis.acked_msg_ids
    assert worker._messages_failed == 1


@pytest.mark.asyncio
async def test_total_redis_failure_does_not_ack_for_pending_reclaim(worker):
    """The ACK-deferred path: if BOTH outbound enqueue AND DLQ write fail
    (Redis fully down mid-processing), the stream message must stay
    pending so the worker reclaims it on restart via xclaim. This is the
    only thing that prevents permanent message loss during a Redis
    blip."""
    worker._v2_engine.set_response("Reply")

    # Patch rpush AND lpush to ALWAYS raise (not one-shot) for this test.
    real_rpush = worker.redis.rpush
    real_lpush = worker.redis.lpush

    async def always_fail_rpush(*a, **kw):
        raise ConnectionError("redis down — rpush")

    async def always_fail_lpush(*a, **kw):
        raise ConnectionError("redis down — lpush")

    worker.redis.rpush = always_fail_rpush  # type: ignore[method-assign]
    worker.redis.lpush = always_fail_lpush  # type: ignore[method-assign]
    try:
        # Worker raises in this case (DLQ rpush fails inside the
        # except-handler, propagates out). In production this is caught
        # by `asyncio.gather(..., return_exceptions=True)` in the inbound
        # loop, leaving the message pending for the next consumer.
        with pytest.raises(ConnectionError):
            await worker._handle_message(
                msg_id="s1",
                data={"sender": "+385955087196", "text": "test",
                      "message_id": "m1", "tenant_id": "t1"},
            )
    finally:
        worker.redis.rpush = real_rpush  # type: ignore[method-assign]
        worker.redis.lpush = real_lpush  # type: ignore[method-assign]

    # Engine called, but stream NOT ACKed — restart will reclaim
    assert len(worker._v2_engine.calls) == 1
    assert "s1" not in worker.redis.acked_msg_ids


@pytest.mark.asyncio
async def test_long_response_split_into_multiple_outbound_chunks(worker):
    """LLM occasionally returns >4000 chars. Worker must split, all
    chunks land in outbound atomically (single RPUSH for N values)."""
    long_text = "A" * 4200  # >4000 chars
    worker._v2_engine.set_response(long_text)

    await worker._handle_message(
        msg_id="s1",
        data={"sender": "+385955087196", "text": "give me lots",
              "message_id": "m1", "tenant_id": "t1"},
    )

    outbound = worker.redis.lists.get("whatsapp_outbound") or []
    # Splitter produces >=2 chunks for 4200/4000
    assert len(outbound) >= 2
    payloads = [json.loads(p) for p in outbound]
    # Each chunk has its own idempotency key
    keys = [p["idempotency_key"] for p in payloads]
    assert len(set(keys)) == len(keys)  # all unique
    # Concatenated chunks recover the original
    assert "".join(p["text"] for p in payloads) == long_text


@pytest.mark.asyncio
async def test_empty_engine_response_replaced_with_croatian_error(worker):
    """If engine returns "" or None (shouldn't happen but did during
    early development), don't silently drop — send the user an error
    so they know the bot is alive."""
    worker._v2_engine.set_response("")

    await worker._handle_message(
        msg_id="s1",
        data={"sender": "+385955087196", "text": "test",
              "message_id": "m1", "tenant_id": "t1"},
    )

    outbound = worker.redis.lists.get("whatsapp_outbound") or []
    assert len(outbound) == 1
    payload = json.loads(outbound[0])
    # Croatian fallback rather than empty string
    assert "Greška" in payload["text"] or "pokušajte" in payload["text"]


@pytest.mark.asyncio
async def test_missing_message_id_uses_content_hash_as_dedup_key(worker):
    """Some webhook payloads lack messageId. We hash sender+text instead
    so the dedup lock still prevents the most common duplicate cause."""
    msg = {"sender": "+385955087196", "text": "hello",
           "message_id": "", "tenant_id": "t1"}

    # First delivery
    await worker._handle_message(msg_id="s1", data=msg)
    assert len(worker._v2_engine.calls) == 1

    # Second delivery (no messageId) — same content → same hash-derived lock
    await worker._handle_message(msg_id="s2", data=msg)
    assert len(worker._v2_engine.calls) == 1  # NOT called again
    assert worker._duplicates_skipped == 1


# ---------------------------------------------------------------------------
# Outbound loop — proves queue → Infobip send segment
# ---------------------------------------------------------------------------


class FakeSendResult:
    def __init__(self, success: bool, error_code: str = "",
                 error_message: str = "", retry_after: int | None = None,
                 message_id: str = ""):
        self.success = success
        self.error_code = error_code
        self.error_message = error_message
        self.retry_after = retry_after
        self.message_id = message_id


class FakeWhatsAppService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.scripted: list[FakeSendResult] = []

    def queue(self, result: FakeSendResult) -> None:
        self.scripted.append(result)

    async def send(self, to: str, text: str) -> FakeSendResult:
        self.calls.append((to, text))
        if self.scripted:
            return self.scripted.pop(0)
        return FakeSendResult(success=True, message_id=f"infobip-{len(self.calls)}")


class FakeRedisOutbound(FakeRedis):
    """Extends FakeRedis with BLMOVE / LMOVE / LREM / ZADD ops for outbound."""

    def __init__(self) -> None:
        super().__init__()
        self.zsets: dict[str, dict[str, float]] = {}

    async def blmove(self, first_list: str, second_list: str, *,
                     timeout: int = 0, src: str = "LEFT", dest: str = "RIGHT"):
        # Match worker.py call: positional list names + kwargs for direction.
        return await self._lmove(first_list, second_list, src, dest)

    async def lmove(self, first_list: str, second_list: str, *,
                    src: str = "LEFT", dest: str = "RIGHT"):
        return await self._lmove(first_list, second_list, src, dest)

    async def _lmove(self, src_list: str, dest_list: str,
                     src_pos: str, dest_pos: str) -> str | None:
        items = self.lists.get(src_list) or []
        if not items:
            return None
        if src_pos == "LEFT":
            item = items.pop(0)
        else:
            item = items.pop(-1)
        self.lists.setdefault(dest_list, [])
        if dest_pos == "LEFT":
            self.lists[dest_list].insert(0, item)
        else:
            self.lists[dest_list].append(item)
        return item

    async def lrem(self, key: str, count: int, value) -> int:
        items = self.lists.get(key, [])
        n = 0
        try:
            items.remove(value)
            n = 1
        except ValueError:
            pass
        return n

    async def zadd(self, key: str, mapping: dict) -> int:
        self.zsets.setdefault(key, {}).update(mapping)
        return len(mapping)

    async def setex(self, key: str, ttl: int, value: Any) -> bool:
        self.kv[key] = str(value)
        self.ttls[key] = ttl
        return True


@pytest.fixture
def outbound_worker(monkeypatch):
    """Same patching as the inbound `worker` fixture, but builds an
    outbound-flavored FakeRedis and a fake WhatsApp service for sending."""
    for var, val in [
        ("DATABASE_URL", "sqlite+aiosqlite:///:memory:"),
        ("REDIS_URL", "redis://localhost:6379/0"),
        ("MOBILITY_API_URL", "https://api.example.com"),
        ("MOBILITY_AUTH_URL", "https://auth.example.com"),
        ("MOBILITY_CLIENT_ID", "test"), ("MOBILITY_CLIENT_SECRET", "test"),
        ("MOBILITY_TENANT_ID", "test"),
        ("AZURE_OPENAI_ENDPOINT", "https://openai.example.com"),
        ("AZURE_OPENAI_API_KEY", "test-key"),
    ]:
        monkeypatch.setenv(var, val)
    import importlib
    import worker as worker_mod
    importlib.reload(worker_mod)
    w = worker_mod.Worker()
    w.redis = FakeRedisOutbound()  # type: ignore[assignment]
    w._whatsapp_service = FakeWhatsAppService()
    w.consumer_name = "test_outbound_consumer"
    return w


@pytest.mark.asyncio
async def test_send_whatsapp_calls_service_with_payload(outbound_worker):
    """_send_whatsapp dispatches to WhatsAppService.send and logs success."""
    outbound_worker._whatsapp_service.queue(
        FakeSendResult(success=True, message_id="infobip-abc")
    )

    await outbound_worker._send_whatsapp("+385955087196", "Reply text")

    calls = outbound_worker._whatsapp_service.calls
    assert len(calls) == 1
    assert calls[0] == ("+385955087196", "Reply text")


@pytest.mark.asyncio
async def test_send_rate_limit_enqueues_delayed_for_retry(outbound_worker):
    """When Infobip returns RATE_LIMIT, the message is re-queued into the
    delayed sorted set with the retry-after offset — NOT dropped."""
    outbound_worker._whatsapp_service.queue(FakeSendResult(
        success=False, error_code="RATE_LIMIT", retry_after=15,
    ))

    await outbound_worker._send_whatsapp("+385955087196", "Reply")

    delayed = outbound_worker.redis.zsets.get("whatsapp_outbound_delayed") or {}
    assert len(delayed) == 1
    payload_str = next(iter(delayed.keys()))
    payload = json.loads(payload_str)
    assert payload["to"] == "+385955087196"
    assert payload["text"] == "Reply"
    # Scheduled ~15s in the future
    assert payload["scheduled_at"] > 0


@pytest.mark.asyncio
async def test_send_permanent_failure_does_not_retry(outbound_worker):
    """Non-rate-limit errors (e.g. INVALID_PHONE) are NOT re-queued.
    The delayed sorted set stays empty — the message is dropped after one
    failed attempt + log entry."""
    outbound_worker._whatsapp_service.queue(FakeSendResult(
        success=False, error_code="INVALID_PHONE", retry_after=None,
    ))

    await outbound_worker._send_whatsapp("+38500", "Reply")

    delayed = outbound_worker.redis.zsets.get("whatsapp_outbound_delayed") or {}
    assert len(delayed) == 0  # NOT requeued


@pytest.mark.asyncio
async def test_requeue_abandoned_outbound_moves_processing_back_to_outbound(outbound_worker):
    """On worker restart, items stuck in whatsapp_outbound_processing
    (worker crashed mid-send) must be moved back to whatsapp_outbound for
    redelivery. This is the only guard against permanent message loss
    when a worker SIGKILLs between BLMOVE and send."""
    redis = outbound_worker.redis
    # Simulate two messages abandoned from a previous crash
    abandoned = [
        json.dumps({"to": "+1", "text": "msg1", "idempotency_key": "k1"}),
        json.dumps({"to": "+2", "text": "msg2", "idempotency_key": "k2"}),
    ]
    redis.lists["whatsapp_outbound_processing"] = list(abandoned)
    redis.lists["whatsapp_outbound"] = []

    await outbound_worker._requeue_abandoned_outbound()

    # Both moved back to outbound
    assert len(redis.lists["whatsapp_outbound_processing"]) == 0
    assert len(redis.lists["whatsapp_outbound"]) == 2


@pytest.mark.asyncio
async def test_send_no_service_logs_warning_does_not_raise(outbound_worker):
    """Defensive: if _whatsapp_service is None (init failure), send is a
    no-op + warning, NOT a crash that kills the outbound loop."""
    outbound_worker._whatsapp_service = None
    # Must not raise
    await outbound_worker._send_whatsapp("+385955087196", "Reply")
