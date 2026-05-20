"""Azure global rate limit guard — addresses critique #9.

Token budget is per-phone/per-tenant. Azure has GLOBAL TPM/RPM cap on
deployment. 30 concurrent users × 3 LLM calls = 90 calls in flight may
exceed Azure deployment quota.

This module: process-wide async semaphore. Limits concurrent LLM calls
to N (default 20, configurable). Each call acquires before send_to_azure,
releases on response.

When semaphore is full, queries queue (graceful) instead of erroring.
Queue depth + wait time logged for capacity planning.
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class AzureRateGuardStats:
    acquires_total: int = 0
    waits_count: int = 0
    waits_total_ms: float = 0.0
    max_queue_observed: int = 0


class AzureRateGuard:
    """Process-wide concurrency limit on Azure LLM calls.

    Usage:
        guard = AzureRateGuard(max_concurrent=20)
        async with guard.acquire(timeout_s=30):
            response = await llm.chat.completions.create(...)

    On timeout → raises asyncio.TimeoutError; engine renders graceful
    "Sustav je preopterećen, pokušaj kroz minutu" message.
    """

    def __init__(self, max_concurrent: int = 20):
        self._sem = asyncio.Semaphore(max_concurrent)
        self._max = max_concurrent
        self._in_flight = 0
        self.stats = AzureRateGuardStats()

    @asynccontextmanager
    async def acquire(self, timeout_s: float = 30.0):
        """Acquire one concurrency slot or raise TimeoutError."""
        t_start = time.time()
        self.stats.acquires_total += 1
        try:
            await asyncio.wait_for(self._sem.acquire(), timeout=timeout_s)
        except asyncio.TimeoutError:
            logger.warning(
                "Azure rate guard timeout after %.1fs (max=%d in_flight=%d)",
                timeout_s, self._max, self._in_flight,
            )
            raise
        wait_ms = (time.time() - t_start) * 1000
        if wait_ms > 50:
            self.stats.waits_count += 1
            self.stats.waits_total_ms += wait_ms
        self._in_flight += 1
        if self._in_flight > self.stats.max_queue_observed:
            self.stats.max_queue_observed = self._in_flight
        try:
            yield
        finally:
            self._in_flight -= 1
            self._sem.release()

    @property
    def in_flight(self) -> int:
        return self._in_flight

    @property
    def avg_wait_ms(self) -> float:
        if self.stats.waits_count == 0:
            return 0.0
        return self.stats.waits_total_ms / self.stats.waits_count
