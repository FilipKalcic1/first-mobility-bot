"""
Background Worker

Processes messages from Redis queue.
"""

import asyncio
import signal
import time
import json
import sys
import hashlib
import random
import traceback
import os
import logging
from datetime import datetime, timezone
from typing import Optional, Set, Dict
from contextlib import suppress

# PII-Safe Logging Filter (shared module — single source of truth)
from services.pii_filter import PIIScrubFilter


# Configure logging BEFORE any imports that use logging
# This prevents duplicate handlers from being added
_pii_filter = PIIScrubFilter()
_stderr_handler = logging.StreamHandler(open(sys.stderr.fileno(), mode='w', encoding='utf-8', closefd=False))
_stderr_handler.addFilter(_pii_filter)
logging.basicConfig(
    level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format='%(message)s',  # Simple format since we use structured JSON logs
    handlers=[_stderr_handler],
    force=True  # Override any existing configuration
)
# Allow WARNING+ from most libraries, but let key services log at INFO
# This prevents noisy spam while keeping important service logs visible
logging.getLogger('sqlalchemy.engine').setLevel(logging.WARNING)
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)

import redis.asyncio as aioredis
try:
    from redis.exceptions import (
        ConnectionError as RedisConnectionError,
        RedisError,
        ResponseError,
    )
    if not (isinstance(RedisConnectionError, type) and issubclass(RedisConnectionError, BaseException)):
        raise TypeError("redis.exceptions returned stub types")
except Exception:
    RedisConnectionError = OSError  # type: ignore[assignment,misc]
    RedisError = OSError  # type: ignore[assignment,misc]
    class ResponseError(Exception):  # type: ignore[assignment]
        """Sentinel — never raised; safe fallback when redis.exceptions unavailable."""

from config import get_settings
from services.rag_scheduler import RAGScheduler, get_rag_scheduler
from services.errors import ConversationError, ErrorCode
from services.tracing import get_tracer, trace_span

settings = get_settings()
_tracer = get_tracer("worker")

MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "5"))  # 0.5 CPU single-server default: 5 concurrent
                                # Bottleneck is LLM RTT (4s), not concurrency. KEDA scales pods if needed.
MESSAGE_LOCK_TTL = 300          # 5 min - enough for longest LLM calls

# Lua script for atomic lock release: only delete if caller still holds it
_RELEASE_LOCK_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""

# Lua script for atomic delayed-outbound promotion.
# Atomically pops up to ARGV[2] due items from the sorted set and pushes each
# to the outbound list.  Returns the raw JSON members that were promoted.
# This prevents the race where two workers both zrangebyscore the same items.
_PROMOTE_DELAYED_LUA = """
local due = redis.call('zrangebyscore', KEYS[1], '-inf', ARGV[1], 'LIMIT', 0, ARGV[2])
if #due == 0 then return {} end
for _, raw in ipairs(due) do
    redis.call('rpush', KEYS[2], raw)
    redis.call('zrem', KEYS[1], raw)
end
return due
"""

REDIS_MAX_RETRIES = 30          # 30 x 2s = 60s max wait for Redis
REDIS_RETRY_DELAY = 2
HEALTH_REPORT_INTERVAL = 60     # Every minute
STREAM_BLOCK_MS = 1000          # 1s blocking read
MEMORY_WARNING_MB = 800         # Warn when memory exceeds this

# Enable tracemalloc for production memory monitoring (low overhead: ~3MB, <2% CPU)
# Only stores 1 frame per allocation to minimize cost
tracemalloc = None  # Lazy: only import if enabled
if os.environ.get("TRACEMALLOC", "").lower() in ("true", "1", "yes"):
    import tracemalloc
    tracemalloc.start(1)


def get_memory_usage_mb() -> float:
    """Get current process memory usage in MB."""
    try:
        import psutil
        process = psutil.Process(os.getpid())
        return process.memory_info().rss / 1024 / 1024
    except ImportError:
        # psutil not installed, try /proc/self/status on Linux
        try:
            with open('/proc/self/status', 'r', encoding='utf-8') as f:
                for line in f:
                    if line.startswith('VmRSS:'):
                        return int(line.split()[1]) / 1024  # Convert KB to MB
        except (OSError, IOError) as e:
            log("debug", "memory_proc_read_failed", {"error": str(e)})
    except (OSError, ValueError) as e:
        log("debug", "memory_psutil_failed", {"error": str(e)})
    return 0.0


def log(level: str, event: str, data: dict = None):
    """JSON structured logging for container orchestrators."""
    log_entry = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        "level": level,
        "event": event,
        "worker": "worker",
        **(data or {})
    }

    # Add memory usage for warnings and errors
    if level in ("warn", "error"):
        log_entry["memory_mb"] = round(get_memory_usage_mb(), 1)

    sys.stdout.write(json.dumps(log_entry) + "\n")
    sys.stdout.flush()


def log_exception(event: str, e: Exception, context: dict = None):
    """Log exception with full stack trace."""
    error_data = {
        "error_type": type(e).__name__,
        "error_message": str(e),
        "stack_trace": traceback.format_exc(),
        "memory_mb": round(get_memory_usage_mb(), 1),
        **(context or {})
    }
    log("error", event, error_data)


class GracefulShutdown:
    """Handles graceful shutdown signals."""

    def __init__(self):
        self.shutdown_event = asyncio.Event()
        self.active_tasks: Set[asyncio.Task] = set()

    def request_shutdown(self):
        log("info", "shutdown_requested")
        self.shutdown_event.set()

    def is_shutting_down(self) -> bool:
        return self.shutdown_event.is_set()

    async def wait_for_shutdown(self):
        await self.shutdown_event.wait()

    def track_task(self, task: asyncio.Task):
        self.active_tasks.add(task)
        task.add_done_callback(self.active_tasks.discard)

    async def wait_for_tasks(self, timeout: float = 100.0):
        if not self.active_tasks:
            return

        log("info", "waiting_for_tasks", {"count": len(self.active_tasks)})

        try:
            await asyncio.wait_for(
                asyncio.gather(*self.active_tasks, return_exceptions=True),
                timeout=timeout
            )
            log("info", "tasks_completed")
        except asyncio.TimeoutError:
            log("warn", "tasks_timeout_cancelling", {"count": len(self.active_tasks)})
            for task in self.active_tasks:
                task.cancel()


