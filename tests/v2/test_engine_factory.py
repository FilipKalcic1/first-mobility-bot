"""Tests for V2Engine production factory (post-Phase-4 rewrite).

Verifies that `make_v2_engine_for_production` constructs a V2Engine with
all required dependencies wired. Stubs out Azure clients so the test runs
without network access; the factory's anchor-index build is allowed to
fail gracefully — engine still constructs.
"""
from __future__ import annotations

import pytest

from services.v2.engine import (
    V2Engine,
    V2EngineBundle,
    make_v2_engine_for_production,
)


class _FakeRedis:
    """Minimal Redis duck-type for V2Engine dependencies."""
    async def get(self, *_a, **_kw): return None
    async def set(self, *_a, **_kw): return True
    async def setex(self, *_a, **_kw): return True
    async def delete(self, *_a, **_kw): return 0
    async def expire(self, *_a, **_kw): return True
    async def exists(self, *_a, **_kw): return 0
    async def rpush(self, *_a, **_kw): return 1
    async def ltrim(self, *_a, **_kw): return True
    async def lrange(self, *_a, **_kw): return []
    async def incrby(self, *_a, **_kw): return 1
    async def hget(self, *_a, **_kw): return None
    async def hset(self, *_a, **_kw): return 1
    async def hgetall(self, *_a, **_kw): return {}
    async def zadd(self, *_a, **_kw): return 1
    async def zrangebyscore(self, *_a, **_kw): return []
    async def zremrangebyscore(self, *_a, **_kw): return 0
    async def script_load(self, *_a, **_kw): return "stub-sha"
    async def evalsha(self, *_a, **_kw): return 1
    async def eval(self, *_a, **_kw): return 1


class _FakeGateway:
    pass


class _FakeRegistry:
    tools = []
    def get_tool(self, *_a, **_kw): return None


class _FakeSettings:
    """Minimal settings shape the factory reads."""
    AZURE_OPENAI_DEPLOYMENT_NAME = "gpt-4o-mini"
    AZURE_OPENAI_EMBEDDING_DEPLOYMENT = "text-embedding-ada-002"


class _FakeEmbeddingsResp:
    def __init__(self, dim: int = 8):
        self.data = [type("_E", (), {"embedding": [0.0] * dim})()]


class _FakeEmbeddings:
    async def create(self, *, input, model):  # noqa: A002 — mirrors SDK kwarg
        return type("_R", (), {
            "data": [type("_E", (), {"embedding": [0.0] * 8})()
                     for _ in (input if isinstance(input, list) else [input])]
        })()


class _FakeEmbeddingClient:
    """Quacks like AsyncAzureOpenAI for the factory's `_embed_fn` closure."""
    def __init__(self):
        self.embeddings = _FakeEmbeddings()


def _stub_openai(monkeypatch):
    """Patch the openai_client accessors so the factory never tries Azure."""
    monkeypatch.setattr(
        "services.openai_client.get_openai_client", lambda: object(),
    )
    monkeypatch.setattr(
        "services.openai_client.get_embedding_client", _FakeEmbeddingClient,
    )


@pytest.mark.asyncio
async def test_factory_returns_bundle_with_engine(monkeypatch):
    """Factory builds V2EngineBundle with engine + all stores."""
    _stub_openai(monkeypatch)

    bundle = await make_v2_engine_for_production(
        redis_client=_FakeRedis(),
        gateway=_FakeGateway(),
        tool_registry=_FakeRegistry(),
        settings=_FakeSettings(),
    )

    assert isinstance(bundle, V2EngineBundle)
    assert isinstance(bundle.engine, V2Engine)
    assert bundle.identity is not None
    assert bundle.conversation_history is not None
    assert bundle.pending_mutation is not None
    assert bundle.pending_clarify is not None
    assert bundle.gdpr_audit is not None
    assert bundle.engine.gdpr_audit_store is bundle.gdpr_audit


@pytest.mark.asyncio
async def test_factory_wires_new_router_and_formatter(monkeypatch):
    """After Phase 4: V2Engine must have .router (LLMRouter) and
    .formatter_llm (LLMFormatter) wired, not recognition/formatter-templates."""
    _stub_openai(monkeypatch)

    bundle = await make_v2_engine_for_production(
        redis_client=_FakeRedis(),
        gateway=_FakeGateway(),
        tool_registry=_FakeRegistry(),
        settings=_FakeSettings(),
    )
    eng = bundle.engine
    assert eng.router is not None
    assert eng.formatter_llm is not None
    # The old recognition field must be gone — keeps the rebuild honest
    assert not hasattr(eng, "recognition")


@pytest.mark.asyncio
async def test_factory_idempotent(monkeypatch):
    """Calling factory multiple times must not error or share state."""
    _stub_openai(monkeypatch)

    bundle1 = await make_v2_engine_for_production(
        redis_client=_FakeRedis(),
        gateway=_FakeGateway(),
        tool_registry=_FakeRegistry(),
        settings=_FakeSettings(),
    )
    bundle2 = await make_v2_engine_for_production(
        redis_client=_FakeRedis(),
        gateway=_FakeGateway(),
        tool_registry=_FakeRegistry(),
        settings=_FakeSettings(),
    )
    assert bundle1.engine is not bundle2.engine  # fresh per-call
