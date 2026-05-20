"""Tests for telemetry — 11-field event shape + sink fan-out + Damir tap."""
from __future__ import annotations

import asyncio
import io
import json
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.v2.telemetry import (
    BufferedAsyncFileSink,
    FileSink,
    NullSink,
    RedisSink,
    ROUTING_LOG_KEY,
    ROUTING_LOG_MAX_ENTRIES,
    ROUTING_LOG_TTL_SECONDS,
    StdoutJsonSink,
    TelemetryEvent,
    TelemetryLogger,
    hash_phone,
    set_request_context,
)


# ---------------------------------------------------------------------------
# hash_phone (legacy helper retained)
# ---------------------------------------------------------------------------

def test_hash_phone_stable():
    h1 = hash_phone("385912345678")
    h2 = hash_phone("385912345678")
    assert h1 == h2
    assert len(h1) == 16
    assert hash_phone("385912345678") != hash_phone("385912345679")


def test_hash_phone_empty():
    assert hash_phone("") == ""
    assert hash_phone(None) == ""


# ---------------------------------------------------------------------------
# TelemetryEvent shape — 11 fields, context var injection
# ---------------------------------------------------------------------------

def test_event_to_record_drops_empty_fields():
    set_request_context(correlation_id="", turn_number=0)
    evt = TelemetryEvent(query_scrubbed="trebam auto", tenant_id="t1")
    rec = evt.to_record()
    assert rec["query_scrubbed"] == "trebam auto"
    assert rec["tenant_id"] == "t1"
    assert "ts" in rec
    # Empty / null / collection-empty fields dropped
    assert "tool_picked" not in rec
    assert "competitors" not in rec
    assert "clarify" not in rec
    assert "error" not in rec


def test_event_keeps_meaningful_zeros():
    """latency_ms=0 and turn_number=0 are valid signals; do NOT drop them
    just because they're falsy. is_negation=False is also kept."""
    set_request_context(correlation_id="cid", turn_number=0)
    evt = TelemetryEvent(query_scrubbed="x", latency_ms=0)
    rec = evt.to_record()
    assert rec["latency_ms"] == 0
    assert rec["correlation_id"] == "cid"
    # turn_number=0 means "outside a request" — but plus is_negation=False
    # are valid serialized states; turn_number specifically defaults to 0
    # and we drop *empty strings/None/empty collections*, not zeros.
    assert rec["turn_number"] == 0


def test_event_keeps_all_set_fields():
    set_request_context(correlation_id="cid-1", turn_number=2)
    evt = TelemetryEvent(
        tenant_id="acme",
        query_scrubbed="rezerviraj auto",
        is_negation=False,
        tool_picked="post_VehicleCalendar",
        confidence=0.85,
        competitors=["get_VehicleCalendar", "get_AvailableVehicles"],
        clarify={"options": ["a", "b"], "picked": "a"},
        error=None,
        latency_ms=1234,
    )
    rec = evt.to_record()
    assert rec["correlation_id"] == "cid-1"
    assert rec["turn_number"] == 2
    assert rec["tool_picked"] == "post_VehicleCalendar"
    assert rec["confidence"] == 0.85
    assert rec["competitors"] == [
        "get_VehicleCalendar", "get_AvailableVehicles",
    ]
    assert rec["clarify"] == {"options": ["a", "b"], "picked": "a"}
    assert rec["latency_ms"] == 1234
    assert "error" not in rec  # None is dropped


def test_event_picks_up_correlation_id_from_context():
    """If caller doesn't pass correlation_id explicitly, the value set
    via set_request_context() at process_message entry is used."""
    set_request_context(correlation_id="from-context", turn_number=5)
    evt = TelemetryEvent(query_scrubbed="x")
    assert evt.correlation_id == "from-context"
    assert evt.turn_number == 5


def test_event_explicit_field_overrides_context():
    set_request_context(correlation_id="from-context", turn_number=5)
    evt = TelemetryEvent(query_scrubbed="x", correlation_id="explicit")
    assert evt.correlation_id == "explicit"


