"""Unit tests for ToolUseResponder — real 2-pass tool_use loop."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from services.v2.tool_use_responder import (
    ToolUseResponder, ToolUseResult,
    _build_tool_schemas, _format_history, _format_identity, _safe_fn_name,
)


# ---- Fakes ----

class _FakeToolCall:
    def __init__(self, fn_name, args_json, call_id="tc_1"):
        self.id = call_id
        self.type = "function"
        self.function = SimpleNamespace(name=fn_name, arguments=args_json)


class _FakeMsg:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _FakeChoice:
    def __init__(self, msg):
        self.message = msg


class _FakeResponse:
    def __init__(self, msg):
        self.choices = [_FakeChoice(msg)]


class _FakeLLM:
    """Stub that returns a queue of pre-set responses (Pass 1, Pass 2, ...)."""

    def __init__(self, responses):
        self._responses = list(responses)
        self._idx = 0
        self.call_args = []
        self.chat = self
        self.completions = self

    async def create(self, **kwargs):
        self.call_args.append(kwargs)
        if self._idx >= len(self._responses):
            raise RuntimeError("No more stubbed responses")
        msg = self._responses[self._idx]
        self._idx += 1
        return _FakeResponse(msg)


async def _stub_retriever(query, k):
    return [
        {
            "tool_id": "get_MasterData",
            "method": "GET",
            "intent_summary": "snapshot vozila",
            "returns": {"TotalKm": "km"},
            "required_params": [],
        },
        {
            "tool_id": "post_AddMileage",
            "method": "POST",
            "intent_summary": "unos km",
            "returns": {"id": "ID"},
            "required_params": ["value"],
        },
    ]


async def _stub_executor_success(tool_id, params, ident):
    return SimpleNamespace(
        success=True, data={"TotalKm": 34521, "registracija": "DA053F"},
        error=None, circuit_open=False,
    )


async def _stub_executor_failure(tool_id, params, ident):
    return SimpleNamespace(
        success=False, data={}, error="http_500", circuit_open=False,
    )


# ---- Tests ----

@pytest.mark.asyncio
async def test_empty_query_returns_error():
    llm = _FakeLLM([])
    r = ToolUseResponder(llm, "gpt-4o-mini", _stub_retriever, _stub_executor_success)
    out = await r.respond("")
    assert out.error == "empty_query"


@pytest.mark.asyncio
async def test_no_candidates_short_circuits():
    async def empty(q, k):
        return []
    llm = _FakeLLM([])
    r = ToolUseResponder(llm, "gpt-4o-mini", empty, _stub_executor_success)
    out = await r.respond("kolika km")
    assert out.error == "no_candidates"


@pytest.mark.asyncio
async def test_pass1_text_response_no_tool_call():
    """Greeting / OOS — LLM responds with content, no tool_calls."""
    llm = _FakeLLM([_FakeMsg(content="Bok!", tool_calls=None)])
    r = ToolUseResponder(llm, "gpt-4o-mini", _stub_retriever, _stub_executor_success)
    out = await r.respond("Bok")
    assert out.response_text == "Bok!"
    assert out.tool_called is None
    assert len(llm.call_args) == 1  # only Pass 1


@pytest.mark.asyncio
async def test_2pass_tool_use_with_real_data():
    """LLM picks tool → executor runs → LLM Pass 2 sees JSON and writes response."""
    llm = _FakeLLM([
        _FakeMsg(
            content=None,
            tool_calls=[_FakeToolCall("get_MasterData", "{}")],
        ),
        _FakeMsg(content="Tvoja kilometraža je 34 521 km."),
    ])
    r = ToolUseResponder(llm, "gpt-4o-mini", _stub_retriever, _stub_executor_success)
    out = await r.respond("kolika km")
    assert out.tool_called == "get_MasterData"
    assert out.response_text == "Tvoja kilometraža je 34 521 km."
    assert out.api_data["TotalKm"] == 34521
    assert len(llm.call_args) == 2  # Pass 1 + Pass 2


@pytest.mark.asyncio
async def test_executor_failure_pass2_writes_graceful_message():
    """API fails → Pass 2 LLM gets error in tool_result, writes friendly HR error."""
    llm = _FakeLLM([
        _FakeMsg(
            content=None,
            tool_calls=[_FakeToolCall("get_MasterData", "{}")],
        ),
        _FakeMsg(content="Nažalost, došlo je do problema. Pokušaj ponovo."),
    ])
    r = ToolUseResponder(llm, "gpt-4o-mini", _stub_retriever, _stub_executor_failure)
    out = await r.respond("kolika km")
    assert out.tool_called == "get_MasterData"
    assert "problem" in (out.response_text or "").lower()
    assert out.api_data.get("_failed") is True


@pytest.mark.asyncio
async def test_pass1_llm_error_returns_error_no_pass2():
    class _ErrLLM:
        chat = None
        completions = None

        def __init__(self):
            self.chat = self
            self.completions = self

        async def create(self, **kwargs):
            raise ConnectionError("azure 503")

    r = ToolUseResponder(_ErrLLM(), "gpt-4o-mini", _stub_retriever, _stub_executor_success)
    out = await r.respond("kolika km")
    assert out.error and out.error.startswith("pass1_llm_error:")
    assert out.response_text is None


@pytest.mark.asyncio
async def test_pass2_llm_error_keeps_executor_data():
    """Pass 2 fails after API succeeded — return error but preserve api_data for caller debugging."""
    class _Err2:
        def __init__(self):
            self._calls = 0
            self.chat = self
            self.completions = self

        async def create(self, **kwargs):
            self._calls += 1
            if self._calls == 1:
                return _FakeResponse(_FakeMsg(
                    content=None,
                    tool_calls=[_FakeToolCall("get_MasterData", "{}")],
                ))
            raise ConnectionError("pass2 timeout")

    r = ToolUseResponder(_Err2(), "gpt-4o-mini", _stub_retriever, _stub_executor_success)
    out = await r.respond("kolika km")
    assert out.tool_called == "get_MasterData"
    assert out.api_data is not None
    assert out.api_data["TotalKm"] == 34521
    assert out.error and out.error.startswith("pass2_llm_error:")


@pytest.mark.asyncio
async def test_arg_parsing_handles_invalid_json():
    """LLM emits broken JSON args — responder defaults to {} not crash."""
    llm = _FakeLLM([
        _FakeMsg(
            content=None,
            tool_calls=[_FakeToolCall("get_MasterData", "{not json")],
        ),
        _FakeMsg(content="OK."),
    ])
    r = ToolUseResponder(llm, "gpt-4o-mini", _stub_retriever, _stub_executor_success)
    out = await r.respond("test")
    assert out.tool_called == "get_MasterData"
    assert out.tool_params == {}


@pytest.mark.asyncio
async def test_tool_id_safe_name_round_trip():
    """Long tool_id gets hashed into 64-char fn name; Pass 1 result resolves back."""
    long_id = "delete_VeryLongResourceNameThatExceedsTheSixtyFourCharacterLimit_DeleteByCriteria"
    safe = _safe_fn_name(long_id)
    assert len(safe) <= 64
    assert safe != long_id  # hashed
    # Round-trip in real responder
    async def long_retriever(q, k):
        return [{
            "tool_id": long_id, "method": "DELETE",
            "intent_summary": "x", "required_params": [],
        }]

    llm = _FakeLLM([
        _FakeMsg(
            content=None,
            tool_calls=[_FakeToolCall(safe, "{}")],
        ),
        _FakeMsg(content="OK."),
    ])
    r = ToolUseResponder(llm, "gpt-4o-mini", long_retriever, _stub_executor_success)
    out = await r.respond("test")
    assert out.tool_called == long_id  # resolved back


def test_safe_fn_name_short_unchanged():
    assert _safe_fn_name("get_MasterData") == "get_MasterData"


def test_safe_fn_name_truncates_long():
    long = "x" * 100
    safe = _safe_fn_name(long)
    assert len(safe) <= 64


def test_build_tool_schemas_caps_at_30():
    cands = [
        {"tool_id": f"get_T{i}", "method": "GET",
         "intent_summary": "x", "required_params": []}
        for i in range(50)
    ]
    schemas, name_map = _build_tool_schemas(cands)
    assert len(schemas) == 30
    assert len(name_map) == 30


def test_format_identity_includes_provided_fields():
    text = _format_identity({
        "first_name": "Filip", "vehicle_name": "DA053F",
    })
    assert "Filip" in text and "DA053F" in text


def test_format_history_empty_marker():
    assert "nema povijesti" in _format_history([])


def test_format_history_truncates_long_turn():
    turns = [{"user": "a" * 300, "bot": "b" * 300}]
    text = _format_history(turns)
    assert "a" * 141 not in text
    assert "b" * 141 not in text


def test_tool_use_result_dataclass_defaults():
    r = ToolUseResult(response_text="hi")
    assert r.tool_called is None
    assert r.tool_params == {}
    assert r.needs_confirm is False
    assert r.api_data is None
    assert r.reasoning_trace == []
