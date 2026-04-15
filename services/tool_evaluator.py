"""
Tool Evaluator - Performance Tracking & Learning

KRITIČNA KOMPONENTA za kvalitetu sustava.

Prati:
1. Uspješnost alata (success rate)
2. Točnost rezultata (user feedback)
3. Vrijeme izvršenja
4. Učestalost grešaka

Omogućava:
- Penaliziranje alata koji daju krive rezultate
- Boost alata koji rade dobro
- Učenje iz grešaka
- Statistike za debugging

STORAGE:
- In-memory dict for fast sync reads (get_score, get_penalty, get_boost)
- Redis HASH for persistence across pods (shared state in K8s)
- Async writes to Redis (non-blocking)
- Graceful fallback to in-memory-only if Redis unavailable

PRIMJER KORIŠTENJA:
    evaluator = get_tool_evaluator(redis_client=redis)

    # After a successful call (async — writes to Redis)
    await evaluator.record_success("get_MasterData", response_time_ms=500)

    # After a failed call (async — writes to Redis)
    await evaluator.record_failure("get_Vehicles", "Wrong data returned")

    # Sync reads from in-memory cache (fast, no I/O)
    score = evaluator.get_score("get_MasterData")  # 0.0 - 1.0
"""

import logging
import time
from typing import Dict, Any, Optional
from dataclasses import dataclass, field
import threading
import json

logger = logging.getLogger(__name__)

# Redis key prefix for tool metrics
_REDIS_KEY_PREFIX = "tool_eval:"


@dataclass
class ToolMetrics:
    """Metrics for a single tool."""
    operation_id: str

    # Success/Failure tracking
    total_calls: int = 0
    successful_calls: int = 0
    failed_calls: int = 0

    # User feedback tracking
    positive_feedback: int = 0
    negative_feedback: int = 0

    # Error tracking
    error_types: Dict[str, int] = field(default_factory=dict)
    last_error: Optional[str] = None
    last_error_ts: float = 0.0  # POSIX timestamp (monotonic-friendly)

    # Performance tracking
    avg_response_time_ms: float = 0.0
    total_response_time_ms: float = 0.0
    timed_calls: int = 0  # Calls that had response_time_ms > 0

    # Timestamps (POSIX float)
    first_call_ts: float = 0.0
    last_call_ts: float = 0.0

    @property
    def success_rate(self) -> float:
        """Calculate success rate (0.0 - 1.0)."""
        if self.total_calls == 0:
            return 0.5  # Neutral for new tools
        return self.successful_calls / self.total_calls

    @property
    def user_satisfaction(self) -> float:
        """Calculate user satisfaction (0.0 - 1.0)."""
        total_feedback = self.positive_feedback + self.negative_feedback
        if total_feedback == 0:
            return 0.5  # Neutral if no feedback
        return self.positive_feedback / total_feedback

    @property
    def overall_score(self) -> float:
        """
        Calculate overall tool score (0.0 - 1.0).

        Combines:
        - Success rate (60% weight)
        - User satisfaction (30% weight)
        - Recency penalty (10% weight) - penalize tools with recent errors
        """
        score = 0.0

        # Success rate (60%)
        score += self.success_rate * 0.6

        # User satisfaction (30%)
        score += self.user_satisfaction * 0.3

        # Recency bonus/penalty (10%)
        if self.last_error_ts > 0:
            hours_since_error = (time.time() - self.last_error_ts) / 3600

            if hours_since_error < 1:
                score += 0.0
            elif hours_since_error < 24:
                score += 0.05
            else:
                score += 0.10
        else:
            # No errors ever - bonus
            score += 0.10

        return min(1.0, max(0.0, score))

    def to_redis_dict(self) -> Dict[str, str]:
        """Serialize to flat string dict for Redis HASH."""
        return {
            "total_calls": str(self.total_calls),
            "successful_calls": str(self.successful_calls),
            "failed_calls": str(self.failed_calls),
            "positive_feedback": str(self.positive_feedback),
            "negative_feedback": str(self.negative_feedback),
            "error_types": json.dumps(self.error_types),
            "last_error": self.last_error or "",
            "last_error_ts": str(self.last_error_ts),
            "avg_response_time_ms": str(self.avg_response_time_ms),
            "total_response_time_ms": str(self.total_response_time_ms),
            "timed_calls": str(self.timed_calls),
            "first_call_ts": str(self.first_call_ts),
            "last_call_ts": str(self.last_call_ts),
        }

    def to_dict(self) -> Dict:
        """Serialize for stats/debug output."""
        return {
            "operation_id": self.operation_id,
            "total_calls": self.total_calls,
            "successful_calls": self.successful_calls,
            "failed_calls": self.failed_calls,
            "positive_feedback": self.positive_feedback,
            "negative_feedback": self.negative_feedback,
            "error_types": self.error_types,
            "last_error": self.last_error,
            "last_error_ts": self.last_error_ts,
            "avg_response_time_ms": self.avg_response_time_ms,
            "total_response_time_ms": self.total_response_time_ms,
            "timed_calls": self.timed_calls,
            "first_call_ts": self.first_call_ts,
            "last_call_ts": self.last_call_ts,
            "success_rate": self.success_rate,
            "user_satisfaction": self.user_satisfaction,
            "overall_score": self.overall_score
        }

    @classmethod
    def from_redis_dict(cls, operation_id: str, data: Dict[str, str]) -> "ToolMetrics":
        """Deserialize from Redis HASH values."""
        error_types = {}
        try:
            error_types = json.loads(data.get("error_types", "{}"))
        except (json.JSONDecodeError, TypeError):
            pass

        return cls(
            operation_id=operation_id,
            total_calls=int(data.get("total_calls", 0)),
            successful_calls=int(data.get("successful_calls", 0)),
            failed_calls=int(data.get("failed_calls", 0)),
            positive_feedback=int(data.get("positive_feedback", 0)),
            negative_feedback=int(data.get("negative_feedback", 0)),
            error_types=error_types,
            last_error=data.get("last_error") or None,
            last_error_ts=float(data.get("last_error_ts", 0)),
            avg_response_time_ms=float(data.get("avg_response_time_ms", 0)),
            total_response_time_ms=float(data.get("total_response_time_ms", 0)),
            timed_calls=int(data.get("timed_calls", 0)),
            first_call_ts=float(data.get("first_call_ts", 0)),
            last_call_ts=float(data.get("last_call_ts", 0)),
        )

    @classmethod
    def from_dict(cls, data: Dict) -> "ToolMetrics":
        """Deserialize from dict (backwards compat for tests)."""
        return cls(
            operation_id=data["operation_id"],
            total_calls=data.get("total_calls", 0),
            successful_calls=data.get("successful_calls", 0),
            failed_calls=data.get("failed_calls", 0),
            positive_feedback=data.get("positive_feedback", 0),
            negative_feedback=data.get("negative_feedback", 0),
            error_types=data.get("error_types", {}),
            last_error=data.get("last_error"),
            last_error_ts=data.get("last_error_ts", 0.0),
            avg_response_time_ms=data.get("avg_response_time_ms", 0.0),
            total_response_time_ms=data.get("total_response_time_ms", 0.0),
            timed_calls=data.get("timed_calls", 0),
            first_call_ts=data.get("first_call_ts", 0.0),
            last_call_ts=data.get("last_call_ts", 0.0),
        )