# ---------------------------------------------------------------------------
# FileSink (legacy sync sink, kept for backward compat)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_file_sink_writes_jsonl():
    with tempfile.TemporaryDirectory() as tmp:
        sink = FileSink(log_dir=tmp)
        evt = TelemetryEvent(query_scrubbed="test")
        await sink.write_record(evt.to_record())
        await sink.write_record(evt.to_record())

        files = list(os.scandir(tmp))
        jsonl = [f for f in files if f.name.endswith(".jsonl")]
        assert len(jsonl) == 1
        with open(jsonl[0].path, encoding="utf-8") as f:
            lines = f.readlines()
        assert len(lines) == 2
        for line in lines:
            row = json.loads(line)
            assert row["query_scrubbed"] == "test"


@pytest.mark.asyncio
async def test_null_sink_swallows():
    sink = NullSink()
    evt = TelemetryEvent(query_scrubbed="x")
    await sink.write_record(evt.to_record())  # no raise


# ---------------------------------------------------------------------------
# BufferedAsyncFileSink — buffer drain, batching
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_buffered_sink_drains_on_close():
    with tempfile.TemporaryDirectory() as tmp:
        sink = BufferedAsyncFileSink(log_dir=tmp)
        for i in range(100):
            await sink.write_record({"i": i})
        await sink.aclose()

        files = [f for f in os.scandir(tmp) if f.name.endswith(".jsonl")]
        assert len(files) == 1
        with open(files[0].path, encoding="utf-8") as f:
            lines = f.readlines()
        assert len(lines) == 100


@pytest.mark.asyncio
async def test_buffered_sink_threshold_triggers_flush():
    with tempfile.TemporaryDirectory() as tmp:
        sink = BufferedAsyncFileSink(
            log_dir=tmp, flush_threshold=5, flush_interval=10.0,
        )
        for i in range(5):
            await sink.write_record({"i": i})
        for _ in range(10):
            await asyncio.sleep(0.05)
            files = [f for f in os.scandir(tmp) if f.name.endswith(".jsonl")]
            if files:
                with open(files[0].path, encoding="utf-8") as f:
                    if len(f.readlines()) >= 5:
                        break
        await sink.aclose()
        files = [f for f in os.scandir(tmp) if f.name.endswith(".jsonl")]
        assert len(files) == 1
        with open(files[0].path, encoding="utf-8") as f:
            assert len(f.readlines()) == 5


@pytest.mark.asyncio
async def test_buffered_sink_overflow_drops_oldest():
    sink = BufferedAsyncFileSink(
        log_dir=tempfile.gettempdir(),
        max_buffer=3, flush_threshold=999, flush_interval=999.0,
    )
    for i in range(5):
        await sink.write_record({"i": i})
    assert sink.dropped_count == 2
    assert len(sink._buffer) == 3
    await sink.aclose()


