"""Unit tests for services/hyde.py — trigger logic, expansion, cache, generation."""

import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from services import hyde


@pytest.fixture(autouse=True)
def _enable_hyde(monkeypatch):
    monkeypatch.setattr(hyde, "HYDE_ENABLED", True)
    monkeypatch.setattr(hyde, "HYDE_THRESHOLD", 0.83)
    monkeypatch.setattr(hyde, "HYDE_MIN_QUERY_LEN", 4)


# ---------------------------------------------------------------------------
# should_trigger
# ---------------------------------------------------------------------------


def test_should_trigger_disabled(monkeypatch):
    monkeypatch.setattr(hyde, "HYDE_ENABLED", False)
    assert hyde.should_trigger(0.1, "koji auti su slobodni sutra") is False


def test_should_trigger_short_query():
    # Below HYDE_MIN_QUERY_LEN=4
    assert hyde.should_trigger(0.1, "hi") is False
    assert hyde.should_trigger(0.1, "   ") is False


def test_should_trigger_none_score_fires():
    assert hyde.should_trigger(None, "koji auti su slobodni") is True


def test_should_trigger_below_threshold():
    assert hyde.should_trigger(0.50, "koji auti su slobodni") is True
    assert hyde.should_trigger(0.829, "koji auti su slobodni") is True


def test_should_trigger_confident_skip():
    assert hyde.should_trigger(0.83, "koji auti su slobodni") is False
    assert hyde.should_trigger(0.95, "koji auti su slobodni") is False


# ---------------------------------------------------------------------------
# build_expanded_query
# ---------------------------------------------------------------------------


def test_build_expanded_query_concat():
    result = hyde.build_expanded_query("koji auti", "provjera dostupnosti vozila")
    assert result == "koji auti | provjera dostupnosti vozila"


def test_build_expanded_query_none_passthrough():
    assert hyde.build_expanded_query("koji auti", None) == "koji auti"


def test_build_expanded_query_empty_passthrough():
    assert hyde.build_expanded_query("koji auti", "") == "koji auti"


# ---------------------------------------------------------------------------
# _cache_key
# ---------------------------------------------------------------------------


def test_cache_key_is_deterministic():
    a = hyde._cache_key("Koji AUTI su slobodni?")
    b = hyde._cache_key("koji auti su slobodni?")
    assert a == b  # normalize_query + lower should collapse casing
    assert a.startswith("hyde:")


def test_cache_key_differs_for_different_queries():
    assert hyde._cache_key("koji auti") != hyde._cache_key("koje rezervacije")


# ---------------------------------------------------------------------------
# generate_hypothetical_doc — cache hit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_returns_cached(monkeypatch):
    redis = MagicMock()
    redis.get = AsyncMock(return_value=b"cached hypothetical doc")
    openai = MagicMock()  # should NOT be called

    result = await hyde.generate_hypothetical_doc(
        query="koji auti",
        openai_client=openai,
        redis_client=redis,
    )
    assert result == "cached hypothetical doc"
    redis.get.assert_awaited_once()


@pytest.mark.asyncio
async def test_generate_cache_miss_calls_llm_and_sets(monkeypatch):
    redis = MagicMock()
    redis.get = AsyncMock(return_value=None)
    redis.setex = AsyncMock(return_value=True)

    fake_response = MagicMock()
    fake_response.choices = [MagicMock(message=MagicMock(content="provjera dostupnosti vozila — ..."))]
    fake_response.usage = None

    openai = MagicMock()
    openai.chat.completions.create = AsyncMock(return_value=fake_response)

    # Stub get_settings
    import config as config_mod
    fake_settings = MagicMock(AZURE_OPENAI_DEPLOYMENT_NAME="gpt-4o-mini")
    monkeypatch.setattr(config_mod, "get_settings", lambda: fake_settings)

    result = await hyde.generate_hypothetical_doc(
        query="koji auti su slobodni",
        openai_client=openai,
        redis_client=redis,
    )
    assert result == "provjera dostupnosti vozila — ..."
    openai.chat.completions.create.assert_awaited_once()
    redis.setex.assert_awaited_once()


@pytest.mark.asyncio
async def test_generate_fails_open_on_llm_error(monkeypatch):
    openai = MagicMock()
    openai.chat.completions.create = AsyncMock(side_effect=RuntimeError("timeout"))

    import config as config_mod
    monkeypatch.setattr(
        config_mod,
        "get_settings",
        lambda: MagicMock(AZURE_OPENAI_DEPLOYMENT_NAME="gpt-4o-mini"),
    )

    result = await hyde.generate_hypothetical_doc(
        query="koji auti",
        openai_client=openai,
        redis_client=None,
    )
    assert result is None


@pytest.mark.asyncio
async def test_generate_no_client_returns_none():
    result = await hyde.generate_hypothetical_doc(
        query="koji auti", openai_client=None, redis_client=None
    )
    assert result is None


@pytest.mark.asyncio
async def test_generate_empty_query_returns_none():
    openai = MagicMock()
    result = await hyde.generate_hypothetical_doc(
        query="", openai_client=openai, redis_client=None
    )
    assert result is None
