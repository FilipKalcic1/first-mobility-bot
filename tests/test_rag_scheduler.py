"""Tests for services/rag_scheduler.py – RAGScheduler."""
import asyncio
import json
import pytest
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

# Must patch config.get_settings before importing RAGScheduler
_mock_settings = MagicMock()
_mock_settings.RAG_REFRESH_INTERVAL_HOURS = 6
_mock_settings.RAG_LOCK_TTL_SECONDS = 600


def _redis():
    r = AsyncMock()
    r.get = AsyncMock(return_value=None)
    r.set = AsyncMock(return_value=True)
    r.delete = AsyncMock(return_value=True)
    r.publish = AsyncMock(return_value=1)
    ps = AsyncMock()
    ps.subscribe = AsyncMock()
    ps.unsubscribe = AsyncMock()
    ps.close = AsyncMock()
    ps.get_message = AsyncMock(return_value=None)
    r.pubsub = MagicMock(return_value=ps)
    return r


def _sched(redis=None, interval=1.0, callback=None):
    with patch("config.get_settings", return_value=_mock_settings):
        from services.rag_scheduler import RAGScheduler
        return RAGScheduler(redis or _redis(), refresh_interval_hours=interval, on_refresh_callback=callback)


# ===========================================================================
# Enums & Dataclasses
# ===========================================================================

class TestRefreshStatus:
    def test_values(self):
        from services.rag_scheduler import RefreshStatus
        assert RefreshStatus.IDLE == "idle"
        assert RefreshStatus.SUCCESS == "success"
        assert RefreshStatus.FAILED == "failed"
        assert RefreshStatus.REFRESHING == "refreshing"


class TestRefreshMetrics:
    def test_defaults(self):
        from services.rag_scheduler import RefreshMetrics
        m = RefreshMetrics()
        assert m.total_refreshes == 0
        assert m.consecutive_failures == 0
        assert m.last_refresh_at is None

    def test_asdict(self):
        from services.rag_scheduler import RefreshMetrics
        m = RefreshMetrics(total_refreshes=5, tools_count=10)
        d = asdict(m)
        assert d["total_refreshes"] == 5


# ===========================================================================
# Init
# ===========================================================================

class TestInit:
    def test_default_interval(self):
        s = _sched()
        assert s.refresh_interval == timedelta(hours=1)

    def test_initial_state(self):
        s = _sched()
        assert s._running is False
        assert s._task is None
        assert s._refresh_in_progress is False


# ===========================================================================
# _calculate_backoff
# ===========================================================================

class TestCalculateBackoff:
    def test_zero_failures(self):
        s = _sched()
        s.metrics.consecutive_failures = 0
        assert s._calculate_backoff() == 60

    def test_one_failure(self):
        s = _sched()
        s.metrics.consecutive_failures = 1
        assert s._calculate_backoff() == 120

    def test_capped_at_max(self):
        s = _sched()
        s.metrics.consecutive_failures = 20
        assert s._calculate_backoff() == 3600


# ===========================================================================
# _calculate_next_refresh
# ===========================================================================