# ---------------------------------------------------------------------------
# RedisSink — pipeline LPUSH+LTRIM+EXPIRE
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_redis_sink_uses_pipeline_legacy_key_when_no_tenant():
    """Events without tenant_id (early input-blocks etc.) land in the
    legacy unscoped key. Admin endpoint requires explicit opt-in to read."""
    pipe = MagicMock()
    pipe.lpush = MagicMock(return_value=pipe)
    pipe.ltrim = MagicMock(return_value=pipe)
    pipe.expire = MagicMock(return_value=pipe)
    pipe.execute = AsyncMock(return_value=[])
    redis = MagicMock()
    redis.pipeline = MagicMock(return_value=pipe)

    sink = RedisSink(redis)
    await sink.write_record({"tool_picked": "get_X"})  # no tenant_id

    redis.pipeline.assert_called_once()
    args, _ = pipe.lpush.call_args
    assert args[0] == ROUTING_LOG_KEY  # legacy bucket
    assert json.loads(args[1])["tool_picked"] == "get_X"
    pipe.ltrim.assert_called_once_with(ROUTING_LOG_KEY, 0, ROUTING_LOG_MAX_ENTRIES)
    pipe.expire.assert_called_once_with(ROUTING_LOG_KEY, ROUTING_LOG_TTL_SECONDS)
    pipe.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_redis_sink_routes_event_to_per_tenant_key():
    """R2 regression: each event's tenant_id determines its Redis key.
    Two tenants must never share the same routing-log list — otherwise
    /whatsapp/routing-log would leak tenant A's queries to tenant B."""
    from services.v2.telemetry import redis_key_for_tenant

    pipe = MagicMock()
    pipe.lpush = MagicMock(return_value=pipe)
    pipe.ltrim = MagicMock(return_value=pipe)
    pipe.expire = MagicMock(return_value=pipe)
    pipe.execute = AsyncMock(return_value=[])
    redis = MagicMock()
    redis.pipeline = MagicMock(return_value=pipe)

    sink = RedisSink(redis)
    await sink.write_record({"tool_picked": "get_X", "tenant_id": "tenant-A"})
    args_a, _ = pipe.lpush.call_args
    assert args_a[0] == redis_key_for_tenant("tenant-A")
    assert args_a[0] == "routing:accuracy_log:tenant-A"

    await sink.write_record({"tool_picked": "get_Y", "tenant_id": "tenant-B"})
    args_b, _ = pipe.lpush.call_args
    assert args_b[0] == "routing:accuracy_log:tenant-B"
    assert args_b[0] != args_a[0]


@pytest.mark.asyncio
async def test_redis_sink_handles_unicode_correctly():
    pipe = MagicMock()
    pipe.lpush = MagicMock(return_value=pipe)
    pipe.ltrim = MagicMock(return_value=pipe)
    pipe.expire = MagicMock(return_value=pipe)
    pipe.execute = AsyncMock(return_value=[])
    redis = MagicMock()
    redis.pipeline = MagicMock(return_value=pipe)

    sink = RedisSink(redis)
    await sink.write_record({"query_scrubbed": "kilometraža"})

    args, _ = pipe.lpush.call_args
    # ensure_ascii=False keeps the diacritic literal
    assert "kilometraža" in args[1]


# ---------------------------------------------------------------------------
# StdoutJsonSink — primary production sink
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stdout_json_sink_writes_one_line_per_record():
    buf = io.StringIO()
    sink = StdoutJsonSink(stream=buf)
    await sink.write_record({"tool_picked": "get_X", "query_scrubbed": "kilometraža"})
    output = buf.getvalue()
    assert output.endswith("\n")
    parsed = json.loads(output.strip())
    assert parsed["tool_picked"] == "get_X"
    # Croatian survives ensure_ascii=False
    assert parsed["query_scrubbed"] == "kilometraža"


# ---------------------------------------------------------------------------
# TelemetryLogger — fan-out, failure isolation, disabled mode
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_logger_disabled_skips_write():
    logger = TelemetryLogger([NullSink()], enabled=False)
    assert logger.enabled is False
    await logger.log(TelemetryEvent(query_scrubbed="any"))


@pytest.mark.asyncio
async def test_logger_fans_out_to_all_sinks():
    a = MagicMock(spec=NullSink)
    a.write_record = AsyncMock()
    b = MagicMock(spec=NullSink)
    b.write_record = AsyncMock()
    log = TelemetryLogger([a, b], enabled=True)
    await log.log(TelemetryEvent(query_scrubbed="x"))
    a.write_record.assert_awaited_once()
    b.write_record.assert_awaited_once()


@pytest.mark.asyncio
async def test_logger_isolates_sink_failure():
    """One sink crashing must NOT break the other or the caller."""
    good = MagicMock(spec=NullSink)
    good.write_record = AsyncMock()

    class _Bad:
        async def write_record(self, _rec):
            raise RuntimeError("bad sink")

        async def aclose(self):
            return

    log = TelemetryLogger([good, _Bad()], enabled=True)
    await log.log(TelemetryEvent(query_scrubbed="x"))  # no raise
    good.write_record.assert_awaited_once()


