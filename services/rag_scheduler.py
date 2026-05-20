"""
RAG Refresh Scheduler

PROBLEM SOLVED:
- RAG embeddings only refresh on container restart
- API changes go unnoticed until restart
- Stale embeddings = wrong tool selection

FEATURES:
- Periodic refresh (configurable interval)
- Force refresh via Redis pub/sub
- Health check for embedding freshness
- Versioned embeddings with rollback
- Proper async singleton pattern
- Graceful shutdown handling

USAGE:
    # Start scheduler in background
    scheduler = RAGScheduler(redis_client)
    await scheduler.start()

    # Force refresh (e.g., after API deployment)
    result = await scheduler.force_refresh()

    # Check health
    status = await scheduler.get_status()
"""

import os
import asyncio
import logging
import time
import json
import hashlib
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional, Callable, Awaitable
from dataclasses import dataclass, asdict
from enum import Enum

from services.retry_utils import calculate_backoff

logger = logging.getLogger(__name__)


def _task_error_logger(task: asyncio.Task) -> None:
    """Module-level done callback — avoids circular ref from bound methods."""
    try:
        exc = task.exception()
        if exc and not isinstance(exc, asyncio.CancelledError):
            logger.error(f"RAGScheduler task failed: {exc}")
    except (asyncio.CancelledError, asyncio.InvalidStateError):
        pass


# Singleton lock — created at module level. Safe because asyncio.Lock() does not
# require a running event loop at construction time (only at acquire time).
_singleton_lock = asyncio.Lock()


def _get_singleton_lock() -> asyncio.Lock:
    return _singleton_lock


class RefreshStatus(str, Enum):
    """Refresh operation status."""
    IDLE = "idle"
    REFRESHING = "refreshing"
    SUCCESS = "success"
    FAILED = "failed"


@dataclass
class RefreshMetrics:
    """Metrics for refresh operations."""
    last_refresh_at: Optional[str] = None
    last_refresh_duration_ms: int = 0
    last_refresh_status: str = RefreshStatus.IDLE.value
    total_refreshes: int = 0
    failed_refreshes: int = 0
    consecutive_failures: int = 0  # Track consecutive failures for backoff
    tools_count: int = 0
    embeddings_count: int = 0
    swagger_version: Optional[str] = None
    next_scheduled_refresh: Optional[str] = None
    last_error: Optional[str] = None


