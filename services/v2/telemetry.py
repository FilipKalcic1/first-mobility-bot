"""Production telemetry — single event shape for routing decisions.

One structured event per user-facing routing decision. Calibrated for
**frequent debugger** usage pattern: when you open Log Analytics multiple
times a day during a Damir support session, the single-event view must
give the full picture (competitors + clarify + turn_number) without
joining across multiple records.

Schema (11 fields, single event type — no `kind` discriminator):

    {
      "ts": "2026-05-08T17:30:00Z",
      "tenant_id": "...",
      "correlation_id": "uuid",     # one per webhook, NOT chained
      "turn_number": 1,             # within a conversation
      "query_scrubbed": "...",      # PII-scrubbed user message
      "is_negation": false,         # query == "nije točno"
      "tool_picked": "operation_id|null",
      "confidence": 0.85,
      "competitors": ["op2", "op3"],
      "clarify": {"options": [...], "picked": "id"|null} | null,
      "error": "string|null",       # null = success
      "latency_ms": 1234
    }

Privacy: caller is responsible for PII scrubbing. By the time a message
reaches V2Engine.process_message, services/v2/pii_scrubber has already
redacted phone numbers, IDs, etc. from the raw text.

Sinks:
- Production: StdoutJsonSink → Container App stdout → Log Analytics (90d)
                + RedisSink (live Damir endpoint, last 1000 events)
- Dev: StdoutJsonSink + BufferedAsyncFileSink (logs/v2_telemetry-*.jsonl)
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import hashlib
import json
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Damir-export Redis key — admin endpoint at webhook_simple.py:763 reads
# this list. Bounded to 1k entries (LTRIM) with 30-day TTL so the key
# disappears if telemetry stops writing (no stale snapshot).
ROUTING_LOG_KEY = "routing:accuracy_log"
ROUTING_LOG_MAX_ENTRIES = 999  # LTRIM 0..999 keeps 1000 newest
ROUTING_LOG_TTL_SECONDS = 30 * 24 * 3600

# BufferedAsyncFileSink defaults — flush every 100 events OR every 1s.
BUFFER_MAX_SIZE = 10_000
BUFFER_FLUSH_THRESHOLD = 100
BUFFER_FLUSH_INTERVAL_SECONDS = 1.0


# ---------------------------------------------------------------------------
# Context vars for per-request fields (correlation_id, turn_number)
# ---------------------------------------------------------------------------

# These are set once at the entry of V2Engine.process_message and read
# wherever a TelemetryEvent is constructed. Prevents threading them
# through 25+ call sites as explicit arguments.
_correlation_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_v2_correlation_id", default=""
)
_turn_number_var: contextvars.ContextVar[int] = contextvars.ContextVar(
    "_v2_turn_number", default=0
)


def set_request_context(*, correlation_id: str, turn_number: int) -> None:
    """Record the current request's correlation_id + turn_number.

    Called once at V2Engine.process_message entry. Subsequent
    TelemetryEvent(...) constructions read from these vars when the
    fields aren't explicitly provided.
    """
    _correlation_id_var.set(correlation_id)
    _turn_number_var.set(turn_number)


def hash_phone(phone: str) -> str:
    """Legacy helper retained for callers that still hash phones.

    Not part of the canonical event shape (phone_hash field dropped),
    but kept exported because tests and a few legacy code paths import
    it. Returns "" for empty input.
    """
    if not phone:
        return ""
    return hashlib.sha256(phone.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Canonical event
# ---------------------------------------------------------------------------

@dataclass
class TelemetryEvent:
    """Single routing-decision event.

    All fields have safe defaults so partial events (e.g. early input
    block before identity resolution) still construct cleanly.
    """
    # Identity
    tenant_id: str = ""
    correlation_id: str = ""
    turn_number: int = 0

    # User input
    query_scrubbed: str = ""
    is_negation: bool = False

    # Router decision
    tool_picked: Optional[str] = None
    confidence: float = 0.0
    competitors: list[str] = field(default_factory=list)

    # UX flow
    clarify: Optional[dict] = None

    # Execution
    error: Optional[str] = None
    latency_ms: int = 0

    def __post_init__(self) -> None:
        # Auto-fill from context vars if caller didn't supply explicitly.
        # This is the central propagation point for correlation_id +
        # turn_number — set once at process_message entry, picked up by
        # every TelemetryEvent constructed during that request.
        if not self.correlation_id:
            self.correlation_id = _correlation_id_var.get()
        if not self.turn_number:
            self.turn_number = _turn_number_var.get()

    def to_record(self) -> dict[str, Any]:
        rec = asdict(self)
        rec["ts"] = datetime.now(timezone.utc).isoformat()
        # Drop None / empty-string / empty-collection fields. Keep
        # zeros and False — they are meaningful (turn_number=0 means
        # "outside a request", latency_ms=0 means "not measured").
        return {k: v for k, v in rec.items() if v not in (None, "", [], {})}


# ---------------------------------------------------------------------------
# Sinks
# ---------------------------------------------------------------------------

class TelemetrySink:
    """Pluggable backend protocol — subclass and override write_record()."""

    async def write_record(self, record: dict[str, Any]) -> None:  # pragma: no cover
        raise NotImplementedError

    async def aclose(self) -> None:
        """Optional shutdown hook. Default is no-op; sinks with buffers
        override this to flush before the process exits."""
        return


class FileSink(TelemetrySink):
    """JSONL sink with daily rotation. Synchronous open/write per event
    under a global lock. Kept for backward compatibility with the
    `test_file_sink_writes_jsonl` test; not exposed via from_env().
    """

    def __init__(self, log_dir: str = "logs"):
        self._dir = Path(log_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    def _path_for_today(self) -> Path:
        return self._dir / f"v2_telemetry-{datetime.now(timezone.utc).date()}.jsonl"

    async def write_record(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False)
        async with self._lock:
            with self._path_for_today().open("a", encoding="utf-8") as f:
                f.write(line + "\n")


class BufferedAsyncFileSink(TelemetrySink):
    """JSONL sink with in-memory buffer + background flush task.

    For dev workflow. Production uses StdoutJsonSink → Log Analytics.
    Buffer (deque, maxlen=10k) drains every 100 events OR every 1s,
    whichever first. Drains via asyncio.to_thread so blocking I/O
    runs off the event loop.
    """

    def __init__(
        self,
        log_dir: str = "logs",
        max_buffer: int = BUFFER_MAX_SIZE,
        flush_threshold: int = BUFFER_FLUSH_THRESHOLD,
        flush_interval: float = BUFFER_FLUSH_INTERVAL_SECONDS,
    ):
        self._dir = Path(log_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._buffer: deque[str] = deque(maxlen=max_buffer)
        self._max_buffer = max_buffer
        self._flush_threshold = flush_threshold
        self._flush_interval = flush_interval
        self._dropped_count = 0
        self._dropped_warning_emitted = False
        self._task: Optional[asyncio.Task] = None
        self._stopping = False
        self._wake = asyncio.Event()
        self._flush_lock = asyncio.Lock()

    def _path_for_today(self) -> Path:
        return self._dir / f"v2_telemetry-{datetime.now(timezone.utc).date()}.jsonl"

    async def write_record(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False)
        if len(self._buffer) >= self._max_buffer:
            self._dropped_count += 1
            if not self._dropped_warning_emitted:
                logger.warning(
                    "telemetry buffer full (%d events) — dropping oldest",
                    self._max_buffer,
                )
                self._dropped_warning_emitted = True
        self._buffer.append(line)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._flush_loop())
        if len(self._buffer) >= self._flush_threshold:
            self._wake.set()

    async def _flush_loop(self) -> None:
        try:
            while not self._stopping:
                try:
                    await asyncio.wait_for(
                        self._wake.wait(), timeout=self._flush_interval,
                    )
                except asyncio.TimeoutError:
                    pass
                self._wake.clear()
                await self._flush_once()
        except asyncio.CancelledError:
            await self._flush_once()
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("telemetry flush loop crashed: %s", e)

    async def _flush_once(self) -> None:
        async with self._flush_lock:
            if not self._buffer:
                return
            batch: list[str] = []
            while self._buffer:
                batch.append(self._buffer.popleft())
            path = self._path_for_today()
            try:
                await asyncio.to_thread(self._write_batch_sync, path, batch)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "telemetry batch write failed (%d events lost): %s",
                    len(batch), e,
                )

    @staticmethod
    def _write_batch_sync(path: Path, batch: list[str]) -> None:
        with path.open("a", encoding="utf-8") as f:
            for line in batch:
                f.write(line + "\n")

    @property
    def dropped_count(self) -> int:
        return self._dropped_count

    async def aclose(self) -> None:
        self._stopping = True
        self._wake.set()
        if self._task is not None and not self._task.done():
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
                with contextlib.suppress(Exception):
                    await self._task
        await self._flush_once()


class RedisSink(TelemetrySink):
    """Mirror events into a bounded Redis list for the live Damir endpoint.

    Single pipeline per write (one round-trip):
        LPUSH routing:accuracy_log <json>
        LTRIM routing:accuracy_log 0 999
        EXPIRE routing:accuracy_log <30 days>

    The webhook_simple.py /whatsapp/routing-log endpoint reads the same
    key (LRANGE 0..N). Bounded list = no unbounded memory in Redis.

    Failure mode: exceptions propagate to the TelemetryLogger.log()
    inline loop, which logs and continues to other sinks.
    """

    def __init__(
        self,
        redis_client,
        key: str = ROUTING_LOG_KEY,
        max_entries: int = ROUTING_LOG_MAX_ENTRIES,
        ttl_seconds: int = ROUTING_LOG_TTL_SECONDS,
    ):
        self._redis = redis_client
        self._key = key
        self._max_entries = max_entries
        self._ttl = ttl_seconds

    async def write_record(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False)
        pipe = self._redis.pipeline()
        pipe.lpush(self._key, line)
        pipe.ltrim(self._key, 0, self._max_entries)
        pipe.expire(self._key, self._ttl)
        await pipe.execute()


class StdoutJsonSink(TelemetrySink):
    """Emit one JSON line per event to stdout.

    Production primary store: Container App auto-ships stdout to
    Log Analytics, where 90d retention + KQL queries replace any
    custom aggregator. No buffering — Container App agent is
    optimized for stdout volume; we don't second-guess it.

    `ensure_ascii=False` is mandatory so Croatian characters survive
    round-trip into KQL parse_json().
    """

    def __init__(self, stream=None):
        # Lazy import — keeps the module loadable in environments
        # where `python-json-logger` isn't installed (e.g. tests run
        # before the requirements bump). Falls back to plain json.dumps().
        self._stream = stream  # if None, use sys.stdout at write time

    async def write_record(self, record: dict[str, Any]) -> None:
        # asyncio.to_thread keeps stdout writes off the event loop
        # under high concurrency. Container App stdout is line-buffered,
        # so a partial flush mid-line would be an issue — avoid it by
        # writing the full line in one syscall.
        await asyncio.to_thread(self._write_sync, record)

    def _write_sync(self, record: dict[str, Any]) -> None:
        import sys
        line = json.dumps(record, ensure_ascii=False, default=str)
        stream = self._stream or sys.stdout
        stream.write(line + "\n")
        stream.flush()


class NullSink(TelemetrySink):
    """No-op sink — used when telemetry is disabled."""

    async def write_record(self, record: dict[str, Any]) -> None:
        return


# ---------------------------------------------------------------------------
# Logger facade
# ---------------------------------------------------------------------------

class TelemetryLogger:
    """High-level facade. Async-safe, failure-isolated.

    Holds a list of sinks and fans out each event. A single sink raising
    is logged once per (process, sink type) and silently skipped for
    that event — telemetry is observability, not correctness.
    """

    def __init__(self, sinks, enabled: bool = True):
        # Accept either a single TelemetrySink (legacy) or a list.
        if isinstance(sinks, TelemetrySink):
            self._sinks = [sinks]
        else:
            self._sinks = list(sinks)
        self._enabled = enabled
        self._failed_types: set[str] = set()

    @classmethod
    def from_env(cls, *, redis_client=None) -> "TelemetryLogger":
        """Build a logger from environment configuration.

        Env vars:
          V2_TELEMETRY=0|1               — master switch (default 1)
          V2_TELEMETRY_BACKEND=stdout|stdout+redis|file|both
                                          — sink selection (default stdout+redis)
          V2_TELEMETRY_DIR=<path>        — file sink output dir (default "logs")

        Production default (`stdout+redis`): StdoutJsonSink → Log Analytics
        + RedisSink → live Damir endpoint. RedisSink is silently skipped if
        no redis_client is passed (e.g. in tests).

        Dev (`file` or `both`): adds BufferedAsyncFileSink for local logs.
        """
        if os.environ.get("V2_TELEMETRY", "1") != "1":
            return cls([NullSink()], enabled=False)
        backend = os.environ.get("V2_TELEMETRY_BACKEND", "stdout+redis").lower()
        log_dir = os.environ.get("V2_TELEMETRY_DIR", "logs")
        sinks: list[TelemetrySink] = []
        if backend in ("stdout", "stdout+redis", "both"):
            sinks.append(StdoutJsonSink())
        if backend in ("redis", "stdout+redis", "both") and redis_client is not None:
            sinks.append(RedisSink(redis_client))
        if backend in ("file", "both"):
            sinks.append(BufferedAsyncFileSink(log_dir=log_dir))
        if not sinks:
            # Defensive: if backend asked redis-only and no client, fall
            # back to stdout so we don't silently lose all telemetry.
            sinks.append(StdoutJsonSink())
        return cls(sinks, enabled=True)

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def log(self, event: TelemetryEvent) -> None:
        """Fan-out one event to all sinks. Per-sink failures are
        isolated — broken sink doesn't affect siblings or the caller."""
        if not self._enabled:
            return
        record = event.to_record()
        for sink in self._sinks:
            try:
                await sink.write_record(record)
            except Exception as e:  # noqa: BLE001
                sink_type = type(sink).__name__
                if sink_type not in self._failed_types:
                    logger.warning(
                        "telemetry sink %s failed (further muted): %s",
                        sink_type, e,
                    )
                    self._failed_types.add(sink_type)

    async def aclose(self) -> None:
        """Drain buffered sinks. Safe to call on disabled loggers."""
        if not self._enabled:
            return
        for sink in self._sinks:
            with contextlib.suppress(Exception):
                await sink.aclose()

    @contextlib.asynccontextmanager
    async def timed(self, **fields):
        """Context manager that auto-records latency_ms.

        Usage:
            async with tel.timed(query_scrubbed=q) as evt:
                evt.tool_picked = tool_id
                ... do work ...
                evt.error = None  # or "..." on failure
            # logged on exit
        """
        evt = TelemetryEvent(**fields)
        t0 = time.time()
        try:
            yield evt
        finally:
            evt.latency_ms = int(round((time.time() - t0) * 1000))
            await self.log(evt)