class Worker:
    """Background message processor with concurrent execution."""

    def __init__(self):
        self.redis: Optional[aioredis.Redis] = None
        self.shutdown = GracefulShutdown()
        # EDGE-1 fix (Filip 2026-05-20): timestamp alone collides if two
        # workers start in the same second → same consumer_name → XREADGROUP
        # competes for the same messages, breaking ordering. PID + random
        # suffix guarantees uniqueness across concurrent worker boots.
        self.consumer_name = (
            f"worker_{int(datetime.now(timezone.utc).timestamp())}"
            f"_{os.getpid()}_{random.randint(0, 9999):04d}"
        )
        self.group_name = "workers"

        # Rate limiting
        self._rate_limits: dict = {}
        self.rate_limit = settings.RATE_LIMIT_PER_MINUTE
        self.rate_window = settings.RATE_LIMIT_WINDOW
        self._rate_limit_cleanup_counter = 0

        # Singleton services
        self._gateway = None
        self._registry = None
        self._v2_engine = None
        self._whatsapp_service = None

        # Per-request services
        self._queue = None
        self._cache = None
        self._context = None

        # RAG refresh scheduler
        self._rag_scheduler: Optional[RAGScheduler] = None

        # Stats
        self._messages_processed = 0
        self._messages_failed = 0
        self._duplicates_skipped = 0
        self._start_time = None

        # Concurrency control
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT)
        self._processing_locks: Dict[str, asyncio.Lock] = {}
        self._lock_access_times: Dict[str, float] = {}
        self._lock_cleanup_interval = 100   # Clean every N messages
        self._lock_max_age_sec = 300         # Remove locks idle > 5 min
        # Guard for the lock-dict mutations themselves. Two concurrent
        # _handle_message coroutines could otherwise race on the
        # `if sender not in self._processing_locks` → insert / evict
        # path and corrupt the dict state. This lock is held only for
        # the brief read-or-create window, never around user work.
        self._lock_dict_guard = asyncio.Lock()

    async def start(self):
        self._start_time = datetime.now(timezone.utc)
        log("info", "worker_starting", {
            "consumer": self.consumer_name,
            "max_concurrent": MAX_CONCURRENT,
        })

        self._setup_signals()

        await self._wait_for_redis()
        await self._wait_for_database()
        await self._init_services()
        await self._create_consumer_group()

        log("info", "worker_ready")

        try:
            # Start RAG scheduler if available
            if self._rag_scheduler:
                await self._rag_scheduler.start()
                log("info", "rag_scheduler_started")

            await asyncio.gather(
                self._process_inbound_loop(),
                self._process_outbound_loop(),
                self._process_delayed_outbound_loop(),
                self._health_reporter(),
                self._shutdown_watcher()
            )
        except asyncio.CancelledError:
            log("info", "worker_cancelled")

    def _setup_signals(self):
        """Setup signal handlers - compatible with Python 3.10+."""
        try:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, self.shutdown.request_shutdown)
            log("info", "signals_installed")
        except NotImplementedError:
            # Windows doesn't support add_signal_handler
            log("warn", "signals_not_supported")

    async def _shutdown_watcher(self):
        await self.shutdown.wait_for_shutdown()
        log("info", "shutdown_initiated")
        await self.shutdown.wait_for_tasks(timeout=100.0)
        await self._cleanup()

    async def _cleanup(self):
        """Graceful shutdown: stop services in reverse-init order.

        Stops health reporter, WhatsApp sender, queue service, Redis,
        and DB connections. Each step is wrapped individually so a failure
        in one does not prevent the rest from cleaning up.
        """
        log("info", "cleanup_started")

        uptime = (datetime.now(timezone.utc) - self._start_time).total_seconds() if self._start_time else 0
        log("info", "final_stats", {
            "processed": self._messages_processed,
            "failed": self._messages_failed,
            "duplicates": self._duplicates_skipped,
            "uptime_seconds": int(uptime)
        })

        # Each step is independent — continue cleanup even if one fails
        try:
            if self._rag_scheduler:
                await self._rag_scheduler.stop()
                log("info", "rag_scheduler_stopped")
        except Exception as e:
            log("warn", "rag_scheduler_stop_failed", {"error": str(e)})

        try:
            if self._gateway:
                await self._gateway.close()
        except Exception as e:
            log("warn", "gateway_close_failed", {"error": str(e)})

        try:
            if self._whatsapp_service:
                await self._whatsapp_service.close()
        except Exception as e:
            log("warn", "whatsapp_service_close_failed", {"error": str(e)})

        try:
            if self.redis:
                await self.redis.aclose()
        except Exception as e:
            log("warn", "redis_close_failed", {"error": str(e)})

        log("info", "worker_stopped")

    async def _connect_redis(self) -> bool:
        """Create Redis connection. Returns True if successful."""
        try:
            if self.redis:
                await self.redis.aclose()
        except (ConnectionError, OSError, RuntimeError) as e:
            log("warn", "redis_old_conn_close_failed", {"error": str(e)})

        try:
            self.redis = aioredis.from_url(
                settings.REDIS_URL,
                encoding="utf-8",
                decode_responses=True,
                max_connections=10,  # Worker: stream + outbound + locks + state. 10 for 5 concurrent tasks.
                socket_keepalive=True,
                health_check_interval=30
            )
            await self.redis.ping()
            return True
        except Exception:
            return False

    async def _reconnect_redis(self):
        """Reconnect to Redis with exponential backoff."""
        log("warn", "redis_reconnecting")
        for attempt in range(5):
            if self.shutdown.is_shutting_down():
                return
            if await self._connect_redis():
                log("info", "redis_reconnected", {"attempt": attempt + 1})
                # Update services with new connection
                if self._queue:
                    self._queue.redis = self.redis
                if self._cache:
                    self._cache.redis = self.redis
                if self._context:
                    self._context.redis = self.redis
                return
            delay = min(REDIS_RETRY_DELAY * (2 ** attempt), 30) + random.uniform(0, 1)
            await asyncio.sleep(delay)
        log("error", "redis_reconnect_failed")

    async def _wait_for_redis(self):
        for attempt in range(REDIS_MAX_RETRIES):
            if self.shutdown.is_shutting_down():
                raise asyncio.CancelledError()

            if await self._connect_redis():
                log("info", "redis_connected")
                return

            delay = min(REDIS_RETRY_DELAY * (2 ** min(attempt, 5)), 60)
            log("warn", "redis_retry", {
                "attempt": attempt + 1,
                "max": REDIS_MAX_RETRIES,
                "next_delay": delay
            })
            await asyncio.sleep(delay)

        raise RuntimeError("Could not connect to Redis")

    async def _wait_for_database(self):
        from database import engine
        from sqlalchemy import text
        from sqlalchemy.exc import OperationalError, DatabaseError

        for attempt in range(REDIS_MAX_RETRIES):
            if self.shutdown.is_shutting_down():
                raise asyncio.CancelledError()

            try:
                async with engine.connect() as conn:
                    await conn.execute(text("SELECT 1"))
                log("info", "database_connected")
                return
            except (OperationalError, DatabaseError, OSError, TimeoutError) as e:
                delay = min(REDIS_RETRY_DELAY * (2 ** min(attempt, 5)), 60) + random.uniform(0, 1)
                log("warn", "database_retry", {
                    "attempt": attempt + 1,
                    "max": REDIS_MAX_RETRIES,
                    "error": str(e),
                    "next_delay": round(delay, 1)
                })
                await asyncio.sleep(delay)
            except Exception as e:
                log("error", "database_unexpected_error", {"error": str(e), "type": type(e).__name__})
                raise

        raise RuntimeError("Could not connect to database")

    async def _init_services(self):
        """Initialize singleton services."""
        log("info", "init_services_started")

        from services.api_gateway import APIGateway
        from services.registry import ToolRegistry
        from services.queue_service import QueueService
        from services.cache_service import CacheService
        from services.context_service import ContextService
        from services.whatsapp_service import WhatsAppService

        self._gateway = APIGateway(redis_client=self.redis)
        self._registry = ToolRegistry(redis_client=self.redis)

        self._queue = QueueService(self.redis)
        self._cache = CacheService(self.redis)
        self._context = ContextService(self.redis)

        self._whatsapp_service = WhatsAppService()
        health = self._whatsapp_service.health_check()
        log("info", "whatsapp_service_init", {"healthy": health["healthy"]})

        swagger_sources = settings.swagger_sources
        if not swagger_sources:
            log("warn", "no_swagger_sources")
        else:
            log("info", "registry_init", {"sources": len(swagger_sources)})
            success = await self._registry.initialize(swagger_sources)

            if success:
                tools_count = len(self._registry.tools)
                log("info", "registry_ready", {"tools": tools_count})

                from services.api_capabilities import initialize_capability_registry
                capability_registry = await initialize_capability_registry(self._registry)
                log("info", "capabilities_ready", {
                    "capabilities": len(capability_registry.capabilities)
                })
            else:
                log("error", "registry_failed")
                raise RuntimeError("Tool Registry initialization failed")

        # V2Engine — primary (and only) request-path engine since v1
        # delete 2026-05-08. Init failure NOW blocks boot — there is no
        # v1 fallback. If V2Engine can't construct, the worker stops so
        # the bot is never half-broken.
        log("info", "v2_engine_init")
        from services.v2.engine import make_v2_engine_for_production
        bundle = await make_v2_engine_for_production(
            redis_client=self.redis,
            gateway=self._gateway,
            tool_registry=self._registry,
            settings=settings,
        )
        self._v2_engine = bundle.engine
        log("info", "v2_engine_ready")

        # Register Lua script for atomic lock release
        self._release_lock_sha = await self.redis.script_load(_RELEASE_LOCK_LUA)
        log("info", "lock_lua_script_registered")

        # Initialize RAG scheduler for periodic embedding refresh
        try:
            self._rag_scheduler = await get_rag_scheduler(self.redis)
            log("info", "rag_scheduler_initialized")
        except Exception as e:
            log("warn", "rag_scheduler_init_failed", {"error": str(e)})

        # ML warm-up removed in Phase D (ML cull).

    async def _create_consumer_group(self):
        try:
            # Use "0" to read ALL messages in the stream (including those
            # that arrived while no worker was running). Using "$" would
            # silently skip any messages pushed before the group existed.
            await self.redis.xgroup_create(
                "whatsapp_stream_inbound",
                self.group_name,
                "0",
                mkstream=True
            )
            log("info", "consumer_group_created", {"group": self.group_name})
        except ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise
            log("info", "consumer_group_exists", {"group": self.group_name})

        # Clean up zombie consumers and reclaim their stuck messages
        await self._cleanup_stale_consumers()

        # Clean up stale pending messages from previous worker runs
        await self._cleanup_stale_pending()

    async def _cleanup_stale_consumers(self):
        """
        Remove zombie consumers from previous worker restarts.

        Each restart creates a new consumer (worker_{ts}_{pid}_{rand}) but
        never removes the old one. Before removing a stale consumer that has
        pending messages, reclaim those messages so they get reprocessed.

        EDGE-5 fix (Filip 2026-05-20): when 2 workers boot concurrently, both
        would run cleanup + xautoclaim and could reclaim the SAME messages
        into two consumers (double-processing). A Redis SET NX lock serializes
        cleanup so only one worker does it at a time. Lock auto-expires (60s)
        so a crashed worker doesn't deadlock cleanup forever.
        """
        lock_key = "worker:cleanup_lock"
        got_lock = False
        try:
            got_lock = await self.redis.set(lock_key, self.consumer_name, nx=True, ex=60)
            if not got_lock:
                log("info", "cleanup_skipped_locked")
                return
        except (ResponseError, RedisConnectionError, RedisError) as e:
            # If lock acquisition fails, proceed without it (fail-open) — the
            # message-level idempotency lock still prevents double-processing.
            log("warn", "cleanup_lock_failed", {"error": str(e)})

        try:
            consumers = await self.redis.xinfo_consumers(
                "whatsapp_stream_inbound",
                self.group_name
            )

            if not consumers or len(consumers) <= 1:
                log("info", "no_stale_consumers")
                return

            removed = 0
            reclaimed = 0
            for consumer in consumers:
                name = consumer.get("name", "")
                pending = consumer.get("pending", 0)
                idle = consumer.get("idle", 0)

                # Skip current consumer
                if name == self.consumer_name:
                    continue

                # Only touch consumers idle for more than 5 minutes
                if idle <= 300000:
                    continue

                # Reclaim pending messages from zombie consumer before removing
                if pending > 0:
                    try:
                        result = await self.redis.xautoclaim(
                            "whatsapp_stream_inbound",
                            self.group_name,
                            self.consumer_name,
                            min_idle_time=300000,
                            start_id="0",
                            count=pending
                        )
                        claimed_msgs = result[1] if len(result) > 1 else []
                        reclaimed += len(claimed_msgs)
                    except (ResponseError, RedisConnectionError, RedisError) as e:
                        log("warn", "xautoclaim_stale_failed", {"consumer": name, "error": str(e)})

                try:
                    await self.redis.xgroup_delconsumer(
                        "whatsapp_stream_inbound",
                        self.group_name,
                        name
                    )
                    removed += 1
                except (ResponseError, RedisConnectionError, RedisError) as e:
                    log("warn", "delconsumer_failed", {"consumer": name, "error": str(e)})

            if removed or reclaimed:
                log("info", "stale_consumers_removed", {
                    "removed": removed,
                    "reclaimed": reclaimed,
                    "total_before": len(consumers),
                    "remaining": len(consumers) - removed
                })

        except (ConnectionError, TimeoutError, RedisError) as e:
            log("warn", "consumer_cleanup_failed", {"error": str(e)})
        finally:
            # EDGE-5: release cleanup lock if we hold it. Best-effort —
            # 60s TTL guarantees release even if this fails.
            if got_lock:
                with suppress(Exception):
                    await self.redis.delete("worker:cleanup_lock")

    async def _cleanup_stale_pending(self):
        """
        Reclaim stale pending messages from previous worker runs.

        On restart, messages claimed by dead consumers are reassigned to
        this worker via XAUTOCLAIM so they get reprocessed (not deleted).
        Only truly old messages (>10 min) are ACKed and removed.
        """
        try:
            pending = await self.redis.xpending(
                "whatsapp_stream_inbound",
                self.group_name
            )

            total_pending = pending.get("pending", 0) if isinstance(pending, dict) else (pending[0] if pending else 0)

            if not total_pending:
                log("info", "no_stale_pending")
                return

            log("warn", "stale_pending_found", {"count": total_pending})

            # Reclaim messages idle > 5 min to this consumer for reprocessing
            try:
                result = await self.redis.xautoclaim(
                    "whatsapp_stream_inbound",
                    self.group_name,
                    self.consumer_name,
                    min_idle_time=300000,
                    start_id="0",
                    count=100
                )
                claimed_msgs = result[1] if len(result) > 1 else []
                if claimed_msgs:
                    log("info", "stale_pending_reclaimed", {"count": len(claimed_msgs)})
            except (ResponseError, RedisConnectionError, RedisError) as e:
                log("warn", "xautoclaim_failed", {"error": str(e)})

            # Only delete messages pending > 10 minutes (likely unrecoverable)
            detailed = await self.redis.xpending_range(
                "whatsapp_stream_inbound",
                self.group_name,
                min="-",
                max="+",
                count=100
            )

            cleaned = 0
            for entry in detailed:
                msg_id = entry.get("message_id") or (entry[0] if isinstance(entry, (list, tuple)) else None)
                idle_time = entry.get("time_since_delivered") or (entry[3] if isinstance(entry, (list, tuple)) and len(entry) > 3 else 0)
                if not msg_id:
                    continue

                # Only remove messages stuck for more than 10 minutes
                if idle_time > 600000:
                    try:
                        await self.redis.xack("whatsapp_stream_inbound", self.group_name, msg_id)
                        await self.redis.xdel("whatsapp_stream_inbound", msg_id)
                        cleaned += 1
                    except (ConnectionError, OSError) as e:
                        log("warn", "stale_msg_cleanup_failed", {"msg_id": msg_id, "error": str(e)})

            if cleaned:
                log("info", "stale_pending_cleaned", {"cleaned": cleaned, "total": total_pending})

        except (ConnectionError, TimeoutError, RedisError) as e:
            log("warn", "stale_cleanup_failed", {"error": str(e)})

    async def _process_inbound_loop(self):
        """Main inbound loop: read Redis Stream → dispatch to _handle_message().

        Reads up to MAX_CONCURRENT messages per iteration via XREADGROUP,
        dispatches each as a semaphore-guarded async task, and periodically
        cleans stale locks. Exits on shutdown signal or burst mode limits.
        """
        log("info", "inbound_processor_started")

        while not self.shutdown.is_shutting_down():
            try:
                messages = await self._queue.read_stream(
                    group=self.group_name,
                    consumer=self.consumer_name,
                    count=MAX_CONCURRENT,
                    block=STREAM_BLOCK_MS
                )

                if not messages:
                    continue

                tasks = [
                    asyncio.create_task(self._handle_message_safe(msg_id, data))
                    for msg_id, data in messages
                ]
                for task in tasks:
                    self.shutdown.track_task(task)

                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)

                # Periodic cleanup of stale local locks to prevent memory leaks
                total = self._messages_processed + self._messages_failed
                if total > 0 and total % self._lock_cleanup_interval == 0:
                    self._cleanup_stale_locks()

            except asyncio.CancelledError:
                break
            except (ConnectionError, OSError) as e:
                log("warn", "redis_connection_lost_inbound", {"error": str(e)})
                await self._reconnect_redis()
                await asyncio.sleep(2)
            except Exception as e:
                log_exception("inbound_loop_error", e)
                await asyncio.sleep(2)

    async def _release_lock_lua(self, lock_key: str) -> int:
        """Fallback: run Lua script inline if SHA not cached."""
        return await self.redis.eval(_RELEASE_LOCK_LUA, 1, lock_key, self.consumer_name)

    def _cleanup_stale_locks(self) -> None:
        """Remove processing locks idle for more than _lock_max_age_sec."""
        now = time.time()
        stale_keys = [
            k for k, t in self._lock_access_times.items()
            if now - t > self._lock_max_age_sec
        ]
        for k in stale_keys:
            self._processing_locks.pop(k, None)
            self._lock_access_times.pop(k, None)
        if stale_keys:
            log("debug", "processing_locks_cleaned", {"removed": len(stale_keys), "remaining": len(self._processing_locks)})

    async def _handle_message_safe(self, msg_id: str, data: dict):
        async with self._semaphore:
            await self._handle_message(msg_id, data)

    async def _acquire_message_lock(self, sender: str, message_id: str) -> bool:
        """Acquire distributed lock to prevent double execution."""
        lock_key = f"msg_lock:{sender}:{message_id}"

        try:
            acquired = await self.redis.set(
                lock_key,
                self.consumer_name,
                nx=True,
                ex=MESSAGE_LOCK_TTL
            )

            if acquired:
                self._lock_access_times[lock_key] = time.time()
                return True
            else:
                holder = await self.redis.get(lock_key)
                log("warn", "duplicate_detected", {
                    "lock_key": lock_key,
                    "holder": holder
                })
                return False

        except (ConnectionError, TimeoutError, OSError) as e:
            log("error", "lock_error", {"error": str(e)})
            return True  # Fail open — allow processing rather than dropping message
        except Exception as e:
            log("error", "lock_unexpected_error", {"error": str(e), "type": type(e).__name__})
            return True  # Fail open

    async def _release_message_lock(self, sender: str, message_id: str) -> None:
        """Atomically release a distributed message lock via Lua script.

        Uses EVALSHA to execute the cached Lua script that only DELETEs the key
        if the caller (consumer_name) still holds it — prevents releasing a lock
        that was already stolen by another consumer after TTL expiry.
        Fails open: errors are logged but never raised.
        """
        lock_key = f"msg_lock:{sender}:{message_id}"
        try:
            # Atomic release: only delete if we still hold the lock
            released = await self.redis.evalsha(
                self._release_lock_sha,
                1, lock_key,
                self.consumer_name
            ) if hasattr(self, '_release_lock_sha') else await self._release_lock_lua(lock_key)
            if released == 0:
                log("debug", "lock_already_expired_or_stolen", {"lock_key": lock_key})
        except (ConnectionError, TimeoutError, OSError) as e:
            log("warn", "lock_release_error", {"error": str(e)})
        except Exception as e:
            log("warn", "lock_release_unexpected", {"error": str(e), "type": type(e).__name__})
        finally:
            self._lock_access_times.pop(lock_key, None)

    async def _handle_message(self, msg_id: str, data: dict):
        """Handle single message with deduplication and distributed tracing."""
        sender = data.get("sender", "")
        text = data.get("text", "")
        message_id = data.get("message_id", "")
        request_id = data.get("request_id", "")  # Propagated from webhook
        # Option C: webhook resolved phone→tenant via YAML map. Worker writes
        # it to a per-session Redis key BEFORE engine runs so V6.2 precedence
        # in api_gateway picks it up via user_context["tenant_id"].
        tenant_id = data.get("tenant_id", "")
        if tenant_id and sender:
            try:
                await self.redis.set(
                    f"session:{sender}:tenant_id", tenant_id, ex=3600
                )
            except Exception as redis_err:
                log("warn", "tenant_session_write_failed", {
                    "sender": sender[-4:] if sender else "",
                    "error": str(redis_err),
                })

        _span_attrs = {
            "message.id": message_id[:20] if message_id else "",
            "message.sender_suffix": sender[-4:] if sender else "",
            "message.stream_id": msg_id,
            "message.request_id": request_id,
        }

        if not message_id:
            content_hash = hashlib.md5(
                f"{sender}:{text}".encode(), usedforsecurity=False
            ).hexdigest()[:16]
            message_id = f"hash_{content_hash}"

        log("info", "processing", {
            "sender": sender[-4:] if sender else "",
            "text_len": len(text) if text else 0,
            "request_id": request_id,
        })

        # IDEMPOTENCY: Lock FIRST, before ANY processing (including non-text).
        # If Infobip retries the same message_id due to 500ms lag, we must
        # detect the duplicate before sending a second response.
        if not await self._acquire_message_lock(sender, message_id):
            self._duplicates_skipped += 1
            err = ConversationError(ErrorCode.DUPLICATE_MESSAGE, f"Duplicate message {message_id[:20]} from {sender[-4:]}")
            log("warn", "skipping_duplicate", {
                "sender": sender[-4:],
                "message_id": message_id[:20],
                "error_code": err.code.value,
            })
            await self._ack_message(msg_id)
            return

        # Handle non-text messages (images, locations, voice, etc.)
        # These come from webhook as [NON_TEXT:TYPE] markers
        # Placed AFTER lock to prevent duplicate responses on Infobip retries
        if text.startswith("[NON_TEXT:"):
            original_type = data.get("original_type", "UNKNOWN")
            err = ConversationError(ErrorCode.UNSUPPORTED_MEDIA, f"Unsupported media type: {original_type}")
            log("info", "non_text_message", {
                "sender": sender[-4:] if sender else "",
                "type": original_type,
                "error_code": err.code.value,
            })
            response = (
                "Trenutno mogu obraditi samo tekstualne poruke. "
                "Molimo pošaljite svoju poruku kao tekst."
            )
            await self._enqueue_outbound(sender, response)
            self._messages_processed += 1
            await self._release_message_lock(sender, message_id)
            await self._ack_message(msg_id)
            return

        if not self._check_rate_limit(sender):
            log("warn", "rate_limited", {"sender": sender[-4:]})
            await self._enqueue_outbound(sender, "Šaljete previše poruka. Molimo pričekajte kratko pa pokušajte ponovo.")
            await self._release_message_lock(sender, message_id)
            await self._ack_message(msg_id)
            return

        start_time = time.time()

        # Per-sender lock: serialize processing for same user within this pod.
        # Prevents conversation state race when user sends rapid messages.
        # FIX (race audit): get-or-create + cap-eviction is wrapped in a
        # single async lock so two concurrent coroutines can't both pass
        # the membership check, both insert, or evict mid-iteration.
        async with self._lock_dict_guard:
            if sender not in self._processing_locks:
                if len(self._processing_locks) >= 10000:
                    oldest = min(
                        self._lock_access_times,
                        key=self._lock_access_times.get,
                    )
                    del self._processing_locks[oldest]
                    del self._lock_access_times[oldest]
                self._processing_locks[sender] = asyncio.Lock()
            self._lock_access_times[sender] = time.time()
            sender_lock = self._processing_locks[sender]

        # ACK PROTOCOL: XACK only after outbound enqueue succeeds.
        # If we ACK before enqueue and the pod dies, the user's reply
        # is lost permanently (message already removed from stream).
        # The outbound queue has its own crash recovery via BLMOVE +
        # _requeue_abandoned_outbound(), so once RPUSH succeeds the
        # reply is durable.
        ack_ok = False

        async with sender_lock:
          with trace_span(_tracer, "worker.process_message", _span_attrs) as span:
            try:
              # Timeout protection: prevent stuck LLM calls from blocking all slots
              response = await asyncio.wait_for(
                  self._process_message(sender, text, message_id, tenant_id=tenant_id),
                  timeout=90.0
              )

              # Fallback if engine returns None/empty
              if not response:
                  response = "Greška pri obradi poruke. Molimo pokušajte ponovno."
                  log("warn", "empty_response_fallback", {"sender": sender[-4:]})

              span.set_attribute("response.length", len(response))
              await self._enqueue_outbound(sender, response)
              ack_ok = True  # Outbound enqueue succeeded — safe to ACK

              self._messages_processed += 1

            except asyncio.TimeoutError:
              log("error", "process_timeout", {
                  "sender": sender[-4:] if sender else "",
                  "text_len": len(text) if text else 0,
                  "timeout_sec": 90,
                  "request_id": request_id,
              })
              self._messages_failed += 1
              await self._enqueue_outbound(
                  sender,
                  "Obrada poruke je trajala predugo. Molimo pokušajte ponovno."
              )
              ack_ok = True  # Timeout response enqueued — safe to ACK

            except Exception as e:
              span.record_exception(e)
              log_exception("processing_error", e, {
                  "sender": sender[-4:] if sender else "",
                  "text_len": len(text) if text else 0,
                  "message_id": message_id[:20] if message_id else "",
                  "request_id": request_id,
              })
              self._messages_failed += 1
              await self._store_dlq(data, str(e))
              try:
                  await self._enqueue_outbound(
                      sender,
                      "Došlo je do greške pri obradi vaše poruke. Molimo pokušajte ponovno."
                  )
              except Exception:
                  log("warn", "error_response_failed", {"sender": f"***{sender[-4:]}"})
              ack_ok = True  # DLQ write succeeded — safe to ACK

            finally:
              await self._release_message_lock(sender, message_id)
              if ack_ok:
                  await self._ack_message(msg_id)
              else:
                  # Enqueue failed (Redis down mid-processing). Do NOT ACK.
                  # Message stays pending in stream → reclaimed on restart
                  # via _cleanup_stale_pending().
                  log("warn", "ack_deferred_enqueue_failed", {
                      "msg_id": msg_id,
                      "sender": sender[-4:] if sender else ""
                  })
              elapsed = time.time() - start_time
              log("info", "processed", {
                  "elapsed_ms": int(elapsed * 1000),
                  "request_id": request_id,
              })

    async def _process_message(
        self,
        sender: str,
        text: str,
        message_id: str,
        tenant_id: str = "",
    ) -> Optional[str]:
        """Process message through V2Engine.

        v1 stack deleted 2026-05-08. V2Engine is the only engine — there
        is no fallback. If V2Engine raises, the error propagates to the
        caller which enqueues a generic Croatian error reply.

        `db_session` and `tenant_id` are kept on the function signature
        for symmetry with the prior MessageEngine.process() contract,
        but V2Engine routes all DB/tenant concerns through its own
        identity context, so they aren't passed down.
        """
        try:
            return await self._v2_engine.process_message(sender, text)
        except Exception as e:
            log_exception("v2_engine_error", e, {
                "sender": sender[-4:] if sender else "",
                "text_len": len(text) if text else 0,
            })
            raise

    async def _requeue_abandoned_outbound(self):
        """Re-queue messages left in processing list from previous crashes."""
        try:
            count = await self.redis.llen("whatsapp_outbound_processing")
            if count > 0:
                log("warn", "requeue_abandoned_outbound", {"count": count})
                requeued = 0
                while True:
                    item = await self.redis.lmove(
                        "whatsapp_outbound_processing",
                        "whatsapp_outbound",
                        src="RIGHT",
                        dest="LEFT"
                    )
                    if not item:
                        break
                    requeued += 1
                log("info", "requeued_abandoned_outbound", {"requeued": requeued})
        except (ResponseError, RedisConnectionError, RedisError) as e:
            log("warn", "requeue_abandoned_failed", {"error": str(e)})

    async def _process_outbound_loop(self):
        """Main outbound loop: pop from Redis List → send via WhatsApp API.

        Uses LMOVE to atomically move messages from outbound to a processing list,
        preventing message loss on crash. Successfully sent messages are removed
        from the processing list; failed ones are retried or DLQ'd.
        """
        log("info", "outbound_processor_started")

        # Re-queue any messages abandoned by a previous crash
        await self._requeue_abandoned_outbound()

        while not self.shutdown.is_shutting_down():
            try:
                # Atomically move from outbound to processing list
                # If we crash after this but before send, the message
                # survives in whatsapp_outbound_processing and gets
                # re-queued on next startup.
                data = await self.redis.blmove(
                    "whatsapp_outbound",
                    "whatsapp_outbound_processing",
                    timeout=1,
                    src="LEFT",
                    dest="RIGHT"
                )
                if not data:
                    continue

                payload = json.loads(data)

                # Idempotency check: skip if already sent (crash recovery scenario)
                idem_key = payload.get("idempotency_key", "")
                if idem_key:
                    already_sent = await self.redis.get(f"sent:{idem_key}")
                    if already_sent:
                        log("warn", "skipping_already_sent", {"key": idem_key[:30]})
                        await self.redis.lrem("whatsapp_outbound_processing", 1, data)
                        continue

                await self._send_whatsapp(
                    to=payload.get("to"), text=payload.get("text"),
                    attempt=payload.get("attempt", 0),
                )

                # Mark as sent for idempotency (TTL 10 min)
                if idem_key:
                    await self.redis.setex(f"sent:{idem_key}", 600, "1")

                # Successfully sent - remove from processing list
                await self.redis.lrem("whatsapp_outbound_processing", 1, data)

            except asyncio.CancelledError:
                break
            except (ConnectionError, OSError) as e:
                log("warn", "redis_connection_lost_outbound", {"error": str(e)})
                await self._reconnect_redis()
                await asyncio.sleep(1)
            except json.JSONDecodeError as e:
                log("error", "outbound_json_error", {"error": str(e)})
                # Remove corrupt message from processing list
                if data:
                    with suppress(Exception):
                        await self.redis.lrem("whatsapp_outbound_processing", 1, data)
                await asyncio.sleep(1)
            except Exception as e:
                log_exception("outbound_error", e)
                await asyncio.sleep(1)

    async def _health_reporter(self):
        """Periodic health reporting loop (runs every HEALTH_REPORT_INTERVAL seconds).

        Publishes worker stats (processed/failed/duplicates, memory, uptime) to logs.
        Also verifies the Lua lock-release script is still cached in Redis — if Redis
        flushed scripts (SCRIPT FLUSH), the SHA is automatically reloaded here.
        Emits a warning with tracemalloc top-5 allocators when memory exceeds threshold.
        """
        while not self.shutdown.is_shutting_down():
            try:
                await asyncio.sleep(HEALTH_REPORT_INTERVAL)

                if self.shutdown.is_shutting_down():
                    break

                active = len(self.shutdown.active_tasks)
                memory_mb = get_memory_usage_mb()

                whatsapp_stats = {}
                if self._whatsapp_service:
                    whatsapp_stats = self._whatsapp_service.get_stats()

                # Calculate uptime
                uptime_sec = 0
                if self._start_time:
                    uptime_sec = int((datetime.now(timezone.utc) - self._start_time).total_seconds())

                # Verify Lua lock script is still cached in Redis
                lua_cached = True
                if hasattr(self, '_release_lock_sha'):
                    exists = await self.redis.script_exists(self._release_lock_sha)
                    if not exists or not exists[0]:
                        self._release_lock_sha = await self.redis.script_load(_RELEASE_LOCK_LUA)
                        log("warn", "lua_script_reloaded", {"sha": self._release_lock_sha})
                        lua_cached = False

                # EDGE-4 (Filip 2026-05-20): surface DLQ depth so dead-letter
                # queues aren't a silent black hole. We do NOT auto-retry
                # inbound DLQ (poison-message risk — a msg that crashed the
                # engine would crash it again). Instead make depth visible so
                # the weekly active-learning report + ops can inspect/replay
                # manually. Warn when either DLQ grows past threshold.
                dlq_inbound = 0
                dlq_outbound = 0
                with suppress(Exception):
                    dlq_inbound = await self.redis.llen("dlq:inbound")
                    dlq_outbound = await self.redis.llen("dlq:outbound")

                health_data = {
                    "processed": self._messages_processed,
                    "failed": self._messages_failed,
                    "duplicates": self._duplicates_skipped,
                    "active_tasks": active,
                    "tools": len(self._registry.tools) if self._registry else 0,
                    "wa_sent": whatsapp_stats.get("messages_sent", 0),
                    "wa_retries": whatsapp_stats.get("total_retries", 0),
                    "memory_mb": round(memory_mb, 1),
                    "uptime_sec": uptime_sec,
                    "lua_lock_cached": lua_cached,
                    "local_locks": len(self._lock_access_times),
                    "dlq_inbound": dlq_inbound,
                    "dlq_outbound": dlq_outbound,
                }

                # Alert if DLQ is accumulating — signals systemic failure
                # (engine bug, Infobip outage) needing manual inspection.
                if dlq_inbound > 50 or dlq_outbound > 50:
                    log("warn", "dlq_growing", {
                        "dlq_inbound": dlq_inbound,
                        "dlq_outbound": dlq_outbound,
                        "action": "inspect dlq:inbound / dlq:outbound in Redis",
                    })

                # Warn if memory is high; include tracemalloc top allocators
                if memory_mb > MEMORY_WARNING_MB:
                    if tracemalloc is not None and tracemalloc.is_tracing():
                        snapshot = tracemalloc.take_snapshot()
                        top = snapshot.statistics("lineno")[:5]
                        health_data["tracemalloc_top5"] = [
                            f"{s.traceback[0]}: {s.size / 1024:.0f}KB" for s in top
                        ]
                    log("warn", "high_memory_usage", health_data)
                else:
                    log("info", "health", health_data)

            except asyncio.CancelledError:
                break

    # EDGE-2 (Filip 2026-05-20): outbound send resiliency.
    # Permanent errors never succeed on retry → straight to DLQ.
    # Transient errors (timeout, 5xx, conn reset, RATE_LIMIT) → delayed
    # retry with exponential backoff, capped at MAX_SEND_ATTEMPTS, then DLQ.
    # Before this, only RATE_LIMIT re-queued — every other failure silently
    # dropped the reply, so the user never learned their action's outcome.
    MAX_SEND_ATTEMPTS = 3
    _PERMANENT_SEND_ERRORS = frozenset({
        "INVALID_PHONE", "INVALID_RECIPIENT", "INVALID_NUMBER",
        "AUTH", "UNAUTHORIZED", "FORBIDDEN", "BLOCKED",
    })

    async def _send_whatsapp(self, to: str, text: str, attempt: int = 0):
        """Send WhatsApp message via WhatsAppService.

        attempt: 0-based retry counter (carried through the delayed-outbound
        queue). On transient failure we re-enqueue with attempt+1 until
        MAX_SEND_ATTEMPTS, then park in the outbound DLQ.
        """
        if not self._whatsapp_service:
            log("warn", "whatsapp_not_initialized")
            return

        result = await self._whatsapp_service.send(to, text)

        if result.success:
            log("info", "sent", {
                "to": to[-4:] if to else "",
                "message_id": result.message_id,
                "attempt": attempt,
            })
            return

        log("error", "send_failed", {
            "error_code": result.error_code,
            "error": result.error_message,
            "attempt": attempt,
        })

        # Permanent errors: retry would never succeed → DLQ immediately.
        if result.error_code in self._PERMANENT_SEND_ERRORS:
            await self._store_outbound_dlq(to, text, result.error_code, attempt)
            return

        # Transient (RATE_LIMIT, timeout, 5xx, connection reset): retry with
        # backoff until the cap, then DLQ so the reply isn't silently lost.
        if attempt + 1 >= self.MAX_SEND_ATTEMPTS:
            log("error", "send_exhausted", {
                "to": to[-4:] if to else "",
                "error_code": result.error_code,
                "attempts": attempt + 1,
            })
            await self._store_outbound_dlq(to, text, result.error_code, attempt)
            return

        # RATE_LIMIT honors server retry_after; others use exponential backoff.
        if result.error_code == "RATE_LIMIT":
            delay = result.retry_after or 30
        else:
            delay = 5 * (2 ** attempt)  # 5s, 10s, 20s
        await self._enqueue_outbound_delayed(
            to, text, delay=delay, attempt=attempt + 1,
        )

    async def _store_outbound_dlq(
        self, to: str, text: str, error_code: str, attempt: int,
    ) -> None:
        """Park an undeliverable outbound reply in the outbound DLQ.
        Full text retained (access-controlled recovery store, 7d TTL)."""
        try:
            entry = json.dumps({
                "dlq": "outbound",
                "to": to,
                "text": text,
                "error_code": error_code,
                "attempts": attempt + 1,
                "time": datetime.now(timezone.utc).isoformat(),
            })
            await self.redis.rpush("dlq:outbound", entry)
            await self.redis.expire("dlq:outbound", 604800)  # 7d
            log("error", "outbound_dlq_stored", {
                "to": to[-4:] if to else "",
                "error_code": error_code,
            })
        except (ResponseError, RedisConnectionError, RedisError) as e:
            log("error", "outbound_dlq_failed", {"error": str(e)})

    async def _enqueue_outbound_delayed(
        self, to: str, text: str, delay: int = 30, attempt: int = 0,
    ):
        """Enqueue outbound message with delay for rate limiting / retry.

        attempt: carried through so _send_whatsapp can enforce the retry cap
        when this message is eventually re-sent (EDGE-2)."""
        try:
            delayed_payload = json.dumps({
                "to": to,
                "text": text,
                "attempt": attempt,
                "idempotency_key": f"{to}:{hashlib.md5(text.encode(), usedforsecurity=False).hexdigest()[:12]}:{int(time.time() + delay)}",
                "scheduled_at": time.time() + delay
            })

            await self.redis.zadd(
                "whatsapp_outbound_delayed",
                {delayed_payload: time.time() + delay}
            )

            log("info", "queued_delayed", {"to": to[-4:], "delay": delay})
        except (ResponseError, RedisConnectionError, RedisError) as e:
            log("error", "queue_delayed_failed", {"error": str(e)})

    async def _process_delayed_outbound_loop(self):
        """Poll whatsapp_outbound_delayed sorted set and move due messages to outbound.

        Uses a Lua script to atomically pop + push, preventing the race where
        two workers both fetch the same due items via zrangebyscore.
        """
        log("info", "delayed_outbound_processor_started")
        while not self.shutdown.is_shutting_down():
            try:
                now = time.time()
                # Atomic: zrangebyscore + rpush + zrem in one round-trip
                promoted = await self.redis.eval(
                    _PROMOTE_DELAYED_LUA,
                    2,  # number of KEYS
                    "whatsapp_outbound_delayed",  # KEYS[1]
                    "whatsapp_outbound",           # KEYS[2]
                    str(now),                      # ARGV[1] = score ceiling
                    "10",                          # ARGV[2] = batch size
                )
                for raw in (promoted or []):
                    try:
                        payload = json.loads(raw)
                        log("info", "delayed_msg_promoted", {"to": payload.get("to", "?")[-4:]})
                    except (json.JSONDecodeError, KeyError) as e:
                        log("warn", "delayed_msg_corrupt", {"error": str(e)})

                await asyncio.sleep(5)  # Poll every 5 seconds
            except asyncio.CancelledError:
                break
            except (ConnectionError, OSError) as e:
                log("warn", "delayed_outbound_redis_error", {"error": str(e)})
                await asyncio.sleep(10)

    async def _enqueue_outbound(self, to: str, text: str):
        # Split long messages to respect WhatsApp's 4096 char limit.
        # A5 fix: batch multi-chunk rpush into ONE round-trip — was
        # N synchronous awaits per long message.
        MAX_WA_LENGTH = 4000  # Leave margin for encoding overhead
        ts_ns = time.time_ns()
        if len(text) > MAX_WA_LENGTH:
            chunks = self._split_message(text, MAX_WA_LENGTH)
            log("info", "message_split", {"chunks": len(chunks), "original_len": len(text)})
            payloads = [
                json.dumps({
                    "to": to,
                    "text": chunk,
                    "idempotency_key": (
                        f"{to}:"
                        f"{hashlib.md5(chunk.encode(), usedforsecurity=False).hexdigest()[:12]}"
                        f":{ts_ns}:{idx}"
                    ),
                })
                for idx, chunk in enumerate(chunks)
            ]
            # Single rpush with multiple values — 1 redis RTT vs N.
            await self.redis.rpush("whatsapp_outbound", *payloads)
        else:
            payload = json.dumps({
                "to": to,
                "text": text,
                "idempotency_key": (
                    f"{to}:"
                    f"{hashlib.md5(text.encode(), usedforsecurity=False).hexdigest()[:12]}"
                    f":{ts_ns}"
                ),
            })
            await self.redis.rpush("whatsapp_outbound", payload)

    @staticmethod
    def _split_message(text: str, max_length: int) -> list:
        """Naive split into <= max_length chunks. LLM formatter caps responses
        at 500 tokens (~2 KB) so chunking is almost always a no-op; only edge
        cases (huge list dumps) ever hit max_length."""
        if len(text) <= max_length:
            return [text]
        return [text[i:i + max_length] for i in range(0, len(text), max_length)]

    async def _ack_message(self, msg_id: str):
        with suppress(Exception):
            await self.redis.xack("whatsapp_stream_inbound", self.group_name, msg_id)
            await self.redis.xdel("whatsapp_stream_inbound", msg_id)

    async def _store_dlq(self, data: dict, error: str):
        entry = {
            "original": data,
            "error": error,
            "time": datetime.now(timezone.utc).isoformat(),
            "worker": self.consumer_name
        }
        await self.redis.rpush("dlq:inbound", json.dumps(entry))
        # Set TTL on DLQ to prevent unbounded memory growth (7 days retention)
        await self.redis.expire("dlq:inbound", 604800)

    MAX_RATE_LIMIT_ENTRIES = 10000  # Prevent unbounded memory growth

    def _check_rate_limit(self, identifier: str) -> bool:
        """Check rate limit with periodic cleanup and bounded dict size."""
        now = time.time()
        window_start = now - self.rate_window

        self._rate_limit_cleanup_counter += 1
        if self._rate_limit_cleanup_counter >= 100:
            self._rate_limit_cleanup_counter = 0
            # Clean stale entries (use list() to avoid dict mutation during iteration)
            stale_keys = [
                k for k, v in list(self._rate_limits.items())
                if not v or max(v) < window_start
            ]
            for k in stale_keys:
                self._rate_limits.pop(k, None)

            # Evict oldest entries if dict exceeds size limit
            if len(self._rate_limits) > self.MAX_RATE_LIMIT_ENTRIES:
                sorted_keys = sorted(
                    self._rate_limits.keys(),
                    key=lambda k: max(self._rate_limits[k]) if self._rate_limits[k] else 0
                )
                for k in sorted_keys[:len(self._rate_limits) - self.MAX_RATE_LIMIT_ENTRIES]:
                    self._rate_limits.pop(k, None)

        if identifier in self._rate_limits:
            self._rate_limits[identifier] = [
                t for t in self._rate_limits[identifier]
                if t > window_start
            ]
        else:
            self._rate_limits[identifier] = []

        if len(self._rate_limits[identifier]) >= self.rate_limit:
            return False

        self._rate_limits[identifier].append(now)
        return True


async def main():
    worker = Worker()

    try:
        await worker.start()
    except asyncio.CancelledError:
        log("info", "worker_cancelled")
    except Exception as e:
        log_exception("worker_fatal", e)
        sys.exit(1)
    finally:
        # Safety net: ensure resources are released even if start() exits abnormally
        if worker.redis:
            with suppress(Exception):
                await worker.redis.aclose()
        log("info", "worker_exit")


if __name__ == "__main__":
    asyncio.run(main())