class RAGScheduler:
    """
    Scheduler for periodic RAG refresh operations.

    Components refreshed:
    1. Tool Registry (from Swagger)
    2. Embeddings (semantic search vectors)
    3. Tool documentation cache
    """

    REDIS_KEY_METRICS = "rag:scheduler:metrics"
    REDIS_KEY_LOCK = "rag:scheduler:lock"
    REDIS_CHANNEL_REFRESH = "rag:refresh:trigger"

    # Backoff configuration
    MIN_RETRY_DELAY_SECONDS = 60
    MAX_RETRY_DELAY_SECONDS = 3600  # 1 hour max

    def __init__(
        self,
        redis_client,
        refresh_interval_hours: Optional[float] = None,
        on_refresh_callback: Callable[[], Awaitable[Dict]] = None
    ):
        """
        Initialize scheduler.

        Args:
            redis_client: Async Redis client
            refresh_interval_hours: Hours between refreshes (default: 6)
            on_refresh_callback: Async function to call for refresh
        """
        from config import get_settings
        _settings = get_settings()
        self.redis = redis_client
        self.refresh_interval = timedelta(
            hours=refresh_interval_hours or _settings.RAG_REFRESH_INTERVAL_HOURS
        )
        self.lock_ttl_seconds = _settings.RAG_LOCK_TTL_SECONDS
        self.on_refresh_callback = on_refresh_callback

        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._pubsub_task: Optional[asyncio.Task] = None
        self._pubsub = None
        self.metrics = RefreshMetrics()
        self._refresh_in_progress = False
        self._shutdown_event = asyncio.Event()

        logger.info(
            f"RAGScheduler initialized: interval={self.refresh_interval}, "
            f"lock_ttl={self.lock_ttl_seconds}s"
        )

    async def start(self) -> None:
        """Start the scheduler."""
        if self._running:
            logger.warning("RAGScheduler already running")
            return

        self._running = True
        self._shutdown_event.clear()

        # Load previous metrics
        await self._load_metrics()

        # Start periodic refresh task
        # Use a non-bound callback to avoid cycle: self → task → callback → self
        self._task = asyncio.create_task(self._refresh_loop())
        self._task.add_done_callback(_task_error_logger)

        # Start pub/sub listener for force refresh
        self._pubsub_task = asyncio.create_task(self._pubsub_listener())
        self._pubsub_task.add_done_callback(_task_error_logger)

        logger.info("RAGScheduler started")

    async def stop(self) -> None:
        """Stop the scheduler gracefully with state preservation."""
        logger.info("RAGScheduler stopping...")
        self._running = False
        self._shutdown_event.set()

        # Save current state before shutdown
        await self._save_metrics()

        # Cancel tasks gracefully
        tasks_to_cancel = []
        if self._task and not self._task.done():
            tasks_to_cancel.append(self._task)
        if self._pubsub_task and not self._pubsub_task.done():
            tasks_to_cancel.append(self._pubsub_task)

        for task in tasks_to_cancel:
            task.cancel()

        # Wait for tasks to complete
        if tasks_to_cancel:
            await asyncio.gather(*tasks_to_cancel, return_exceptions=True)

        # Break reference cycles: self → task → callback
        self._task = None
        self._pubsub_task = None

        # Clean up pubsub
        if self._pubsub:
            try:
                await self._pubsub.unsubscribe(self.REDIS_CHANNEL_REFRESH)
                await self._pubsub.close()
            except Exception as e:
                logger.debug(f"Pubsub cleanup error: {e}")

        # Release lock if we hold it
        try:
            await self.redis.delete(self.REDIS_KEY_LOCK)
        except (ConnectionError, OSError) as e:
            logger.debug(f"Could not release RAG lock during shutdown: {e}")

        logger.info("RAGScheduler stopped")

    async def _refresh_loop(self) -> None:
        """Main refresh loop with exponential backoff on failures.

        In addition to periodic scheduled refreshes, performs lightweight
        Swagger version checks every 5 minutes. If the Swagger spec hash
        changes (new API endpoints deployed), triggers an immediate refresh
        without waiting for the full interval.
        """
        # Lightweight Swagger change detection interval (5 min)
        SWAGGER_CHECK_INTERVAL = 300
        last_swagger_check = 0.0

        while self._running:
            try:
                # Calculate time until next refresh
                now = datetime.now(timezone.utc)
                next_refresh = self._calculate_next_refresh()

                self.metrics.next_scheduled_refresh = next_refresh.isoformat()
                await self._save_metrics()

                wait_seconds = max(0, (next_refresh - now).total_seconds())

                if wait_seconds > 0:
                    logger.info(
                        f"RAG refresh scheduled in {wait_seconds / 3600:.1f} hours"
                    )

                    # Instead of sleeping until next full refresh, wake every
                    # SWAGGER_CHECK_INTERVAL to check for Swagger changes
                    while wait_seconds > 0 and self._running:
                        sleep_chunk = min(wait_seconds, SWAGGER_CHECK_INTERVAL)
                        try:
                            await asyncio.wait_for(
                                self._shutdown_event.wait(),
                                timeout=sleep_chunk
                            )
                            # Shutdown requested
                            break
                        except asyncio.TimeoutError:
                            wait_seconds -= sleep_chunk

                        # Swagger change detection (lightweight HTTP HEAD / hash)
                        if time.time() - last_swagger_check >= SWAGGER_CHECK_INTERVAL:
                            last_swagger_check = time.time()
                            if await self._detect_swagger_change():
                                logger.info("Swagger spec changed — triggering immediate refresh")
                                break  # Exit inner loop to trigger refresh now

                    if self._shutdown_event.is_set():
                        break

                if self._running:
                    success = await self._do_refresh(trigger="scheduled")

                    # Backoff on failure
                    if not success:
                        backoff = self._calculate_backoff()
                        logger.warning(
                            f"RAG refresh failed, backing off for {backoff}s"
                        )
                        try:
                            await asyncio.wait_for(
                                self._shutdown_event.wait(),
                                timeout=backoff,
                            )
                            break  # Shutdown requested during backoff
                        except asyncio.TimeoutError:
                            pass  # Backoff complete, continue loop

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"RAG refresh loop error: {e}")
                try:
                    await asyncio.wait_for(
                        self._shutdown_event.wait(),
                        timeout=self.MIN_RETRY_DELAY_SECONDS,
                    )
                    break  # Shutdown requested during retry delay
                except asyncio.TimeoutError:
                    pass  # Normal — continue loop

    async def _detect_swagger_change(self) -> bool:
        """
        Lightweight check: fetch Swagger spec and compare hash to last known version.

        Returns True if the spec has changed (new endpoints deployed),
        triggering an immediate FAISS re-index instead of waiting for the
        next scheduled refresh.
        """
        try:
            from config import get_settings
            _settings = get_settings()
            sources = getattr(_settings, 'swagger_sources', [])
            if not sources:
                return False

            import httpx
            # Fetch first Swagger source (lightweight — just the spec JSON)
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(sources[0])
                if resp.status_code != 200:
                    return False
                content_hash = hashlib.sha256(resp.content).hexdigest()[:16]

            # Compare to stored hash
            stored_hash = await self.redis.get("rag:swagger_hash")
            if stored_hash:
                if isinstance(stored_hash, bytes):
                    stored_hash = stored_hash.decode('utf-8')
                if stored_hash == content_hash:
                    return False  # No change

            # Store new hash
            await self.redis.set("rag:swagger_hash", content_hash, ex=86400)
            if stored_hash is not None:
                logger.info(f"Swagger spec changed: {stored_hash} -> {content_hash}")
                return True  # Changed!
            return False  # First check, no baseline yet

        except Exception as e:
            logger.debug(f"Swagger change detection failed (non-critical): {e}")
            return False

    def _calculate_backoff(self) -> int:
        """Calculate exponential backoff based on consecutive failures."""
        failures = self.metrics.consecutive_failures
        return int(calculate_backoff(failures, base_delay=self.MIN_RETRY_DELAY_SECONDS, max_delay=self.MAX_RETRY_DELAY_SECONDS))

    async def _pubsub_listener(self) -> None:
        """Listen for force refresh commands via Redis pub/sub."""
        try:
            self._pubsub = self.redis.pubsub()
            await self._pubsub.subscribe(self.REDIS_CHANNEL_REFRESH)

            while self._running:
                try:
                    message = await self._pubsub.get_message(
                        ignore_subscribe_messages=True,
                        timeout=1.0
                    )

                    if message and message.get("type") == "message":
                        data_str = message.get("data", "{}")
                        if isinstance(data_str, bytes):
                            data_str = data_str.decode('utf-8')
                        data = json.loads(data_str)
                        logger.info(f"Force refresh triggered: {data}")
                        await self._do_refresh(
                            trigger="manual",
                            reason=data.get("reason")
                        )

                except asyncio.TimeoutError:
                    # Expected - just continue checking
                    continue
                except json.JSONDecodeError as e:
                    logger.warning(f"Invalid refresh message format: {e}")
                except ConnectionError as e:
                    logger.error(f"Redis connection error in pubsub: {e}")
                    await asyncio.sleep(5)
                    # Try to reconnect
                    try:
                        await self._pubsub.unsubscribe(self.REDIS_CHANNEL_REFRESH)
                        await self._pubsub.subscribe(self.REDIS_CHANNEL_REFRESH)
                    except (ConnectionError, OSError) as reconnect_err:
                        logger.warning(f"Pubsub reconnect failed, will retry next loop: {reconnect_err}")
                except Exception as e:
                    logger.error(f"Pub/sub error: {e}", exc_info=True)
                    await asyncio.sleep(5)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Pubsub listener fatal error: {e}", exc_info=True)

    async def force_refresh(
        self,
        reason: str = "manual",
        wait_for_completion: bool = False,
        timeout_seconds: int = 300
    ) -> Dict:
        """
        Trigger immediate refresh.

        Args:
            reason: Reason for refresh (logged)
            wait_for_completion: If True, wait for refresh to complete
            timeout_seconds: Max wait time if waiting

        Returns:
            Status dict
        """
        if wait_for_completion:
            # Execute refresh directly instead of via pubsub
            success = await asyncio.wait_for(
                self._do_refresh(trigger="manual", reason=reason),
                timeout=timeout_seconds
            )
            return {
                "status": "completed" if success else "failed",
                "reason": reason,
                "metrics": asdict(self.metrics)
            }
        else:
            # Just trigger via pubsub (non-blocking)
            await self.redis.publish(
                self.REDIS_CHANNEL_REFRESH,
                json.dumps({
                    "reason": reason,
                    "triggered_at": datetime.now(timezone.utc).isoformat()
                })
            )
            return {"status": "triggered", "reason": reason}

    async def _do_refresh(
        self,
        trigger: str = "unknown",
        reason: Optional[str] = None
    ) -> bool:
        """
        Execute refresh operation.

        Uses distributed lock to prevent concurrent refreshes.
        """
        if self._refresh_in_progress:
            logger.warning("Refresh already in progress, skipping")
            return False

        # Try to acquire lock with unique identifier
        lock_value = f"{os.getpid()}:{time.time()}:{id(self)}"
        lock_acquired = await self.redis.set(
            self.REDIS_KEY_LOCK,
            lock_value,
            nx=True,  # Only if not exists
            ex=self.lock_ttl_seconds
        )

        if not lock_acquired:
            logger.warning("RAG refresh skipped: another instance is refreshing")
            return False

        self._refresh_in_progress = True

        try:
            self.metrics.last_refresh_status = RefreshStatus.REFRESHING.value
            await self._save_metrics()

            start_time = time.time()
            logger.info(f"RAG refresh starting (trigger={trigger}, reason={reason})")

            # Execute refresh callback
            if self.on_refresh_callback:
                result = await self.on_refresh_callback()
            else:
                result = await self._default_refresh()

            # Update metrics
            duration_ms = int((time.time() - start_time) * 1000)

            self.metrics.last_refresh_at = datetime.now(timezone.utc).isoformat()
            self.metrics.last_refresh_duration_ms = duration_ms
            self.metrics.last_refresh_status = RefreshStatus.SUCCESS.value
            self.metrics.total_refreshes += 1
            self.metrics.consecutive_failures = 0  # Reset on success
            self.metrics.tools_count = result.get("tools_count", 0)
            self.metrics.embeddings_count = result.get("embeddings_count", 0)
            self.metrics.swagger_version = result.get("swagger_version")
            self.metrics.last_error = None

            await self._save_metrics()

            logger.info(
                f"RAG refresh completed in {duration_ms}ms: "
                f"tools={self.metrics.tools_count}, "
                f"embeddings={self.metrics.embeddings_count}"
            )

            return True

        except Exception as e:
            self.metrics.last_refresh_status = RefreshStatus.FAILED.value
            self.metrics.failed_refreshes += 1
            self.metrics.consecutive_failures += 1
            self.metrics.last_error = str(e)[:500]  # Truncate error
            await self._save_metrics()

            logger.error(f"RAG refresh failed: {e}", exc_info=True)
            return False

        finally:
            self._refresh_in_progress = False
            # Atomic lock release via Lua (prevents TOCTOU race).
            # Shield ensures the release completes even if the parent task
            # was cancelled (e.g. force_refresh timeout). Without shield,
            # CancelledError propagates through the await and the lock
            # stays stranded until TTL expires (600s).
            try:
                from services.retry_utils import ATOMIC_LOCK_RELEASE_LUA
                await asyncio.shield(self.redis.eval(
                    ATOMIC_LOCK_RELEASE_LUA,
                    1,
                    self.REDIS_KEY_LOCK,
                    lock_value,
                ))
            except (Exception, asyncio.CancelledError) as e:
                logger.warning(f"Failed to release lock: {e}")

    async def _default_refresh(self) -> Dict:
        """
        Default refresh implementation.

        Override on_refresh_callback for custom logic.
        This is a placeholder that should be overridden.
        """
        logger.warning(
            "Using default RAG refresh - configure on_refresh_callback for "
            "actual tool registry and embedding refresh"
        )

        # Try to import actual implementations if available
        result = {
            "tools_count": 0,
            "embeddings_count": 0,
            "swagger_version": None
        }

        # v1 services.engine refresh removed 2026-05-08. RAG refresh
        # logic now lives in scripts/ (sync_tools, generate_tool_embeddings)
        # — invoked via cron, not from the runtime scheduler.

        # Generate version using SHA256 (more secure than MD5)
        version_input = json.dumps({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tools": result["tools_count"],
            "embeddings": result["embeddings_count"]
        }).encode()
        result["swagger_version"] = hashlib.sha256(version_input).hexdigest()[:12]

        return result

    def _calculate_next_refresh(self) -> datetime:
        """Calculate next scheduled refresh time."""
        now = datetime.now(timezone.utc)

        if self.metrics.last_refresh_at:
            try:
                last = datetime.fromisoformat(
                    self.metrics.last_refresh_at.replace('Z', '+00:00')
                )
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)

                next_time = last + self.refresh_interval

                # If next_time is in the past, schedule for now
                if next_time < now:
                    return now

                # If last_refresh_at is somehow in the future, use now + interval
                if last > now:
                    logger.warning(
                        "last_refresh_at is in the future, resetting schedule"
                    )
                    return now + self.refresh_interval

                return next_time

            except (ValueError, TypeError) as e:
                logger.warning(f"Error parsing last_refresh_at: {e}")
                return now + self.refresh_interval
        else:
            # First refresh: schedule immediately
            return now

    async def _load_metrics(self) -> None:
        """Load metrics from Redis."""
        try:
            data = await self.redis.get(self.REDIS_KEY_METRICS)
            if data:
                if isinstance(data, bytes):
                    data = data.decode('utf-8')
                metrics_dict = json.loads(data)
                self.metrics = RefreshMetrics(**metrics_dict)
        except Exception as e:
            logger.warning(f"Failed to load RAG metrics: {e}")

    async def _save_metrics(self) -> None:
        """Save metrics to Redis."""
        try:
            await self.redis.set(
                self.REDIS_KEY_METRICS,
                json.dumps(asdict(self.metrics))
            )
        except Exception as e:
            logger.warning(f"Failed to save RAG metrics: {e}")

    async def get_status(self) -> Dict:
        """
        Get current scheduler status.

        Returns:
            Status dict with metrics
        """
        await self._load_metrics()

        # Calculate freshness
        freshness = "unknown"
        age_hours = None
        if self.metrics.last_refresh_at:
            try:
                last = datetime.fromisoformat(
                    self.metrics.last_refresh_at.replace('Z', '+00:00')
                )
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                age_hours = (datetime.now(timezone.utc) - last).total_seconds() / 3600

                if age_hours < 6:
                    freshness = "fresh"
                elif age_hours < 24:
                    freshness = "stale"
                else:
                    freshness = "outdated"
            except (ValueError, TypeError):
                pass

        return {
            "running": self._running,
            "refresh_in_progress": self._refresh_in_progress,
            "freshness": freshness,
            "age_hours": round(age_hours, 2) if age_hours else None,
            "refresh_interval_hours": self.refresh_interval.total_seconds() / 3600,
            **asdict(self.metrics)
        }

    async def health_check(self) -> Dict:
        """
        Health check for monitoring.

        Returns:
            Health status with warnings
        """
        status = await self.get_status()
        warnings = []

        if status["freshness"] == "outdated":
            warnings.append("RAG embeddings are outdated (>24h)")

        if status["freshness"] == "stale":
            warnings.append("RAG embeddings are stale (>6h)")

        if self.metrics.consecutive_failures > 3:
            warnings.append(
                f"Multiple consecutive failures: {self.metrics.consecutive_failures}"
            )

        if status["failed_refreshes"] > 10:
            warnings.append(
                f"High total failure count: {status['failed_refreshes']}"
            )

        if status["last_refresh_status"] == RefreshStatus.FAILED.value:
            warnings.append(f"Last refresh failed: {self.metrics.last_error or 'unknown'}")

        if not self._running:
            warnings.append("Scheduler is not running")

        return {
            "healthy": len(warnings) == 0 or (
                len(warnings) == 1 and
                status["freshness"] in ["fresh", "stale"]
            ),
            "warnings": warnings,
            "next_refresh": self.metrics.next_scheduled_refresh,
            **status
        }


# Global scheduler instance — all access serialized via the module-level
# asyncio.Lock (see `_singleton_lock` above). The whole system is async, so
# we never mix threading locks on this state.
_scheduler: Optional[RAGScheduler] = None


async def get_rag_scheduler(
    redis_client,
    on_refresh_callback: Callable[[], Awaitable[Dict]] = None
) -> RAGScheduler:
    """Get or create RAG scheduler singleton (async-safe)."""
    global _scheduler

    if _scheduler is not None:
        return _scheduler

    async with _get_singleton_lock():
        if _scheduler is None:
            _scheduler = RAGScheduler(
                redis_client,
                on_refresh_callback=on_refresh_callback,
            )
        return _scheduler


async def reset_rag_scheduler() -> None:
    """Reset the singleton (for testing). Async to share the singleton lock."""
    global _scheduler
    async with _get_singleton_lock():
        _scheduler = None
