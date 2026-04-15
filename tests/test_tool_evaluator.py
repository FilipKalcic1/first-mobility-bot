"""Tests for services/tool_evaluator.py – ToolEvaluator (Redis-backed)."""
import pytest
import time
import json
from unittest.mock import patch, MagicMock, AsyncMock

from services.tool_evaluator import ToolEvaluator, ToolMetrics


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def evaluator():
    """ToolEvaluator with no Redis (in-memory only)."""
    return ToolEvaluator(redis_client=None)


@pytest.fixture
def evaluator_with_redis():
    """ToolEvaluator with mock Redis client."""
    redis = AsyncMock()
    redis.hset = AsyncMock()
    redis.hgetall = AsyncMock(return_value={})
    redis.scan = AsyncMock(return_value=(0, []))
    return ToolEvaluator(redis_client=redis), redis


# ===========================================================================
# ToolMetrics
# ===========================================================================

class TestToolMetrics:
    def test_defaults(self):
        m = ToolMetrics(operation_id="op1")
        assert m.total_calls == 0
        assert m.successful_calls == 0
        assert m.failed_calls == 0
        assert m.error_types == {}

    def test_success_rate_no_calls(self):
        m = ToolMetrics(operation_id="op1")
        assert m.success_rate == 0.5  # neutral

    def test_success_rate_with_calls(self):
        m = ToolMetrics(operation_id="op1", total_calls=10, successful_calls=8)
        assert m.success_rate == 0.8

    def test_user_satisfaction_no_feedback(self):
        m = ToolMetrics(operation_id="op1")
        assert m.user_satisfaction == 0.5

    def test_user_satisfaction_with_feedback(self):
        m = ToolMetrics(operation_id="op1", positive_feedback=7, negative_feedback=3)
        assert m.user_satisfaction == 0.7

    def test_overall_score_new_tool(self):
        m = ToolMetrics(operation_id="op1")
        # 0.5*0.6 + 0.5*0.3 + 0.10 (no errors) = 0.3 + 0.15 + 0.10 = 0.55
        assert abs(m.overall_score - 0.55) < 0.01

    def test_overall_score_perfect(self):
        m = ToolMetrics(operation_id="op1", total_calls=100, successful_calls=100,
                        positive_feedback=50, negative_feedback=0)
        # 1.0*0.6 + 1.0*0.3 + 0.10 = 1.0
        assert m.overall_score == pytest.approx(1.0, abs=0.001)

    def test_overall_score_recent_error(self):
        m = ToolMetrics(operation_id="op1", total_calls=10, successful_calls=5,
                        positive_feedback=5, negative_feedback=5)
        # recent error = 0 recency
        m.last_error_ts = time.time()
        score = m.overall_score
        # 0.5*0.6 + 0.5*0.3 + 0.0 = 0.45
        assert abs(score - 0.45) < 0.01

    def test_overall_score_old_error(self):
        m = ToolMetrics(operation_id="op1", total_calls=10, successful_calls=10,
                        positive_feedback=10, negative_feedback=0)
        m.last_error_ts = time.time() - (48 * 3600)
        score = m.overall_score
        # 1.0*0.6 + 1.0*0.3 + 0.10 = 1.0
        assert score == pytest.approx(1.0, abs=0.001)

    def test_overall_score_error_within_day(self):
        m = ToolMetrics(operation_id="op1", total_calls=10, successful_calls=10)
        m.last_error_ts = time.time() - (12 * 3600)
        score = m.overall_score
        # 1.0*0.6 + 0.5*0.3 + 0.05 = 0.8
        assert abs(score - 0.80) < 0.01

    def test_to_dict(self):
        m = ToolMetrics(operation_id="op1", total_calls=5, successful_calls=3)
        d = m.to_dict()
        assert d["operation_id"] == "op1"
        assert d["total_calls"] == 5
        assert "success_rate" in d
        assert "user_satisfaction" in d
        assert "overall_score" in d

    def test_from_dict(self):
        d = {"operation_id": "op2", "total_calls": 10, "successful_calls": 8,
             "failed_calls": 2, "positive_feedback": 5, "negative_feedback": 1}
        m = ToolMetrics.from_dict(d)
        assert m.operation_id == "op2"
        assert m.total_calls == 10
        assert m.successful_calls == 8

    def test_from_dict_defaults(self):
        d = {"operation_id": "op3"}
        m = ToolMetrics.from_dict(d)
        assert m.total_calls == 0
        assert m.error_types == {}

    def test_redis_roundtrip(self):
        """to_redis_dict -> from_redis_dict preserves data."""
        m = ToolMetrics(
            operation_id="op1", total_calls=10, successful_calls=8,
            failed_calls=2, positive_feedback=5, negative_feedback=1,
            error_types={"network": 2}, last_error="server error",
            last_error_ts=1000.0, avg_response_time_ms=150.0,
            total_response_time_ms=1500.0, timed_calls=10,
            first_call_ts=900.0, last_call_ts=1000.0,
        )
        redis_dict = m.to_redis_dict()
        restored = ToolMetrics.from_redis_dict("op1", redis_dict)
        assert restored.total_calls == 10
        assert restored.successful_calls == 8
        assert restored.error_types == {"network": 2}
        assert restored.last_error == "server error"
        assert restored.last_error_ts == 1000.0