class ToolEvaluator:
    """
    Tool performance evaluator with learning capabilities.

    Features:
    1. Track success/failure rates per tool
    2. Learn from user feedback
    3. Apply penalties for poor performance
    4. Boost well-performing tools
    5. Persist evaluations in Redis (shared across pods)

    Architecture:
    - In-memory dict for fast sync reads (get_score, get_penalty, get_boost)
    - Redis HASH per tool for persistence (writes are async, non-blocking)
    - Falls back to in-memory-only when Redis unavailable
    """

    def __init__(self, redis_client=None):
        self._lock = threading.Lock()
        self.metrics: Dict[str, ToolMetrics] = {}
        self._redis = redis_client

    async def load_from_redis(self) -> None:
        """Load all metrics from Redis into in-memory cache.

        Call this during app startup after Redis is connected.
        """
        if not self._redis:
            logger.info("ToolEvaluator: No Redis client — in-memory only mode")
            return

        try:
            # Scan for all tool_eval:* keys
            cursor = b"0"
            keys = []
            while True:
                cursor, batch = await self._redis.scan(
                    cursor=cursor, match=f"{_REDIS_KEY_PREFIX}*", count=100
                )
                keys.extend(batch)
                if cursor == b"0" or cursor == 0:
                    break

            if not keys:
                logger.info("ToolEvaluator: No metrics in Redis — starting fresh")
                return

            # Load each tool's metrics
            loaded = 0
            for key in keys:
                try:
                    key_str = key.decode() if isinstance(key, bytes) else key
                    operation_id = key_str[len(_REDIS_KEY_PREFIX):]
                    data = await self._redis.hgetall(key)
                    if data:
                        # Decode bytes to strings
                        decoded = {
                            (k.decode() if isinstance(k, bytes) else k):
                            (v.decode() if isinstance(v, bytes) else v)
                            for k, v in data.items()
                        }
                        self.metrics[operation_id] = ToolMetrics.from_redis_dict(
                            operation_id, decoded
                        )
                        loaded += 1
                except Exception as e:
                    logger.warning(f"ToolEvaluator: Failed to load key {key}: {e}")

            logger.info(f"ToolEvaluator: Loaded {loaded} tool metrics from Redis")
        except Exception as e:
            logger.warning(f"ToolEvaluator: Redis load failed — in-memory only: {e}")

    async def _persist_to_redis(self, operation_id: str, metrics: ToolMetrics) -> None:
        """Write metrics to Redis HASH (fire-and-forget safe)."""
        if not self._redis:
            return
        try:
            key = f"{_REDIS_KEY_PREFIX}{operation_id}"
            await self._redis.hset(key, mapping=metrics.to_redis_dict())
        except Exception as e:
            logger.warning(f"ToolEvaluator: Redis write failed for {operation_id}: {e}")

    def _get_or_create_metrics(self, operation_id: str) -> ToolMetrics:
        """Get or create metrics for a tool."""
        if operation_id not in self.metrics:
            self.metrics[operation_id] = ToolMetrics(operation_id=operation_id)
        return self.metrics[operation_id]

    async def record_success(
        self,
        operation_id: str,
        response_time_ms: float = 0.0,
        params_used: Optional[Dict] = None
    ) -> None:
        """
        Record successful tool execution.

        Args:
            operation_id: Tool that succeeded
            response_time_ms: Response time in milliseconds
            params_used: Parameters that were used (for learning)
        """
        with self._lock:
            metrics = self._get_or_create_metrics(operation_id)

            now = time.time()

            metrics.total_calls += 1
            metrics.successful_calls += 1
            metrics.last_call_ts = now

            if metrics.first_call_ts == 0:
                metrics.first_call_ts = now

            if response_time_ms > 0:
                metrics.total_response_time_ms += response_time_ms
                metrics.timed_calls += 1
                metrics.avg_response_time_ms = (
                    metrics.total_response_time_ms / metrics.timed_calls
                )

            should_persist = metrics.total_calls % 10 == 0

        logger.debug(
            f"Recorded success for {operation_id} "
            f"(rate: {metrics.success_rate:.1%}, score: {metrics.overall_score:.2f})"
        )

        if should_persist:
            await self._persist_to_redis(operation_id, metrics)

    async def record_failure(
        self,
        operation_id: str,
        error_message: str,
        error_type: str = "unknown",
        params_used: Optional[Dict] = None
    ) -> None:
        """
        Record failed tool execution.

        Args:
            operation_id: Tool that failed
            error_message: Error message
            error_type: Error category (validation, permission, network, etc.)
            params_used: Parameters that were used
        """
        with self._lock:
            metrics = self._get_or_create_metrics(operation_id)

            now = time.time()

            metrics.total_calls += 1
            metrics.failed_calls += 1
            metrics.last_call_ts = now
            metrics.last_error = error_message[:200]
            metrics.last_error_ts = now

            if metrics.first_call_ts == 0:
                metrics.first_call_ts = now

            if error_type not in metrics.error_types:
                metrics.error_types[error_type] = 0
            metrics.error_types[error_type] += 1

        logger.info(
            f"Recorded failure for {operation_id}: {error_type} "
            f"(rate: {metrics.success_rate:.1%}, score: {metrics.overall_score:.2f})"
        )

        await self._persist_to_redis(operation_id, metrics)

    async def record_user_feedback(
        self,
        operation_id: str,
        positive: bool,
        feedback_text: Optional[str] = None
    ) -> None:
        """
        Record user feedback for tool result.

        This is KEY for learning - users can indicate when results are wrong.

        Args:
            operation_id: Tool that produced the result
            positive: True if user accepted result, False if they complained
            feedback_text: Optional user feedback text
        """
        with self._lock:
            metrics = self._get_or_create_metrics(operation_id)

            if positive:
                metrics.positive_feedback += 1
            else:
                metrics.negative_feedback += 1

        if positive:
            logger.debug(f"Positive feedback for {operation_id}")
        else:
            logger.info(
                f"Negative feedback for {operation_id}: {feedback_text or 'no text'}"
            )

        await self._persist_to_redis(operation_id, metrics)

    def get_score(self, operation_id: str) -> float:
        """
        Get overall score for a tool (0.0 - 1.0).

        Higher score = better performance.
        Reads from in-memory cache (sync, fast).

        Args:
            operation_id: Tool to score

        Returns:
            Score from 0.0 (worst) to 1.0 (best)
        """
        if operation_id not in self.metrics:
            return 0.5  # Neutral for unknown tools

        return self.metrics[operation_id].overall_score

    # Minimum calls before penalty/boost kicks in.
    # Tools with fewer calls stay neutral to avoid cold-start bias.
    _MIN_CALLS_FOR_ADJUSTMENT = 5

    def get_penalty(self, operation_id: str) -> float:
        """
        Get penalty adjustment for tool score.

        Returns negative value to subtract from base similarity score.
        Tools with fewer than _MIN_CALLS_FOR_ADJUSTMENT calls get 0 penalty.

        Args:
            operation_id: Tool to penalize

        Returns:
            Penalty (0.0 to 0.2) to subtract from score
        """
        metrics = self.metrics.get(operation_id)
        if not metrics or metrics.total_calls < self._MIN_CALLS_FOR_ADJUSTMENT:
            return 0.0  # Neutral for new/unknown tools

        score = metrics.overall_score

        # Invert score to get penalty
        # score 1.0 → penalty 0.0
        # score 0.5 → penalty 0.1
        # score 0.0 → penalty 0.2
        penalty = (1.0 - score) * 0.2

        return penalty

    def get_boost(self, operation_id: str) -> float:
        """
        Get boost adjustment for tool score.

        Returns positive value to add to base similarity score.
        Tools with fewer than _MIN_CALLS_FOR_ADJUSTMENT calls get 0 boost.

        Args:
            operation_id: Tool to boost

        Returns:
            Boost (0.0 to 0.1) to add to score
        """
        metrics = self.metrics.get(operation_id)
        if not metrics or metrics.total_calls < self._MIN_CALLS_FOR_ADJUSTMENT:
            return 0.0  # Neutral for new/unknown tools

        score = metrics.overall_score

        # High performers get boost
        # score 1.0 → boost 0.1
        # score 0.5 → boost 0.0
        # score 0.0 → boost 0.0
        if score > 0.7:
            boost = (score - 0.7) / 3.0  # Max 0.1
            return boost

        return 0.0

    def apply_evaluation_adjustment(
        self,
        operation_id: str,
        base_score: float
    ) -> float:
        """
        Apply evaluation-based adjustment to tool score.

        Args:
            operation_id: Tool being scored
            base_score: Base similarity score

        Returns:
            Adjusted score
        """
        boost = self.get_boost(operation_id)
        penalty = self.get_penalty(operation_id)

        adjusted = base_score + boost - penalty

        if boost > 0 or penalty > 0:
            logger.debug(
                f"📊 {operation_id}: {base_score:.3f} "
                f"+ {boost:.3f} - {penalty:.3f} = {adjusted:.3f}"
            )

        return max(0.0, min(1.0, adjusted))

    def get_statistics(self) -> Dict[str, Any]:
        """Get overall statistics for debugging."""
        if not self.metrics:
            return {"message": "No tool evaluations yet"}

        total_calls = sum(m.total_calls for m in self.metrics.values())
        total_success = sum(m.successful_calls for m in self.metrics.values())
        total_failures = sum(m.failed_calls for m in self.metrics.values())

        # Top performers
        sorted_by_score = sorted(
            self.metrics.values(),
            key=lambda m: m.overall_score,
            reverse=True
        )

        top_5 = [
            {"tool": m.operation_id, "score": m.overall_score, "calls": m.total_calls}
            for m in sorted_by_score[:5]
        ]

        # Worst performers
        bottom_5 = [
            {"tool": m.operation_id, "score": m.overall_score, "calls": m.total_calls}
            for m in sorted_by_score[-5:]
            if m.total_calls > 0
        ]

        return {
            "total_tools_tracked": len(self.metrics),
            "total_calls": total_calls,
            "overall_success_rate": total_success / total_calls if total_calls > 0 else 0,
            "total_failures": total_failures,
            "top_performers": top_5,
            "worst_performers": bottom_5,
            "storage": "redis" if self._redis else "in-memory",
        }


# Global instance
_tool_evaluator: Optional[ToolEvaluator] = None
_singleton_lock = threading.Lock()


def get_tool_evaluator(redis_client=None) -> ToolEvaluator:
    """Get global tool evaluator instance.

    Args:
        redis_client: Optional async Redis client for persistence.
                      Only used on first call (singleton creation).
    """
    global _tool_evaluator
    if _tool_evaluator is None:
        with _singleton_lock:
            if _tool_evaluator is None:
                _tool_evaluator = ToolEvaluator(redis_client=redis_client)
    elif redis_client and not _tool_evaluator._redis:
        _tool_evaluator._redis = redis_client
    return _tool_evaluator