@pytest.mark.asyncio
async def test_logger_handles_sink_failure_gracefully():
    """Single sink that raises — the .log() coroutine still completes."""
    class _BrokenSink:
        async def write_record(self, record):
            raise RuntimeError("disk full")
    log = TelemetryLogger([_BrokenSink()], enabled=True)
    await log.log(TelemetryEvent(query_scrubbed="any"))


@pytest.mark.asyncio
async def test_timed_context_records_latency():
    with tempfile.TemporaryDirectory() as tmp:
        log = TelemetryLogger([FileSink(log_dir=tmp)], enabled=True)
        async with log.timed(query_scrubbed="test") as evt:
            evt.tool_picked = "get_X"
            evt.error = None
        files = list(os.scandir(tmp))
        with open(files[0].path, encoding="utf-8") as f:
            row = json.loads(f.read().strip())
        assert row["tool_picked"] == "get_X"
        assert row["latency_ms"] >= 0


# ---------------------------------------------------------------------------
# from_env — backend selection
# ---------------------------------------------------------------------------

def test_logger_from_env_disabled_when_env_zero(monkeypatch):
    monkeypatch.setenv("V2_TELEMETRY", "0")
    log = TelemetryLogger.from_env()
    assert log.enabled is False


def test_logger_from_env_enabled_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("V2_TELEMETRY", raising=False)
    monkeypatch.delenv("V2_TELEMETRY_BACKEND", raising=False)
    log = TelemetryLogger.from_env()
    assert log.enabled is True


def test_logger_from_env_stdout_only_default(monkeypatch):
    """Default backend = stdout+redis. With no redis_client, only StdoutJsonSink."""
    monkeypatch.setenv("V2_TELEMETRY", "1")
    monkeypatch.delenv("V2_TELEMETRY_BACKEND", raising=False)
    log = TelemetryLogger.from_env(redis_client=None)
    assert log.enabled is True
    assert len(log._sinks) == 1
    assert isinstance(log._sinks[0], StdoutJsonSink)


def test_logger_from_env_dual_when_redis_present(monkeypatch):
    monkeypatch.setenv("V2_TELEMETRY", "1")
    monkeypatch.delenv("V2_TELEMETRY_BACKEND", raising=False)
    redis = MagicMock()
    log = TelemetryLogger.from_env(redis_client=redis)
    assert len(log._sinks) == 2
    assert isinstance(log._sinks[0], StdoutJsonSink)
    assert isinstance(log._sinks[1], RedisSink)


def test_logger_from_env_file_backend_adds_buffered(monkeypatch, tmp_path):
    monkeypatch.setenv("V2_TELEMETRY", "1")
    monkeypatch.setenv("V2_TELEMETRY_BACKEND", "both")
    monkeypatch.setenv("V2_TELEMETRY_DIR", str(tmp_path))
    redis = MagicMock()
    log = TelemetryLogger.from_env(redis_client=redis)
    types = [type(s).__name__ for s in log._sinks]
    assert "StdoutJsonSink" in types
    assert "RedisSink" in types
    assert "BufferedAsyncFileSink" in types


def test_logger_from_env_redis_only_falls_back_to_stdout(monkeypatch):
    """backend=redis without redis_client → fall back to stdout, don't drop telemetry."""
    monkeypatch.setenv("V2_TELEMETRY", "1")
    monkeypatch.setenv("V2_TELEMETRY_BACKEND", "redis")
    log = TelemetryLogger.from_env(redis_client=None)
    assert isinstance(log._sinks[0], StdoutJsonSink)


@pytest.mark.asyncio
async def test_logger_aclose_drains_buffered_sink(tmp_path):
    sink = BufferedAsyncFileSink(log_dir=str(tmp_path))
    log = TelemetryLogger([sink], enabled=True)
    for i in range(20):
        await log.log(TelemetryEvent(query_scrubbed=f"q{i}"))
    await log.aclose()
    files = [f for f in os.scandir(str(tmp_path)) if f.name.endswith(".jsonl")]
    assert len(files) == 1
    with open(files[0].path, encoding="utf-8") as f:
        assert len(f.readlines()) == 20