# ===========================================================================
# ToolEvaluator init
# ===========================================================================

class TestInit:
    def test_empty_init(self, evaluator):
        assert evaluator.metrics == {}

    def test_init_with_redis(self, evaluator_with_redis):
        ev, redis = evaluator_with_redis
        assert ev._redis is redis


# ===========================================================================
# record_success (async)
# ===========================================================================

class TestRecordSuccess:
    @pytest.mark.asyncio
    async def test_basic(self, evaluator):
        await evaluator.record_success("op1", response_time_ms=100.0)
        m = evaluator.metrics["op1"]
        assert m.total_calls == 1
        assert m.successful_calls == 1
        assert m.first_call_ts > 0
        assert m.last_call_ts > 0

    @pytest.mark.asyncio
    async def test_response_time_tracked(self, evaluator):
        await evaluator.record_success("op1", response_time_ms=200.0)
        await evaluator.record_success("op1", response_time_ms=400.0)
        m = evaluator.metrics["op1"]
        assert m.total_response_time_ms == 600.0
        assert m.avg_response_time_ms == 300.0

    @pytest.mark.asyncio
    async def test_zero_response_time_ignored(self, evaluator):
        await evaluator.record_success("op1", response_time_ms=0)
        assert evaluator.metrics["op1"].total_response_time_ms == 0.0

    @pytest.mark.asyncio
    async def test_persists_every_10_calls(self, evaluator_with_redis):
        ev, redis = evaluator_with_redis
        for i in range(10):
            await ev.record_success("op1")
        redis.hset.assert_called_once()  # Only on call #10

    @pytest.mark.asyncio
    async def test_first_call_only_set_once(self, evaluator):
        await evaluator.record_success("op1")
        first = evaluator.metrics["op1"].first_call_ts
        await evaluator.record_success("op1")
        assert evaluator.metrics["op1"].first_call_ts == first

    @pytest.mark.asyncio
    async def test_no_redis_no_crash(self, evaluator):
        """Without Redis, record_success works in-memory only."""
        for _ in range(20):
            await evaluator.record_success("op1")
        assert evaluator.metrics["op1"].total_calls == 20


# ===========================================================================
# record_failure (async)
# ===========================================================================

