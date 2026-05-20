"""Tests for ParamLabeler (LLM-generated Croatian labels)."""
from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from services.v2.param_labeler import ParamLabeler


# --------------------------------------------------------------------------
# Fake LLM client (mirrors openai SDK shape)
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
        self.next_content: str | None = None
        self.next_exception: Exception | None = None
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
# Tier 1: preloaded dict
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preloaded_dict_hit_zero_cost():
    """Preloaded entry → no LLM call, no Redis call."""
    client = _FakeClient()
    labeler = ParamLabeler(
        client, "gpt-4o-mini",
        preloaded={"delete_VehicleCalendar_id::id": "ID rezervacije"},
    )
    out = await labeler.label_for(
        "id", {"param_type": "string"},
        tool_id="delete_VehicleCalendar_id",
    )
    assert out == "ID rezervacije"
    assert client.chat.completions.calls == []


@pytest.mark.asyncio
async def test_preloaded_tool_agnostic_fallback():
    """`::param_name` key (no tool_id) matches any tool — for params with
    universal labels (like 'id' meaning 'ID zapisa' generically)."""
    client = _FakeClient()
    labeler = ParamLabeler(
        client, "gpt-4o-mini",
        preloaded={"::id": "ID zapisa"},
    )
    out = await labeler.label_for("id", None, tool_id="some_random_tool")
    assert out == "ID zapisa"
    assert client.chat.completions.calls == []


@pytest.mark.asyncio
async def test_specific_tool_wins_over_generic():
    """If both `tool::param` AND `::param` exist, tool-specific wins."""
    client = _FakeClient()
    labeler = ParamLabeler(
        client, "gpt-4o-mini",
        preloaded={
            "delete_Reservations_id::id": "ID rezervacije",
            "::id": "ID zapisa",
        },
    )
    out = await labeler.label_for(
        "id", None, tool_id="delete_Reservations_id",
    )
    assert out == "ID rezervacije"


# --------------------------------------------------------------------------
# Tier 2: Redis cache
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_redis_cache_hit_skips_llm():
    client = _FakeClient()
    redis = _FakeRedis()
    redis.store["param_label:delete_X::id"] = "ID iz cache-a"
    labeler = ParamLabeler(client, "gpt-4o-mini", redis_client=redis)
    out = await labeler.label_for("id", None, tool_id="delete_X")
    assert out == "ID iz cache-a"
    assert client.chat.completions.calls == []


@pytest.mark.asyncio
async def test_redis_cache_handles_bytes():
    """Some redis clients return bytes — labeler decodes."""
    client = _FakeClient()
    redis = _FakeRedis()
    redis.store["param_label:delete_X::id"] = "ID broj"

    class _BytesRedis:
        async def get(self, key):
            v = redis.store.get(key)
            return v.encode("utf-8") if v else None

        async def setex(self, key, ttl, value):
            pass

    labeler = ParamLabeler(client, "gpt-4o-mini", redis_client=_BytesRedis())
    out = await labeler.label_for("id", None, tool_id="delete_X")
    assert out == "ID broj"


# --------------------------------------------------------------------------
# Tier 3: LLM call + cache writeback
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_generates_label_and_caches():
    client = _FakeClient()
    client.chat.completions.respond_with_json({"label": "broj kilometara"})
    redis = _FakeRedis()
    labeler = ParamLabeler(client, "gpt-4o-mini", redis_client=redis)

    out = await labeler.label_for(
        "Mileage",
        {"param_type": "integer", "description": "Mileage value"},
        tool_id="post_AddMileage",
        tool_intent="Unos km zapisa",
    )

    assert out == "broj kilometara"
    # Wrote-back to cache for next time
    assert redis.store.get("param_label:post_AddMileage::Mileage") == "broj kilometara"


@pytest.mark.asyncio
async def test_llm_error_returns_none():
    """LLM exception → None (caller falls back to humanize)."""
    client = _FakeClient()
    client.chat.completions.raise_on_next(RuntimeError("timeout"))
    labeler = ParamLabeler(client, "gpt-4o-mini")
    out = await labeler.label_for("WeirdParam", None, tool_id="x")
    assert out is None


@pytest.mark.asyncio
async def test_malformed_json_response_returns_none():
    client = _FakeClient()
    client.chat.completions.respond_with_raw("{not valid")
    labeler = ParamLabeler(client, "gpt-4o-mini")
    out = await labeler.label_for("X", None, tool_id="y")
    assert out is None


@pytest.mark.asyncio
async def test_missing_label_field_returns_none():
    client = _FakeClient()
    client.chat.completions.respond_with_json({"text": "krivo polje"})
    labeler = ParamLabeler(client, "gpt-4o-mini")
    out = await labeler.label_for("X", None, tool_id="y")
    assert out is None


@pytest.mark.asyncio
async def test_empty_param_name_returns_none():
    client = _FakeClient()
    labeler = ParamLabeler(client, "gpt-4o-mini")
    out = await labeler.label_for("", None, tool_id="x")
    assert out is None
    assert client.chat.completions.calls == []


@pytest.mark.asyncio
async def test_long_label_capped():
    """LLM ignores prompt → returns 200-char string → cap at 80."""
    long_label = "x" * 500
    client = _FakeClient()
    client.chat.completions.respond_with_json({"label": long_label})
    labeler = ParamLabeler(client, "gpt-4o-mini")
    out = await labeler.label_for("X", None, tool_id="y")
    assert out is not None
    assert len(out) <= 80


@pytest.mark.asyncio
async def test_llm_call_passes_param_type_and_intent():
    """Verify LLM gets useful context, not bare param name."""
    client = _FakeClient()
    client.chat.completions.respond_with_json({"label": "X"})
    labeler = ParamLabeler(client, "gpt-4o-mini")
    await labeler.label_for(
        "Mileage",
        {"param_type": "integer", "description": "Vehicle mileage in km"},
        tool_id="post_AddMileage",
        tool_intent="Bilježenje km",
    )
    sent = client.chat.completions.calls[0]
    user_msg = sent["messages"][1]["content"]
    assert "Mileage" in user_msg
    assert "integer" in user_msg
    assert "Vehicle mileage" in user_msg
    assert "Bilježenje km" in user_msg


@pytest.mark.asyncio
async def test_works_without_llm_client_when_preload_hits():
    """Defensive: if preload covers all params, labeler never needs LLM.
    Caller might construct ParamLabeler with a stub client when running
    in scripted tests."""
    labeler = ParamLabeler(
        None, "gpt-4o-mini",  # LLM client None — would crash if invoked
        preloaded={"x::y": "Z"},
    )
    out = await labeler.label_for("y", None, tool_id="x")
    assert out == "Z"


@pytest.mark.asyncio
async def test_resolution_order_preload_beats_cache():
    """Preloaded dict wins even if Redis has a different value."""
    client = _FakeClient()
    redis = _FakeRedis()
    redis.store["param_label:tool_X::id"] = "iz cache-a"
    labeler = ParamLabeler(
        client, "gpt-4o-mini", redis_client=redis,
        preloaded={"tool_X::id": "iz preload-a"},
    )
    out = await labeler.label_for("id", None, tool_id="tool_X")
    assert out == "iz preload-a"