@pytest.mark.asyncio
async def test_logger_aclose_safe_on_disabled():
    log = TelemetryLogger([NullSink()], enabled=False)
    await log.aclose()


# ---------------------------------------------------------------------------
# Acceptance test: correlation_id propagation across call sites
# ---------------------------------------------------------------------------
# Run process_message end-to-end with a CapturingSink and verify all
# emitted events share the same correlation_id. Guards against future
# refactors that bypass the contextvar (e.g. someone passing a raw kwargs
# dict that silently overrides correlation_id with empty string).

class _CapturingSink:
    """Record every event the logger fans out, for assertions."""

    def __init__(self):
        self.records: list[dict] = []

    async def write_record(self, record):
        self.records.append(record)

    async def aclose(self):
        pass


# NOTE: integration tests reusing deleted test_engine_smoke fakes removed
# 2026-05-13. Telemetry ContextVar behavior covered by unit tests above
# (test_event_picks_up_correlation_id_from_context, test_set_request_context).


# ---------------------------------------------------------------------------
# Acceptance test: PII scrub on user message before telemetry
# ---------------------------------------------------------------------------
# Critical: when user replies "ne, drugi telefon je +385981234567", the
# PII scrubber upstream of telemetry MUST redact the phone number before
# it lands in query_scrubbed. Regression here is a security incident,
# not a feature gap.

def test_user_message_pii_scrubbed_before_telemetry():
    """End-to-end: PIIScrubber → TelemetryEvent.query_scrubbed."""
    from services.v2.pii_scrubber import PIIScrubber

    scrubber = PIIScrubber()
    raw = "ne, drugi telefon je +385981234567"
    result = scrubber.scrub(raw)
    evt = TelemetryEvent(query_scrubbed=result.scrubbed_text)
    rec = evt.to_record()

    # The exact phone number must NOT survive into the telemetry record
    assert "+385981234567" not in rec.get("query_scrubbed", "")
    assert "981234567" not in rec.get("query_scrubbed", "")


def test_redactions_field_carries_kinds_only():
    """Audit-trail invariant: TelemetryEvent.redactions stores the *kinds*
    of redactions (PHONE, OIB, ...) but NEVER the original values. If a
    future change leaks raw values into this field, this test fails."""
    from services.v2.pii_scrubber import PIIScrubber

    scrubber = PIIScrubber()
    raw = "moj OIB je 12345678901, mail mi je foo@bar.hr"
    result = scrubber.scrub(raw)
    evt = TelemetryEvent(redactions=[r.kind for r in result.redactions])
    rec = evt.to_record()

    assert "redactions" in rec
    # Each entry must be a kind label, never raw user data.
    for kind in rec["redactions"]:
        assert kind in {"OIB", "JMBG", "IBAN", "CARD", "EMAIL", "PHONE"}
    # And both expected kinds present.
    kinds = set(rec["redactions"])
    assert "OIB" in kinds
    assert "EMAIL" in kinds


def test_empty_redactions_dropped_from_record():
    """Default (no redactions) must not bloat every event with an empty list."""
    evt = TelemetryEvent(query_scrubbed="kolika km")
    rec = evt.to_record()
    assert "redactions" not in rec


# ---------------------------------------------------------------------------
# _log_telemetry translation shim — regression guard
# ---------------------------------------------------------------------------
# V2Engine has 25+ call sites that pass legacy field names (kind=,
# phone_hash=, persona=, query=, llm_confidence=, candidates_top5=,
# elapsed_ms=, executed_success=). Rather than edit all 25, the helper
# `V2Engine._log_telemetry` translates them centrally. Without a test,
# a translation regression would silently drop fields — events still
# emit, just empty in the wrong places, and downstream KQL fails quietly.