class TestRecordFailure:
    @pytest.mark.asyncio
    async def test_basic(self, evaluator):
        await evaluator.record_failure("op1", "Server Error", error_type="network")
        m = evaluator.metrics["op1"]
        assert m.total_calls == 1
        assert m.failed_calls == 1
        assert m.last_error == "Server Error"
        assert m.last_error_ts > 0
        assert m.error_types["network"] == 1

    @pytest.mark.asyncio
    async def test_error_message_truncated(self, evaluator):
        await evaluator.record_failure("op1", "x" * 500)
        assert len(evaluator.metrics["op1"].last_error) == 200

    @pytest.mark.asyncio
    async def test_error_types_counted(self, evaluator):
        await evaluator.record_failure("op1", "err1", error_type="network")
        await evaluator.record_failure("op1", "err2", error_type="network")
        await evaluator.record_failure("op1", "err3", error_type="validation")
        m = evaluator.metrics["op1"]
        assert m.error_types["network"] == 2
        assert m.error_types["validation"] == 1

    @pytest.mark.asyncio
    async def test_persists_on_every_failure(self, evaluator_with_redis):
        ev, redis = evaluator_with_redis
        await ev.record_failure("op1", "err")
        redis.hset.assert_called_once()

    @pytest.mark.asyncio
    async def test_redis_failure_doesnt_crash(self, evaluator_with_redis):
        ev, redis = evaluator_with_redis
        redis.hset = AsyncMock(side_effect=Exception("Redis down"))
        await ev.record_failure("op1", "err")  # No exception
        assert ev.metrics["op1"].failed_calls == 1


# ===========================================================================
# record_user_feedback (async)
# ===========================================================================

class TestRecordUserFeedback:
    @pytest.mark.asyncio
    async def test_positive(self, evaluator):
        await evaluator.record_user_feedback("op1", positive=True)
        assert evaluator.metrics["op1"].positive_feedback == 1

    @pytest.mark.asyncio
    async def test_negative(self, evaluator):
        await evaluator.record_user_feedback("op1", positive=False, feedback_text="wrong")
        assert evaluator.metrics["op1"].negative_feedback == 1

    @pytest.mark.asyncio
    async def test_persists_on_feedback(self, evaluator_with_redis):
        ev, redis = evaluator_with_redis
        await ev.record_user_feedback("op1", positive=True)
        redis.hset.assert_called_once()


# ===========================================================================
# get_score / get_penalty / get_boost
# ===========================================================================

class TestScoring:
    def test_unknown_tool_neutral(self, evaluator):
        assert evaluator.get_score("nonexistent") == 0.5

    @pytest.mark.asyncio
    async def test_known_tool_score(self, evaluator):
        for _ in range(10):
            await evaluator.record_success("op1")
        score = evaluator.get_score("op1")
        assert score > 0.5

    def test_penalty_for_bad_tool(self, evaluator):
        evaluator.metrics["op1"] = ToolMetrics(
            operation_id="op1", total_calls=10, successful_calls=0, failed_calls=10,
            last_error_ts=time.time()
        )
        penalty = evaluator.get_penalty("op1")
        assert penalty > 0.1

    def test_penalty_for_neutral(self, evaluator):
        """Unknown/new tools get 0 penalty (cold-start protection)."""
        assert evaluator.get_penalty("unknown") == 0.0

    def test_penalty_below_min_calls(self, evaluator):
        """Tools with < 5 calls get 0 penalty."""
        evaluator.metrics["op1"] = ToolMetrics(
            operation_id="op1", total_calls=3, successful_calls=0, failed_calls=3
        )
        assert evaluator.get_penalty("op1") == 0.0

    def test_boost_for_good_tool(self, evaluator):
        evaluator.metrics["op1"] = ToolMetrics(
            operation_id="op1", total_calls=100, successful_calls=100,
            positive_feedback=50, negative_feedback=0
        )
        boost = evaluator.get_boost("op1")
        assert boost > 0

    def test_no_boost_below_threshold(self, evaluator):
        evaluator.metrics["op1"] = ToolMetrics(
            operation_id="op1", total_calls=10, successful_calls=5
        )
        assert evaluator.get_boost("op1") == 0.0

    def test_no_boost_below_min_calls(self, evaluator):
        """Tools with < 5 calls get 0 boost."""
        evaluator.metrics["op1"] = ToolMetrics(
            operation_id="op1", total_calls=2, successful_calls=2,
            positive_feedback=10, negative_feedback=0
        )
        assert evaluator.get_boost("op1") == 0.0


# ===========================================================================
# apply_evaluation_adjustment
# ===========================================================================