class TestCalculateNextRefresh:
    def test_no_last_refresh(self):
        s = _sched(interval=1.0)
        s.metrics.last_refresh_at = None
        r = s._calculate_next_refresh()
        assert abs((r - datetime.now(timezone.utc)).total_seconds()) < 2

    def test_with_recent_refresh(self):
        s = _sched(interval=2.0)
        last = datetime.now(timezone.utc) - timedelta(hours=1)
        s.metrics.last_refresh_at = last.isoformat()
        r = s._calculate_next_refresh()
        expected = last + timedelta(hours=2)
        assert abs((r - expected).total_seconds()) < 2

    def test_overdue_returns_now(self):
        s = _sched(interval=1.0)
        s.metrics.last_refresh_at = (datetime.now(timezone.utc) - timedelta(hours=10)).isoformat()
        r = s._calculate_next_refresh()
        assert abs((r - datetime.now(timezone.utc)).total_seconds()) < 2

    def test_future_last_refresh(self):
        s = _sched(interval=1.0)
        s.metrics.last_refresh_at = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()
        r = s._calculate_next_refresh()
        expected = datetime.now(timezone.utc) + timedelta(hours=1)
        assert abs((r - expected).total_seconds()) < 2

    def test_invalid_date(self):
        s = _sched(interval=1.0)
        s.metrics.last_refresh_at = "not-a-date"
        r = s._calculate_next_refresh()
        expected = datetime.now(timezone.utc) + timedelta(hours=1)
        assert abs((r - expected).total_seconds()) < 2

    def test_z_suffix(self):
        s = _sched(interval=2.0)
        last = datetime.now(timezone.utc) - timedelta(hours=1)
        s.metrics.last_refresh_at = last.strftime("%Y-%m-%dT%H:%M:%S") + "Z"
        r = s._calculate_next_refresh()
        expected = last + timedelta(hours=2)
        assert abs((r - expected).total_seconds()) < 2

    def test_naive_datetime(self):
        s = _sched(interval=1.0)
        s.metrics.last_refresh_at = "2020-01-01T00:00:00"
        r = s._calculate_next_refresh()
        assert abs((r - datetime.now(timezone.utc)).total_seconds()) < 2


# ===========================================================================
# _load_metrics / _save_metrics
# ===========================================================================

class TestMetricsPersistence:
    @pytest.mark.asyncio
    async def test_load_none(self):
        redis = _redis()
        redis.get = AsyncMock(return_value=None)
        s = _sched(redis=redis)
        await s._load_metrics()
        assert s.metrics.total_refreshes == 0

    @pytest.mark.asyncio
    async def test_load_json_string(self):
        from services.rag_scheduler import RefreshMetrics
        data = asdict(RefreshMetrics(total_refreshes=7))
        redis = _redis()
        redis.get = AsyncMock(return_value=json.dumps(data))
        s = _sched(redis=redis)
        await s._load_metrics()
        assert s.metrics.total_refreshes == 7

    @pytest.mark.asyncio
    async def test_load_bytes(self):
        from services.rag_scheduler import RefreshMetrics
        data = asdict(RefreshMetrics(total_refreshes=4))
        redis = _redis()
        redis.get = AsyncMock(return_value=json.dumps(data).encode())
        s = _sched(redis=redis)
        await s._load_metrics()
        assert s.metrics.total_refreshes == 4

    @pytest.mark.asyncio
    async def test_load_error_handled(self):
        redis = _redis()
        redis.get = AsyncMock(side_effect=Exception("fail"))
        s = _sched(redis=redis)
        await s._load_metrics()
        assert s.metrics.total_refreshes == 0

    @pytest.mark.asyncio
    async def test_save(self):
        redis = _redis()
        s = _sched(redis=redis)
        s.metrics.total_refreshes = 99
        await s._save_metrics()
        redis.set.assert_called_once()
        args = redis.set.call_args[0]
        assert args[0] == "rag:scheduler:metrics"
        assert json.loads(args[1])["total_refreshes"] == 99

    @pytest.mark.asyncio
    async def test_save_error_handled(self):
        redis = _redis()
        redis.set = AsyncMock(side_effect=Exception("fail"))
        s = _sched(redis=redis)
        await s._save_metrics()  # no exception


# ===========================================================================
# _do_refresh
# ===========================================================================

