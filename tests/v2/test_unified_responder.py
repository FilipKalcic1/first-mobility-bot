"""Unit tests for UnifiedResponder — single LLM call replacing V3 chain."""
from __future__ import annotations

import pytest

from services.v2.unified_responder import (
    UnifiedDecision, UnifiedResponder,
    _format_history, _format_identity, _format_tools,
)


class _FakeLLM:
    def __init__(self, response_json: str):
        self._response = response_json
        self.calls = 0
        self.chat = self
        self.completions = self

    async def create(self, **kwargs):
        self.calls += 1

        class _Choice:
            def __init__(self, c):
                self.message = type("M", (), {"content": c})()
        return type("R", (), {"choices": [_Choice(self._response)]})()


async def _stub_retriever(query, k):
    return [
        {
            "tool_id": "get_MasterData",
            "method": "GET",
            "intent_summary": "Snapshot vozila vozača",
            "returns": {"TotalKm": "kilometraža", "registracija": "plate"},
            "required_params": [],
        },
        {
            "tool_id": "get_VehicleCalendar",
            "method": "GET",
            "intent_summary": "Lista rezervacija",
            "returns": {"items": "lista", "totalCount": "broj"},
            "required_params": [],
        },
    ]


@pytest.mark.asyncio
async def test_empty_query_returns_error():
    fake = _FakeLLM('{"tool_id": null}')
    r = UnifiedResponder(fake, "gpt-4o-mini", retriever=_stub_retriever)
    out = await r.respond("")
    assert out.error == "empty_query"


@pytest.mark.asyncio
async def test_picks_tool_from_candidates():
    fake = _FakeLLM(
        '{"tool_id":"get_MasterData","params":{},"needs_confirm":false,'
        '"response_text":"Tvoja kilometraža: {TotalKm} km","needs_clarify":false,'
        '"clarify_question":null,"clarify_options":[],"reasoning":"snapshot pita","confidence":0.95}'
    )
    r = UnifiedResponder(fake, "gpt-4o-mini", retriever=_stub_retriever)
    out = await r.respond("kolika km", identity_summary={})
    assert out.tool_id == "get_MasterData"
    assert out.needs_confirm is False
    assert "TotalKm" in (out.response_text or "")
    assert out.confidence == 0.95


@pytest.mark.asyncio
async def test_drops_hallucinated_tool_id():
    fake = _FakeLLM(
        '{"tool_id":"delete_NotInList","params":{},"needs_confirm":true,'
        '"response_text":null,"reasoning":"hallucinated","confidence":0.9}'
    )
    r = UnifiedResponder(fake, "gpt-4o-mini", retriever=_stub_retriever)
    out = await r.respond("obriši nešto", identity_summary={})
    assert out.tool_id is None
    assert out.error == "hallucinated_tool_id"


@pytest.mark.asyncio
async def test_handles_invalid_json_with_retry():
    fake = _FakeLLM("not json")
    r = UnifiedResponder(fake, "gpt-4o-mini", retriever=_stub_retriever, max_retries=1)
    out = await r.respond("kolika km")
    assert out.error == "json_parse_failed"
    assert fake.calls == 2


@pytest.mark.asyncio
async def test_handles_llm_exception():
    class _Err:
        chat = None
        completions = None

        def __init__(self):
            self.chat = self
            self.completions = self

        async def create(self, **kwargs):
            raise ConnectionError("azure")

    r = UnifiedResponder(_Err(), "gpt-4o-mini", retriever=_stub_retriever, max_retries=0)
    out = await r.respond("kolika km")
    assert out.error and out.error.startswith("llm_error:")


@pytest.mark.asyncio
async def test_no_candidates_returns_error():
    async def _empty(query, k):
        return []

    fake = _FakeLLM('{"tool_id": null}')
    r = UnifiedResponder(fake, "gpt-4o-mini", retriever=_empty)
    out = await r.respond("nešto")
    assert out.error == "no_candidates"
    assert fake.calls == 0


@pytest.mark.asyncio
async def test_clarify_path_returns_question():
    fake = _FakeLLM(
        '{"tool_id":"get_VehicleCalendar","params":{},"needs_confirm":false,'
        '"response_text":null,"needs_clarify":true,'
        '"clarify_question":"Za koje vozilo?","clarify_options":["DA053F","ZG0001"],'
        '"reasoning":"missing vehicle","confidence":0.6}'
    )
    r = UnifiedResponder(fake, "gpt-4o-mini", retriever=_stub_retriever)
    out = await r.respond("rezervacije")
    assert out.needs_clarify is True
    assert out.clarify_question == "Za koje vozilo?"
    assert "DA053F" in out.clarify_options


@pytest.mark.asyncio
async def test_mutation_path_sets_needs_confirm():
    fake = _FakeLLM(
        '{"tool_id":"get_MasterData","params":{"value":30000},"needs_confirm":true,'
        '"response_text":null,"reasoning":"mutation","confidence":0.9}'
    )
    r = UnifiedResponder(fake, "gpt-4o-mini", retriever=_stub_retriever)
    out = await r.respond("unesi 30000 km")
    assert out.needs_confirm is True
    assert out.params == {"value": 30000}


def test_decision_is_actionable_property():
    d1 = UnifiedDecision(tool_id="get_MasterData", confidence=0.9)
    assert d1.is_actionable is True
    d2 = UnifiedDecision(tool_id=None, error="x")
    assert d2.is_actionable is False
    d3 = UnifiedDecision(tool_id="get_MasterData", error="x")
    assert d3.is_actionable is False


def test_format_identity_shows_only_present_fields():
    text = _format_identity({"first_name": "Filip", "vehicle_name": "DA053F"})
    assert "Filip" in text
    assert "DA053F" in text
    assert "Tenant" not in text  # not provided


def test_format_tools_truncates_to_30():
    cands = [
        {"tool_id": f"get_T{i}", "method": "GET", "intent_summary": "x"}
        for i in range(40)
    ]
    text = _format_tools(cands)
    # Only 30 should appear
    assert "get_T29" in text
    assert "get_T30" not in text


def test_format_history_returns_empty_marker_for_empty():
    assert "nema povijesti" in _format_history([])


def test_format_history_truncates_long_turn():
    turns = [{"user": "x" * 300, "bot": "y" * 300}]
    text = _format_history(turns)
    # Each side capped at 140
    assert "x" * 141 not in text
    assert "y" * 141 not in text