class TestApplyAdjustment:
    def test_neutral_tool(self, evaluator):
        """Unknown tools get no adjustment (cold-start protection)."""
        result = evaluator.apply_evaluation_adjustment("unknown", 0.8)
        assert result == 0.8  # No penalty, no boost

    def test_clamp_to_bounds(self, evaluator):
        evaluator.metrics["bad"] = ToolMetrics(
            operation_id="bad", total_calls=100, successful_calls=0,
            last_error_ts=time.time()
        )
        result = evaluator.apply_evaluation_adjustment("bad", 0.1)
        assert result >= 0.0

    def test_good_tool_gets_boosted(self, evaluator):
        evaluator.metrics["good"] = ToolMetrics(
            operation_id="good", total_calls=100, successful_calls=100,
            positive_feedback=50, negative_feedback=0
        )
        result = evaluator.apply_evaluation_adjustment("good", 0.8)
        assert result >= 0.8  # boosted above base


# ===========================================================================
# get_statistics
# ===========================================================================

class TestGetStatistics:
    def test_empty(self, evaluator):
        stats = evaluator.get_statistics()
        assert stats["message"] == "No tool evaluations yet"

    @pytest.mark.asyncio
    async def test_with_data(self, evaluator):
        await evaluator.record_success("op1")
        await evaluator.record_success("op1")
        await evaluator.record_failure("op2", "err")
        stats = evaluator.get_statistics()
        assert stats["total_tools_tracked"] == 2
        assert stats["total_calls"] == 3
        assert stats["total_failures"] == 1
        assert len(stats["top_performers"]) > 0
        assert stats["storage"] == "in-memory"


# ===========================================================================
# Redis loading
# ===========================================================================

class TestRedisLoading:
    @pytest.mark.asyncio
    async def test_load_from_redis(self):
        redis = AsyncMock()
        redis.scan = AsyncMock(return_value=(0, [b"tool_eval:op1"]))
        redis.hgetall = AsyncMock(return_value={
            b"total_calls": b"10",
            b"successful_calls": b"8",
            b"failed_calls": b"2",
            b"positive_feedback": b"5",
            b"negative_feedback": b"1",
            b"error_types": b"{}",
            b"last_error": b"",
            b"last_error_ts": b"0",
            b"avg_response_time_ms": b"150.0",
            b"total_response_time_ms": b"1500.0",
            b"timed_calls": b"10",
            b"first_call_ts": b"900.0",
            b"last_call_ts": b"1000.0",
        })
        ev = ToolEvaluator(redis_client=redis)
        await ev.load_from_redis()
        assert "op1" in ev.metrics
        assert ev.metrics["op1"].total_calls == 10
        assert ev.metrics["op1"].successful_calls == 8

    @pytest.mark.asyncio
    async def test_load_from_redis_empty(self):
        redis = AsyncMock()
        redis.scan = AsyncMock(return_value=(0, []))
        ev = ToolEvaluator(redis_client=redis)
        await ev.load_from_redis()
        assert ev.metrics == {}

    @pytest.mark.asyncio
    async def test_load_from_redis_failure(self):
        redis = AsyncMock()
        redis.scan = AsyncMock(side_effect=Exception("Connection refused"))
        ev = ToolEvaluator(redis_client=redis)
        await ev.load_from_redis()  # No exception
        assert ev.metrics == {}

    @pytest.mark.asyncio
    async def test_no_redis_no_crash(self):
        ev = ToolEvaluator(redis_client=None)
        await ev.load_from_redis()  # No exception
        assert ev.metrics == {}


# ===========================================================================
# Singleton
# ===========================================================================

class TestSingleton:
    def test_get_tool_evaluator(self):
        import services.tool_evaluator as mod
        mod._tool_evaluator = None
        e1 = mod.get_tool_evaluator()
        e2 = mod.get_tool_evaluator()
        assert e1 is e2
        mod._tool_evaluator = None

    def test_redis_injected_later(self):
        import services.tool_evaluator as mod
        mod._tool_evaluator = None
        e1 = mod.get_tool_evaluator()
        assert e1._redis is None
        mock_redis = MagicMock()
        e2 = mod.get_tool_evaluator(redis_client=mock_redis)
        assert e1 is e2
        assert e2._redis is mock_redis
        mod._tool_evaluator = None