class TestDoRefresh:
    @pytest.mark.asyncio
    async def test_skip_if_in_progress(self):
        s = _sched()
        s._refresh_in_progress = True
        assert await s._do_refresh() is False

    @pytest.mark.asyncio
    async def test_skip_if_lock_not_acquired(self):
        redis = _redis()
        redis.set = AsyncMock(return_value=False)
        s = _sched(redis=redis)
        assert await s._do_refresh() is False

    @pytest.mark.asyncio
    async def test_success_with_callback(self):
        redis = _redis()
        cb = AsyncMock(return_value={"tools_count": 10, "embeddings_count": 50, "swagger_version": "v1"})
        s = _sched(redis=redis, callback=cb)
        assert await s._do_refresh(trigger="test") is True
        assert s.metrics.tools_count == 10
        assert s.metrics.total_refreshes == 1
        assert s.metrics.consecutive_failures == 0
        assert s.metrics.last_refresh_status == "success"
        assert s._refresh_in_progress is False

    @pytest.mark.asyncio
    async def test_failure_updates_metrics(self):
        redis = _redis()
        cb = AsyncMock(side_effect=RuntimeError("boom"))
        s = _sched(redis=redis, callback=cb)
        assert await s._do_refresh() is False
        assert s.metrics.last_refresh_status == "failed"
        assert s.metrics.failed_refreshes == 1
        assert s.metrics.consecutive_failures == 1
        assert "boom" in s.metrics.last_error
        assert s._refresh_in_progress is False

    @pytest.mark.asyncio
    async def test_error_truncated(self):
        redis = _redis()
        cb = AsyncMock(side_effect=RuntimeError("x" * 1000))
        s = _sched(redis=redis, callback=cb)
        await s._do_refresh()
        assert len(s.metrics.last_error) == 500

    @pytest.mark.asyncio
    async def test_lock_released_on_success(self):
        redis = _redis()
        lock_val = None

        async def capture_set(key, value, **kw):
            nonlocal lock_val
            if key == "rag:scheduler:lock":
                lock_val = value
            return True

        redis.set = AsyncMock(side_effect=capture_set)
        redis.get = AsyncMock(side_effect=lambda k: lock_val if k == "rag:scheduler:lock" else None)
        cb = AsyncMock(return_value={"tools_count": 0, "embeddings_count": 0})
        s = _sched(redis=redis, callback=cb)
        await s._do_refresh()
        # Lock release uses atomic Lua eval (compare-and-delete)
        redis.eval.assert_called_once()
        call_args = redis.eval.call_args
        assert "rag:scheduler:lock" in call_args[0]  # Lock key in args

    @pytest.mark.asyncio
    async def test_lock_not_released_if_stolen(self):
        redis = _redis()
        redis.get = AsyncMock(return_value="other-process")
        cb = AsyncMock(return_value={"tools_count": 0, "embeddings_count": 0})
        s = _sched(redis=redis, callback=cb)
        await s._do_refresh()
        # Lock release is always attempted via Lua eval (compare-and-delete).
        # The Lua script does nothing when the stored token != our token,
        # so the lock is NOT deleted even though eval IS called.
        redis.eval.assert_called_once()

    @pytest.mark.asyncio
    async def test_lock_bytes_decoded(self):
        redis = _redis()
        stored = None

        async def cap(key, value, **kw):
            nonlocal stored
            if key == "rag:scheduler:lock":
                stored = value
            return True

        redis.set = AsyncMock(side_effect=cap)
        redis.get = AsyncMock(side_effect=lambda k: stored.encode() if k == "rag:scheduler:lock" and stored else None)
        cb = AsyncMock(return_value={"tools_count": 0, "embeddings_count": 0})
        s = _sched(redis=redis, callback=cb)
        await s._do_refresh()
        # Lock release uses atomic Lua eval (compare-and-delete)
        redis.eval.assert_called_once()
        call_args = redis.eval.call_args
        assert "rag:scheduler:lock" in call_args[0]  # Lock key in args

    @pytest.mark.asyncio
    async def test_lock_release_error(self):
        redis = _redis()
        redis.get = AsyncMock(side_effect=Exception("gone"))
        cb = AsyncMock(return_value={"tools_count": 0, "embeddings_count": 0})
        s = _sched(redis=redis, callback=cb)
        assert await s._do_refresh() is True

    @pytest.mark.asyncio
    async def test_default_refresh_no_callback(self):
        redis = _redis()
        s = _sched(redis=redis, callback=None)
        result = await s._do_refresh()
        assert result is True
        assert s.metrics.swagger_version is not None

    @pytest.mark.asyncio
    async def test_consecutive_failures_reset(self):
        redis = _redis()
        cb = AsyncMock(return_value={"tools_count": 1, "embeddings_count": 1})
        s = _sched(redis=redis, callback=cb)
        s.metrics.consecutive_failures = 5
        s.metrics.failed_refreshes = 10
        await s._do_refresh()
        assert s.metrics.consecutive_failures == 0
        assert s.metrics.failed_refreshes == 10


# ===========================================================================
# force_refresh
# ===========================================================================

