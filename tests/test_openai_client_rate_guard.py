"""Verify GuardedAzureClient wraps chat.completions.create with rate guard."""
import asyncio
import pytest

from services.openai_client import GuardedAzureClient, _GuardedCompletions
from services.v2.azure_rate_guard import AzureRateGuard


class _FakeCompletions:
    def __init__(self):
        self.create_calls = 0

    async def create(self, *args, **kwargs):
        self.create_calls += 1
        return {"args": args, "kwargs": kwargs}


class _FakeChat:
    def __init__(self):
        self.completions = _FakeCompletions()


class _FakeAzureClient:
    def __init__(self):
        self.chat = _FakeChat()
        self.embeddings = "embeddings-stub"  # plain attr to test passthrough


@pytest.mark.asyncio
async def test_guarded_create_invokes_inner_under_guard():
    inner = _FakeAzureClient()
    guard = AzureRateGuard(max_concurrent=2)
    client = GuardedAzureClient(inner, guard, timeout_s=1.0)

    result = await client.chat.completions.create(model="gpt-4o-mini")
    assert result["kwargs"]["model"] == "gpt-4o-mini"
    assert inner.chat.completions.create_calls == 1
    assert guard.stats.acquires_total == 1


@pytest.mark.asyncio
async def test_guard_concurrency_enforced_on_chat_create():
    inner = _FakeAzureClient()
    guard = AzureRateGuard(max_concurrent=1)
    client = GuardedAzureClient(inner, guard, timeout_s=1.0)

    release = asyncio.Event()
    original_create = inner.chat.completions.create

    async def slow_create(*args, **kwargs):
        await release.wait()
        return await original_create(*args, **kwargs)

    inner.chat.completions.create = slow_create  # type: ignore[method-assign]

    holder = asyncio.create_task(client.chat.completions.create(model="x"))
    await asyncio.sleep(0.01)

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(client.chat.completions.create(model="y"), timeout=0.1)

    release.set()
    await holder


@pytest.mark.asyncio
async def test_unrelated_attributes_pass_through():
    inner = _FakeAzureClient()
    guard = AzureRateGuard(max_concurrent=2)
    client = GuardedAzureClient(inner, guard, timeout_s=1.0)
    # embeddings is not wrapped — passes through unchanged
    assert client.embeddings == "embeddings-stub"


@pytest.mark.asyncio
async def test_guarded_completions_passes_through_other_methods():
    """The proxy only intercepts .create — other completions attrs pass through."""

    class _Comp:
        def __init__(self):
            self.token_count = 42

        async def create(self, **kwargs):
            return "ok"

    guard = AzureRateGuard(max_concurrent=1)
    proxy = _GuardedCompletions(_Comp(), guard, timeout_s=1.0)
    assert proxy.token_count == 42  # __getattr__ fallback


def test_max_concurrent_from_env_default():
    import services.openai_client as oc
    import os

    old = os.environ.pop("AZURE_LLM_MAX_CONCURRENT", None)
    try:
        assert oc._max_concurrent_from_env() == 20
    finally:
        if old is not None:
            os.environ["AZURE_LLM_MAX_CONCURRENT"] = old


def test_max_concurrent_from_env_invalid_falls_back():
    import services.openai_client as oc
    import os

    os.environ["AZURE_LLM_MAX_CONCURRENT"] = "not-a-number"
    try:
        assert oc._max_concurrent_from_env() == 20
    finally:
        del os.environ["AZURE_LLM_MAX_CONCURRENT"]


def test_max_concurrent_from_env_zero_clamped():
    import services.openai_client as oc
    import os

    os.environ["AZURE_LLM_MAX_CONCURRENT"] = "0"
    try:
        assert oc._max_concurrent_from_env() == 1
    finally:
        del os.environ["AZURE_LLM_MAX_CONCURRENT"]
