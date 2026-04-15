"""
Cache Service

Redis caching layer with resilience.
NO DEPENDENCIES on other services.

Phase 4:
- Custom JSON serializer for datetime/UUID
- set_json() with explicit JSON handling
- invalidate() method for cache busting
- Fail-Open design (returns None/False, never crashes)
"""

import asyncio
import json
import logging
import uuid
from datetime import datetime, date
from typing import Any, Optional, Callable, Awaitable
from uuid import UUID

logger = logging.getLogger(__name__)

class SafeJSONEncoder(json.JSONEncoder):
    """
    Custom JSON encoder for datetime and UUID objects.

    Handles:
    - datetime -> ISO format string
    - date -> ISO format string
    - UUID -> string
    - Other objects -> str() fallback
    """

    def default(self, obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, date):
            return obj.isoformat()
        if isinstance(obj, UUID):
            return str(obj)
        # Fallback for unknown types
        try:
            return str(obj)
        except Exception:
            return super().default(obj)

class CacheService:
    """Redis cache wrapper."""

    def __init__(self, redis_client) -> None:
        """
        Initialize cache service.

        Args:
            redis_client: Redis async client
        """
        self.redis = redis_client

    async def get(self, key: str) -> Optional[str]:
        """Get value from cache."""
        try:
            return await self.redis.get(key)
        except Exception as e:
            logger.warning(f"Cache get failed: {e}")
            return None

    async def get_json(self, key: str) -> Optional[Any]:
        """Get JSON value from cache."""
        try:
            data = await self.redis.get(key)
            if data is not None:
                return json.loads(data)
            return None
        except Exception as e:
            logger.warning(f"Cache get_json failed: {e}")
            return None

    async def set(self, key: str, value: Any, ttl: int = 300) -> bool:
        """
        Set value with TTL.

        Args:
            key: Cache key
            value: Value to store (will be JSON encoded if not string)
            ttl: Time to live in seconds

        Returns:
            True if successful
        """
        try:
            if not isinstance(value, (str, bytes)):
                value = json.dumps(value, cls=SafeJSONEncoder)
            await self.redis.setex(key, ttl, value)
            return True
        except Exception as e:
            logger.warning(f"Cache set failed: {e}")
            return False

    async def set_json(self, key: str, value: Any, ttl: int = 300) -> bool:
        """
        Set JSON value with TTL and safe serialization.

        Uses SafeJSONEncoder to handle datetime/UUID objects.

        Args:
            key: Cache key
            value: Value to store (will be JSON encoded)
            ttl: Time to live in seconds

        Returns:
            True if successful
        """
        try:
            serialized = json.dumps(value, cls=SafeJSONEncoder)
            await self.redis.setex(key, ttl, serialized)
            return True
        except Exception as e:
            logger.warning(f"Cache set_json failed: {e}")
            return False

    async def delete(self, key: str) -> bool:
        """Delete key from cache."""
        try:
            await self.redis.delete(key)
            return True
        except Exception as e:
            logger.warning(f"Cache delete failed: {e}")
            return False

    async def invalidate(self, key: str) -> bool:
        """
        Invalidate cache key.

        Alias for delete() - semantic naming for cache busting.

        Args:
            key: Cache key to invalidate

        Returns:
            True if successful
        """
        return await self.delete(key)

    async def invalidate_pattern(self, pattern: str, batch_size: int = 100) -> int:
        """
        Invalidate all keys matching pattern using batched deletes.

        Collects keys in batches and deletes them with a single pipeline call
        instead of one-by-one, reducing round trips from N to N/batch_size.

        Args:
            pattern: Redis key pattern (e.g., "user:*", "context:*")
            batch_size: Number of keys to delete per pipeline batch

        Returns:
            Number of keys deleted
        """
        try:
            deleted = 0
            batch = []
            async for key in self.redis.scan_iter(match=pattern, count=batch_size):
                batch.append(key)
                if len(batch) >= batch_size:
                    pipe = self.redis.pipeline(transaction=False)
                    for k in batch:
                        pipe.delete(k)
                    results = await pipe.execute()
                    deleted += sum(1 for r in results if r)
                    batch.clear()
            # Flush remaining keys
            if batch:
                pipe = self.redis.pipeline(transaction=False)
                for k in batch:
                    pipe.delete(k)
                results = await pipe.execute()
                deleted += sum(1 for r in results if r)
            if deleted > 0:
                logger.info(f"Invalidated {deleted} keys matching '{pattern}'")
            return deleted
        except Exception as e:
            logger.warning(f"Cache invalidate_pattern failed: {e}")
            return 0

    async def exists(self, key: str) -> bool:
        """Check if key exists."""
        try:
            return await self.redis.exists(key) > 0
        except Exception as e:
            logger.warning(f"Cache exists check failed: {e}")
            return False

    async def get_or_compute(
        self,
        key: str,
        compute_fn: Callable[[], Awaitable[Any]],
        ttl: int = 300,
        lock_timeout: int = 10
    ) -> Any:
        """
        Get from cache or compute value with stampede protection.

        Uses SETNX-based distributed lock to ensure only one caller computes
        the value when multiple requests hit a cold cache simultaneously.

        Args:
            key: Cache key
            compute_fn: Async function to compute value if not cached
            ttl: Time to live
            lock_timeout: Max seconds to hold the compute lock

        Returns:
            Cached or computed value
        """
        # Try cache first
        cached = await self.get_json(key)
        if cached is not None:
            return cached

        # Acquire distributed lock to prevent stampede
        lock_key = f"lock:{key}"
        lock_token = uuid.uuid4().hex
        acquired = False
        try:
            acquired = await self.redis.set(
                lock_key, lock_token, nx=True, ex=lock_timeout
            )
        except Exception as e:
            logger.debug(f"Cache lock acquire failed (best-effort): {e}")

        if not acquired:
            # Another caller is computing — poll with backoff then fallback
            for wait in (0.1, 0.2, 0.4):
                await asyncio.sleep(wait)
                cached = await self.get_json(key)
                if cached is not None:
                    return cached
            # Fallback: compute anyway (lock holder may have failed)

        try:
            result = await compute_fn()
            if result is not None:
                await self.set_json(key, result, ttl)
            return result
        finally:
            if acquired:
                try:
                    # Owner-safe release: only delete if we still hold the lock
                    from services.retry_utils import ATOMIC_LOCK_RELEASE_LUA
                    await self.redis.eval(
                        ATOMIC_LOCK_RELEASE_LUA,
                        1, lock_key, lock_token
                    )
                except Exception as e:
                    logger.debug(f"Cache lock release failed (best-effort): {e}")

    async def increment(self, key: str, ttl: Optional[int] = None) -> int:
        """
        Increment counter.

        Args:
            key: Counter key
            ttl: Optional TTL for first increment

        Returns:
            New value
        """
        try:
            if ttl:
                # Atomic INCR + conditional EXPIRE via Lua to avoid TOCTOU race
                lua = (
                    "local v = redis.call('INCR', KEYS[1]) "
                    "if v == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end "
                    "return v"
                )
                value = await self.redis.eval(lua, 1, key, ttl)
                return int(value)
            else:
                return await self.redis.incr(key)
        except Exception as e:
            logger.warning(f"Cache increment failed: {e}")
            return 0