class TestForceRefresh:
    @pytest.mark.asyncio
    async def test_no_wait(self):
        redis = _redis()
        s = _sched(redis=redis)
        result = await s.force_refresh(reason="deploy")
        assert result["status"] == "triggered"
        redis.publish.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_wait_success(self):
        redis = _redis()
        cb = AsyncMock(return_value={"tools_count": 5, "embeddings_count": 20, "swagger_version": "v1"})
        s = _sched(redis=redis, callback=cb)
        result = await s.force_refresh(wait_for_completion=True)
        assert result["status"] == "completed"
        assert "metrics" in result

    @pytest.mark.asyncio
    async def test_wait_failure(self):
        redis = _redis()
        redis.set = AsyncMock(return_value=False)
        s = _sched(redis=redis)
        result = await s.force_refresh(wait_for_completion=True)
        assert result["status"] == "failed"

    @pytest.mark.asyncio
    async def test_wait_timeout(self):
        redis = _redis()

        async def slow():
            await asyncio.sleep(10)
            return {}

        s = _sched(redis=redis, callback=slow)
        with pytest.raises(asyncio.TimeoutError):
            await s.force_refresh(wait_for_completion=True, timeout_seconds=0.1)


# ===========================================================================
# get_status
# ===========================================================================

class TestGetStatus:
    @pytest.mark.asyncio
    async def test_unknown_freshness(self):
        s = _sched()
        status = await s.get_status()
        assert status["freshness"] == "unknown"
        assert status["running"] is False

    @pytest.mark.asyncio
    async def test_fresh(self):
        s = _sched()
        s.metrics.last_refresh_at = datetime.now(timezone.utc).isoformat()
        status = await s.get_status()
        assert status["freshness"] == "fresh"

    @pytest.mark.asyncio
    async def test_stale(self):
        s = _sched()
        s.metrics.last_refresh_at = (datetime.now(timezone.utc) - timedelta(hours=10)).isoformat()
        status = await s.get_status()
        assert status["freshness"] == "stale"

    @pytest.mark.asyncio
    async def test_outdated(self):
        s = _sched()
        s.metrics.last_refresh_at = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
        status = await s.get_status()
        assert status["freshness"] == "outdated"

    @pytest.mark.asyncio
    async def test_invalid_date(self):
        s = _sched()
        s.metrics.last_refresh_at = "bad"
        status = await s.get_status()
        assert status["freshness"] == "unknown"

    @pytest.mark.asyncio
    async def test_z_suffix(self):
        s = _sched()
        s.metrics.last_refresh_at = "2020-01-01T00:00:00Z"
        status = await s.get_status()
        assert status["freshness"] == "outdated"

    @pytest.mark.asyncio
    async def test_naive_timestamp(self):
        s = _sched()
        s.metrics.last_refresh_at = "2020-01-01T00:00:00"
        status = await s.get_status()
        assert status["freshness"] == "outdated"


# ===========================================================================
# health_check
# ===========================================================================

class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_healthy(self):
        s = _sched()
        s._running = True
        s.metrics.last_refresh_at = datetime.now(timezone.utc).isoformat()
        s.metrics.last_refresh_status = "success"
        r = await s.health_check()
        assert r["healthy"] is True
        assert r["warnings"] == []

    @pytest.mark.asyncio
    async def test_outdated_warning(self):
        s = _sched()
        s._running = True
        s.metrics.last_refresh_at = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
        r = await s.health_check()
        assert any("outdated" in w for w in r["warnings"])

    @pytest.mark.asyncio
    async def test_not_running_warning(self):
        s = _sched()
        s._running = False
        r = await s.health_check()
        assert any("not running" in w.lower() for w in r["warnings"])

    @pytest.mark.asyncio
    async def test_consecutive_failures_warning(self):
        s = _sched()
        s._running = True
        s.metrics.consecutive_failures = 5
        s.metrics.last_refresh_at = datetime.now(timezone.utc).isoformat()
        r = await s.health_check()
        assert any("consecutive" in w.lower() for w in r["warnings"])

    @pytest.mark.asyncio
    async def test_last_failed_warning(self):
        s = _sched()
        s._running = True
        s.metrics.last_refresh_status = "failed"
        s.metrics.last_error = "timeout"
        s.metrics.last_refresh_at = datetime.now(timezone.utc).isoformat()
        r = await s.health_check()
        assert any("failed" in w.lower() for w in r["warnings"])

    @pytest.mark.asyncio
    async def test_stale_single_warning_still_healthy(self):
        s = _sched()
        s._running = True
        s.metrics.last_refresh_at = (datetime.now(timezone.utc) - timedelta(hours=10)).isoformat()
        s.metrics.last_refresh_status = "success"
        s.metrics.consecutive_failures = 0
        s.metrics.failed_refreshes = 0
        r = await s.health_check()
        assert r["healthy"] is True


