"""Tests for azure_rate_guard."""
import asyncio
import pytest

from services.v2.azure_rate_guard import AzureRateGuard, AzureRateGuardStats


@pytest.mark.asyncio
async def test_acquire_releases_slot():
    guard = AzureRateGuard(max_concurrent=2)
    async with guard.acquire(timeout_s=1.0):
        assert guard.in_flight == 1
    assert guard.in_flight == 0


@pytest.mark.asyncio
async def test_acquire_tracks_stats():
    guard = AzureRateGuard(max_concurrent=2)
    async with guard.acquire(timeout_s=1.0):
        pass
    assert guard.stats.acquires_total == 1


@pytest.mark.asyncio
async def test_concurrent_acquires_under_limit():
    guard = AzureRateGuard(max_concurrent=3)

    async def worker():
        async with guard.acquire(timeout_s=1.0):
            await asyncio.sleep(0.01)

    await asyncio.gather(*(worker() for _ in range(3)))
    assert guard.in_flight == 0
    assert guard.stats.acquires_total == 3


@pytest.mark.asyncio
async def test_max_queue_observed_tracks_peak():
    guard = AzureRateGuard(max_concurrent=5)
    release = asyncio.Event()

    async def holder():
        async with guard.acquire(timeout_s=1.0):
            await release.wait()

    tasks = [asyncio.create_task(holder()) for _ in range(3)]
    await asyncio.sleep(0.05)
    assert guard.in_flight == 3
    assert guard.stats.max_queue_observed == 3
    release.set()
    await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_acquire_timeout_when_full():
    guard = AzureRateGuard(max_concurrent=1)
    release = asyncio.Event()

    async def holder():
        async with guard.acquire(timeout_s=1.0):
            await release.wait()

    holder_task = asyncio.create_task(holder())
    await asyncio.sleep(0.01)

    with pytest.raises(asyncio.TimeoutError):
        async with guard.acquire(timeout_s=0.05):
            pass

    release.set()
    await holder_task


@pytest.mark.asyncio
async def test_release_after_exception():
    guard = AzureRateGuard(max_concurrent=1)

    with pytest.raises(ValueError):
        async with guard.acquire(timeout_s=1.0):
            raise ValueError("boom")

    assert guard.in_flight == 0
    # Should be able to acquire again after exception
    async with guard.acquire(timeout_s=1.0):
        pass


@pytest.mark.asyncio
async def test_avg_wait_ms_zero_when_no_waits():
    guard = AzureRateGuard(max_concurrent=10)
    async with guard.acquire(timeout_s=1.0):
        pass
    assert guard.avg_wait_ms == 0.0


@pytest.mark.asyncio
async def test_queueing_when_at_capacity():
    guard = AzureRateGuard(max_concurrent=1)
    order: list[int] = []

    async def task(i: int, delay: float):
        async with guard.acquire(timeout_s=2.0):
            order.append(i)
            await asyncio.sleep(delay)

    await asyncio.gather(
        task(1, 0.05),
        task(2, 0.0),
        task(3, 0.0),
    )
    assert sorted(order) == [1, 2, 3]
    assert guard.stats.acquires_total == 3


def test_stats_dataclass_defaults():
    s = AzureRateGuardStats()
    assert s.acquires_total == 0
    assert s.waits_count == 0
    assert s.max_queue_observed == 0