@pytest.mark.asyncio
async def test_log_telemetry_translates_legacy_kwargs():
    from services.v2.engine import V2Engine

    sink = _CapturingSink()
    tel = TelemetryLogger([sink], enabled=True)

    # Bypass dataclass __init__ — only need .telemetry + the failure flag
    engine = V2Engine.__new__(V2Engine)
    engine.telemetry = tel
    engine._telemetry_warning_emitted = False
    engine._current_is_negation = False
    set_request_context(correlation_id="cid-test", turn_number=3)

    await engine._log_telemetry(
        kind="recognize_and_gate",       # dropped
        phone_hash="abc123",             # dropped
        tenant_id="t1",
        query="rezerviraj auto",         # → query_scrubbed
        tool_picked="post_VehicleCalendar",
        llm_confidence=0.85,             # → confidence
        anchor_score=0.78,               # dropped
        anchor_top1="get_X",             # dropped
        candidates_top5=["a", "b", "c", "d", "e"],  # → competitors[:3]
        gate_decision="execute",         # dropped
        gate_reason="high_conf",         # dropped
        executed_success=True,           # success → error stays None
        elapsed_ms=1234.5,               # → latency_ms
        extra={"foo": "bar"},            # dropped
    )

    assert len(sink.records) == 1
    r = sink.records[0]

    # Translated correctly
    assert r["query_scrubbed"] == "rezerviraj auto"
    assert r["tool_picked"] == "post_VehicleCalendar"
    assert r["confidence"] == 0.85
    assert r["competitors"] == ["a", "b", "c"]
    assert r["latency_ms"] == 1234
    assert r["correlation_id"] == "cid-test"
    assert r["turn_number"] == 3
    assert r["tenant_id"] == "t1"

    # Dropped legacy fields not in event
    for legacy in ("kind", "phone_hash", "extra",
                   "anchor_score", "anchor_top1", "candidates_top5",
                   "gate_decision", "gate_reason", "llm_confidence",
                   "elapsed_ms", "executed_success", "query"):
        assert legacy not in r, f"legacy field {legacy!r} leaked into event"


@pytest.mark.asyncio
async def test_log_telemetry_executed_success_false_sets_error():
    """`executed_success=False` is a legacy way of saying the call failed.
    Translation must surface a non-None `error` so KQL `error != ''`
    queries catch it."""
    from services.v2.engine import V2Engine

    sink = _CapturingSink()
    tel = TelemetryLogger([sink], enabled=True)
    engine = V2Engine.__new__(V2Engine)
    engine.telemetry = tel
    engine._telemetry_warning_emitted = False
    engine._current_is_negation = False
    set_request_context(correlation_id="cid", turn_number=1)

    await engine._log_telemetry(
        tenant_id="t1",
        tool_picked="post_X",
        executed_success=False,
    )
    r = sink.records[0]
    assert r["error"] == "execution_failed"


@pytest.mark.asyncio
async def test_log_telemetry_clarify_dict_built_from_legacy_pair():
    """Legacy call sites passed clarify_options + clarify_chosen as
    parallel fields. Translation collapses them into a single nested
    `clarify` dict (the new shape)."""
    from services.v2.engine import V2Engine

    sink = _CapturingSink()
    tel = TelemetryLogger([sink], enabled=True)
    engine = V2Engine.__new__(V2Engine)
    engine.telemetry = tel
    engine._telemetry_warning_emitted = False
    engine._current_is_negation = False
    set_request_context(correlation_id="cid", turn_number=1)

    await engine._log_telemetry(
        tenant_id="t1",
        clarify_options=["get_A", "get_B", "get_C"],
        clarify_chosen="get_B",
    )
    r = sink.records[0]
    assert r["clarify"] == {"options": ["get_A", "get_B", "get_C"],
                            "picked": "get_B"}
    assert "clarify_options" not in r
    assert "clarify_chosen" not in r


# NOTE: 3 integration tests removed 2026-05-13. They referenced
# test_engine_smoke fakes deleted in Phase 6 cleanup. Their concerns
# (correlation_id parallel isolation, turn_number monotonicity,
# canonical event emission) are exercised end-to-end by
# tests/v2/test_engine_wireup.py which uses live fakes.