# ===========================================================================
# _task_done_callback
# ===========================================================================

class TestTaskDoneCallback:
    def test_with_exception(self):
        from services.rag_scheduler import _task_error_logger
        t = MagicMock()
        t.exception.return_value = RuntimeError("fail")
        _task_error_logger(t)

    def test_with_cancelled(self):
        from services.rag_scheduler import _task_error_logger
        t = MagicMock()
        t.exception.side_effect = asyncio.CancelledError()
        _task_error_logger(t)

    def test_with_invalid_state(self):
        from services.rag_scheduler import _task_error_logger
        t = MagicMock()
        t.exception.side_effect = asyncio.InvalidStateError()
        _task_error_logger(t)

    def test_no_exception(self):
        from services.rag_scheduler import _task_error_logger
        t = MagicMock()
        t.exception.return_value = None
        _task_error_logger(t)


# ===========================================================================
# start / stop
# ===========================================================================

class TestStartStop:
    @pytest.mark.asyncio
    async def test_start_and_stop(self):
        redis = _redis()
        s = _sched(redis=redis)
        s._refresh_loop = AsyncMock()
        s._pubsub_listener = AsyncMock()
        await s.start()
        assert s._running is True
        assert s._task is not None
        await s.stop()
        assert s._running is False

    @pytest.mark.asyncio
    async def test_start_idempotent(self):
        redis = _redis()
        s = _sched(redis=redis)
        s._refresh_loop = AsyncMock()
        s._pubsub_listener = AsyncMock()
        await s.start()
        t1 = s._task
        await s.start()
        assert s._task is t1
        await s.stop()

    @pytest.mark.asyncio
    async def test_stop_cleans_pubsub(self):
        redis = _redis()
        s = _sched(redis=redis)
        s._refresh_loop = AsyncMock()
        s._pubsub_listener = AsyncMock()
        await s.start()
        ps = AsyncMock()
        s._pubsub = ps
        await s.stop()
        ps.unsubscribe.assert_awaited_once()
        ps.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stop_pubsub_error(self):
        redis = _redis()
        s = _sched(redis=redis)
        s._refresh_loop = AsyncMock()
        s._pubsub_listener = AsyncMock()
        await s.start()
        ps = AsyncMock()
        ps.unsubscribe.side_effect = Exception("fail")
        s._pubsub = ps
        await s.stop()

    @pytest.mark.asyncio
    async def test_stop_lock_delete_error(self):
        redis = _redis()
        redis.delete = AsyncMock(side_effect=ConnectionError("fail"))
        s = _sched(redis=redis)
        s._refresh_loop = AsyncMock()
        s._pubsub_listener = AsyncMock()
        await s.start()
        await s.stop()


# ===========================================================================
# Singleton helpers
# ===========================================================================

class TestSingletonHelpers:
    @pytest.mark.asyncio
    async def test_get_and_reset(self):
        import services.rag_scheduler as mod
        await mod.reset_rag_scheduler()
        redis = _redis()
        with patch("config.get_settings", return_value=_mock_settings):
            s1 = await mod.get_rag_scheduler(redis)
            s2 = await mod.get_rag_scheduler(redis)
            assert s1 is s2
        await mod.reset_rag_scheduler()
        with patch("config.get_settings", return_value=_mock_settings):
            s3 = await mod.get_rag_scheduler(redis)
            assert s3 is not s1
        await mod.reset_rag_scheduler()
