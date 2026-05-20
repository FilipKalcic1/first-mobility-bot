"""Tests for ApiErrorTranslator."""
from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from services.v2.api_error_translator import ApiErrorTranslator


# --------------------------------------------------------------------------
# Fake LLM client (same shape as openai SDK)
# --------------------------------------------------------------------------


@dataclass
class _Msg:
    content: str = ""


@dataclass
class _Choice:
    message: _Msg


@dataclass
class _Response:
    choices: list


class _FakeCompletions:
    def __init__(self):
        self.next_content = None
        self.next_exception = None
        self.calls: list[dict] = []

    def respond_with_json(self, payload: dict):
        self.next_content = json.dumps(payload, ensure_ascii=False)

    def respond_with_raw(self, content: str):
        self.next_content = content

    def raise_on_next(self, exc: Exception):
        self.next_exception = exc

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.next_exception is not None:
            exc = self.next_exception
            self.next_exception = None
            raise exc
        return _Response(choices=[_Choice(message=_Msg(
            content=self.next_content or "",
        ))])


class _FakeChat:
    def __init__(self):
        self.completions = _FakeCompletions()


class _FakeClient:
    def __init__(self):
        self.chat = _FakeChat()


class _FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    async def get(self, key):
        return self.store.get(key)

    async def setex(self, key, ttl, value):
        self.store[key] = value
        self.ttls[key] = ttl


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_translates_400_with_json_body():
    client = _FakeClient()
    client.chat.completions.respond_with_json({
        "message": "Nedostaje datoteka za upload. Dodaj prilog i pokušaj ponovo.",
    })
    tx = ApiErrorTranslator(client, "gpt-4o-mini")
    out = await tx.translate(
        status_code=400,
        response_body={"detail": "Required field 'file' is missing"},
        tool_id="post_Partners_id_documents",
        tool_intent="Dodavanje dokumenta",
    )
    assert out is not None
    assert "Nedostaje" in out


@pytest.mark.asyncio
async def test_translates_403_to_permission_message():
    client = _FakeClient()
    client.chat.completions.respond_with_json({
        "message": "Nemaš ovlasti za ovu radnju. Kontaktiraj managera.",
    })
    tx = ApiErrorTranslator(client, "gpt-4o-mini")
    out = await tx.translate(
        status_code=403,
        response_body="Forbidden — admin role required",
        tool_id="post_admin_thing",
    )
    assert out is not None
    assert "ovlasti" in out.lower()


@pytest.mark.asyncio
async def test_5xx_returns_none_skip_llm():
    """5xx server errors are NOT translated — user can't fix them."""
    client = _FakeClient()
    tx = ApiErrorTranslator(client, "gpt-4o-mini")
    out = await tx.translate(
        status_code=500,
        response_body={"error": "internal"},
        tool_id="get_X",
    )
    assert out is None
    # LLM was never called
    assert client.chat.completions.calls == []


@pytest.mark.asyncio
async def test_503_returns_none_skip_llm():
    client = _FakeClient()
    tx = ApiErrorTranslator(client, "gpt-4o-mini")
    out = await tx.translate(
        status_code=503,
        response_body="upstream timeout",
        tool_id="get_X",
    )
    assert out is None
    assert client.chat.completions.calls == []


@pytest.mark.asyncio
async def test_empty_body_returns_none():
    client = _FakeClient()
    tx = ApiErrorTranslator(client, "gpt-4o-mini")
    assert await tx.translate(400, None, "x") is None
    assert await tx.translate(400, "", "x") is None
    assert await tx.translate(400, "   ", "x") is None
    # LLM never called
    assert client.chat.completions.calls == []


@pytest.mark.asyncio
async def test_llm_exception_returns_none():
    client = _FakeClient()
    client.chat.completions.raise_on_next(RuntimeError("connection refused"))
    tx = ApiErrorTranslator(client, "gpt-4o-mini")
    out = await tx.translate(400, {"x": "y"}, "post_X")
    assert out is None


@pytest.mark.asyncio
async def test_malformed_json_response_returns_none():
    client = _FakeClient()
    client.chat.completions.respond_with_raw("{not valid json")
    tx = ApiErrorTranslator(client, "gpt-4o-mini")
    out = await tx.translate(400, {"x": "y"}, "post_X")
    assert out is None


@pytest.mark.asyncio
async def test_missing_message_field_returns_none():
    client = _FakeClient()
    client.chat.completions.respond_with_json({"otherfield": "x"})
    tx = ApiErrorTranslator(client, "gpt-4o-mini")
    out = await tx.translate(400, {"x": "y"}, "post_X")
    assert out is None


@pytest.mark.asyncio
async def test_cache_hit_skips_llm_call():
    """Identical (status, body, tool_id) → second call uses Redis cache."""
    client = _FakeClient()
    client.chat.completions.respond_with_json({"message": "Nedostaje polje."})
    redis = _FakeRedis()
    tx = ApiErrorTranslator(client, "gpt-4o-mini", redis_client=redis)

    out1 = await tx.translate(400, {"x": "y"}, "post_X")
    out2 = await tx.translate(400, {"x": "y"}, "post_X")

    assert out1 == out2 == "Nedostaje polje."
    # LLM called exactly once
    assert len(client.chat.completions.calls) == 1


@pytest.mark.asyncio
async def test_cache_keyed_per_tool_not_just_status():
    """Same 400 body but different tool_id → separate cache entries."""
    client = _FakeClient()
    redis = _FakeRedis()
    tx = ApiErrorTranslator(client, "gpt-4o-mini", redis_client=redis)

    client.chat.completions.respond_with_json({"message": "msg-X"})
    await tx.translate(400, {"err": "y"}, "post_X")

    client.chat.completions.respond_with_json({"message": "msg-Y"})
    await tx.translate(400, {"err": "y"}, "post_Y")  # different tool

    # Both went to LLM (cache miss)
    assert len(client.chat.completions.calls) == 2


@pytest.mark.asyncio
async def test_works_without_redis():
    client = _FakeClient()
    client.chat.completions.respond_with_json({"message": "X"})
    tx = ApiErrorTranslator(client, "gpt-4o-mini", redis_client=None)
    out = await tx.translate(400, {"x": "y"}, "post_X")
    assert out == "X"


@pytest.mark.asyncio
async def test_long_response_capped_at_300_chars():
    """Defensive: if LLM ignores the 200-char system instruction, we
    still cap the message so WhatsApp doesn't choke."""
    long_msg = "x" * 1000
    client = _FakeClient()
    client.chat.completions.respond_with_json({"message": long_msg})
    tx = ApiErrorTranslator(client, "gpt-4o-mini")
    out = await tx.translate(400, {"x": "y"}, "post_X")
    assert out is not None
    assert len(out) <= 300


@pytest.mark.asyncio
async def test_response_format_json_object_requested():
    """Verify the LLM is asked for JSON via response_format."""
    client = _FakeClient()
    client.chat.completions.respond_with_json({"message": "X"})
    tx = ApiErrorTranslator(client, "gpt-4o-mini")
    await tx.translate(400, {"x": "y"}, "post_X")
    sent = client.chat.completions.calls[0]
    assert sent.get("response_format") == {"type": "json_object"}
    assert sent.get("temperature") == 0
