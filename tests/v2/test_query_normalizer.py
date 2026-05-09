"""Unit tests for L1.5 QueryNormalizer."""
from __future__ import annotations

import pytest

from services.v2.query_normalizer import (
    NormalizedQuery, QueryNormalizer, _should_skip,
)


class _FakeLLM:
    """Stub LLM that returns canned JSON responses."""

    def __init__(self, response_json: str = '{"canonical": "test", "style": "natural"}'):
        self.calls = 0
        self._response = response_json
        self.chat = self
        self.completions = self

    async def create(self, **kwargs):
        self.calls += 1
        return _FakeResponse(self._response)


class _FakeResponse:
    def __init__(self, content: str):
        class _Choice:
            def __init__(self, c):
                self.message = type("M", (), {"content": c})()
        self.choices = [_Choice(content)]


@pytest.mark.asyncio
async def test_short_fragment_is_skipped():
    norm = QueryNormalizer(_FakeLLM(), "gpt-4o-mini")
    out = await norm.normalize("kolika km")
    assert out.skipped is True
    assert out.reason == "short_fragment"
    assert out.canonical == "kolika km"


@pytest.mark.asyncio
async def test_topic_canonical_short_is_skipped():
    norm = QueryNormalizer(_FakeLLM(), "gpt-4o-mini")
    out = await norm.normalize("moja kilometraza")
    assert out.skipped is True


@pytest.mark.asyncio
async def test_empty_query_is_handled_gracefully():
    norm = QueryNormalizer(_FakeLLM(), "gpt-4o-mini")
    out = await norm.normalize("")
    assert out.skipped is True
    assert out.reason == "empty"


@pytest.mark.asyncio
async def test_formal_long_query_is_normalized():
    fake_llm = _FakeLLM(
        '{"canonical": "obriši cost centre bulk", "style": "formal", '
        '"intent_action": "delete", "intent_entity": "cost_center", "intent_scope": "many"}'
    )
    norm = QueryNormalizer(fake_llm, "gpt-4o-mini")
    out = await norm.normalize(
        "Trebam deaktivirati više poslovnih jedinica koje su suvišne u našem sustavu"
    )
    assert out.skipped is False
    assert out.canonical == "obriši cost centre bulk"
    assert out.style == "formal"
    assert out.intent_action == "delete"
    assert out.intent_entity == "cost_center"
    assert out.intent_scope == "many"
    assert fake_llm.calls == 1


@pytest.mark.asyncio
async def test_llm_error_falls_back_to_original():
    class _ErrLLM:
        chat = None
        completions = None

        def __init__(self):
            self.chat = self
            self.completions = self

        async def create(self, **kwargs):
            raise RuntimeError("azure 503")

    norm = QueryNormalizer(_ErrLLM(), "gpt-4o-mini", max_retries=0)
    out = await norm.normalize("Trebam ažurirati podatke o pojedincu u sustavu")
    assert out.error is not None
    assert out.canonical == out.original  # fallback


@pytest.mark.asyncio
async def test_invalid_json_falls_back_after_retries():
    fake_llm = _FakeLLM("this is not json")
    norm = QueryNormalizer(fake_llm, "gpt-4o-mini", max_retries=1)
    out = await norm.normalize("Trebam izvršiti operaciju nad cost centrima")
    assert out.error == "json_parse_failed"
    assert out.canonical == out.original
    assert fake_llm.calls == 2  # initial + 1 retry


@pytest.mark.asyncio
async def test_empty_canonical_falls_back_to_original():
    fake_llm = _FakeLLM('{"canonical": "", "style": "natural"}')
    norm = QueryNormalizer(fake_llm, "gpt-4o-mini")
    q = "Trebam pomoć oko nekog dugačkog upita koji ima topic vocab"
    out = await norm.normalize(q)
    assert out.canonical == q


def test_should_skip_short():
    skip, reason = _should_skip("kolika km")
    assert skip is True
    assert reason == "short_fragment"


def test_should_skip_topic_canonical():
    skip, _ = _should_skip("registracija mog auta")
    assert skip is True


def test_should_not_skip_long_formal():
    skip, _ = _should_skip("Trebam deaktivirati više poslovnih jedinica iz sustava")
    assert skip is False


def test_normalized_query_both_for_routing():
    nq = NormalizedQuery(
        original="Trebam deaktivirati više cost centara",
        canonical="obriši cost centre bulk",
        style="formal",
    )
    s = nq.both_for_routing
    assert "obriši cost centre bulk" in s
    assert "Trebam deaktivirati" in s


def test_normalized_query_skipped_returns_original_only():
    nq = NormalizedQuery(
        original="kolika km", canonical="kolika km", skipped=True,
    )
    assert nq.both_for_routing == "kolika km"
