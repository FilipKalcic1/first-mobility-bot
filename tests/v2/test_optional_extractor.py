"""Tests for OptionalParamExtractor."""
from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from services.v2.optional_extractor import OptionalParamExtractor


# --------------------------------------------------------------------------
# Fake LLM client (mirrors OpenAI SDK shape: chat.completions.create)
# --------------------------------------------------------------------------


@dataclass
class _Fn:
    name: str
    arguments: str


@dataclass
class _ToolCall:
    function: _Fn


@dataclass
class _Msg:
    tool_calls: list = None
    content: str = ""


@dataclass
class _Choice:
    message: _Msg


@dataclass
class _Response:
    choices: list


class _FakeCompletions:
    def __init__(self):
        self.next_response = None
        self.next_exception = None
        self.calls: list[dict] = []

    def respond_with_tool_call(self, args: dict, name: str = "fill_optionals"):
        self.next_response = _Response(choices=[_Choice(message=_Msg(
            tool_calls=[_ToolCall(function=_Fn(
                name=name, arguments=json.dumps(args),
            ))],
        ))])

    def respond_with_no_tool_call(self, content: str = ""):
        self.next_response = _Response(choices=[_Choice(message=_Msg(
            tool_calls=[], content=content,
        ))])

    def respond_with_malformed_json(self):
        self.next_response = _Response(choices=[_Choice(message=_Msg(
            tool_calls=[_ToolCall(function=_Fn(
                name="fill_optionals", arguments="{not valid json",
            ))],
        ))])

    def raise_on_next(self, exc: Exception):
        self.next_exception = exc

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.next_exception is not None:
            exc = self.next_exception
            self.next_exception = None
            raise exc
        return self.next_response


class _FakeChat:
    def __init__(self):
        self.completions = _FakeCompletions()


class _FakeClient:
    def __init__(self):
        self.chat = _FakeChat()


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extracts_multiple_fields_from_one_message():
    client = _FakeClient()
    client.chat.completions.respond_with_tool_call({
        "Notes": "kasnim po pola sata", "Type": "hitno",
    })
    extractor = OptionalParamExtractor(client, "gpt-4o-mini")

    result = await extractor.extract(
        "kasnim po pola sata i to je hitno",
        {
            "Notes": {"param_type": "string", "description": "Napomena"},
            "Type": {"param_type": "string", "description": "Tip"},
        },
    )
    assert result == {"Notes": "kasnim po pola sata", "Type": "hitno"}


@pytest.mark.asyncio
async def test_empty_tool_args_returns_empty_dict():
    client = _FakeClient()
    client.chat.completions.respond_with_tool_call({})
    extractor = OptionalParamExtractor(client, "gpt-4o-mini")

    result = await extractor.extract(
        "ne znam",
        {"Notes": {"param_type": "string"}},
    )
    assert result == {}


@pytest.mark.asyncio
async def test_no_tool_call_returns_empty_dict():
    client = _FakeClient()
    client.chat.completions.respond_with_no_tool_call(content="nista relevantno")
    extractor = OptionalParamExtractor(client, "gpt-4o-mini")

    result = await extractor.extract(
        "ne",
        {"Notes": {"param_type": "string"}},
    )
    assert result == {}


@pytest.mark.asyncio
async def test_network_exception_returns_empty_dict_no_propagation():
    client = _FakeClient()
    client.chat.completions.raise_on_next(RuntimeError("connection refused"))
    extractor = OptionalParamExtractor(client, "gpt-4o-mini")

    result = await extractor.extract(
        "nešto",
        {"Notes": {"param_type": "string"}},
    )
    assert result == {}


@pytest.mark.asyncio
async def test_malformed_json_args_returns_empty_dict():
    client = _FakeClient()
    client.chat.completions.respond_with_malformed_json()
    extractor = OptionalParamExtractor(client, "gpt-4o-mini")

    result = await extractor.extract(
        "nešto",
        {"Notes": {"param_type": "string"}},
    )
    assert result == {}


@pytest.mark.asyncio
async def test_unknown_keys_in_llm_response_filtered_out():
    """LLM hallucinates a field not in optional_specs → must be dropped."""
    client = _FakeClient()
    client.chat.completions.respond_with_tool_call({
        "Notes": "ok", "RandomField": "abc",
    })
    extractor = OptionalParamExtractor(client, "gpt-4o-mini")

    result = await extractor.extract(
        "ok",
        {"Notes": {"param_type": "string"}},  # RandomField NOT offered
    )
    assert result == {"Notes": "ok"}
    assert "RandomField" not in result


@pytest.mark.asyncio
async def test_null_and_empty_string_values_dropped():
    client = _FakeClient()
    client.chat.completions.respond_with_tool_call({
        "Notes": "real", "Type": None, "Code": "  ",
    })
    extractor = OptionalParamExtractor(client, "gpt-4o-mini")

    result = await extractor.extract(
        "...",
        {
            "Notes": {"param_type": "string"},
            "Type": {"param_type": "string"},
            "Code": {"param_type": "string"},
        },
    )
    assert result == {"Notes": "real"}


@pytest.mark.asyncio
async def test_empty_user_text_skips_llm_call():
    client = _FakeClient()
    extractor = OptionalParamExtractor(client, "gpt-4o-mini")
    result = await extractor.extract("", {"Notes": {"param_type": "string"}})
    assert result == {}
    assert client.chat.completions.calls == []  # no network call


@pytest.mark.asyncio
async def test_empty_optional_specs_skips_llm_call():
    client = _FakeClient()
    extractor = OptionalParamExtractor(client, "gpt-4o-mini")
    result = await extractor.extract("nešto", {})
    assert result == {}
    assert client.chat.completions.calls == []


@pytest.mark.asyncio
async def test_schema_built_has_only_descriptions_no_required():
    """Verify the tool schema sent to LLM has properties but no `required`
    key — everything must be optional in this extractor."""
    client = _FakeClient()
    client.chat.completions.respond_with_tool_call({})
    extractor = OptionalParamExtractor(client, "gpt-4o-mini")
    await extractor.extract(
        "nešto",
        {
            "Notes": {"param_type": "string", "description": "Napomena"},
            "Mileage": {"param_type": "integer", "description": "Broj km"},
        },
    )
    sent = client.chat.completions.calls[0]
    tool_schema = sent["tools"][0]
    params_schema = tool_schema["function"]["parameters"]
    assert "required" not in params_schema  # critical: all-optional contract
    props = params_schema["properties"]
    assert props["Notes"] == {"type": "string", "description": "Napomena"}
    assert props["Mileage"] == {"type": "integer", "description": "Broj km"}


@pytest.mark.asyncio
async def test_invalid_param_type_falls_back_to_string():
    """Unknown param_type (e.g. 'enum') must not crash schema build."""
    client = _FakeClient()
    client.chat.completions.respond_with_tool_call({"Type": "x"})
    extractor = OptionalParamExtractor(client, "gpt-4o-mini")
    result = await extractor.extract(
        "x",
        {"Type": {"param_type": "enum"}},  # not in _TYPE_MAP
    )
    assert result == {"Type": "x"}


@pytest.mark.asyncio
async def test_wrong_tool_name_in_response_returns_empty():
    """Defensive: if LLM somehow returns a tool call with wrong name,
    we ignore it (don't trust unauthorized data)."""
    client = _FakeClient()
    client.chat.completions.respond_with_tool_call(
        {"Notes": "x"}, name="some_other_tool",
    )
    extractor = OptionalParamExtractor(client, "gpt-4o-mini")
    result = await extractor.extract(
        "x", {"Notes": {"param_type": "string"}},
    )
    assert result == {}
