"""Tests for DomainScopedToolPicker — Stage 2 of V3 routing."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

import pytest

from services.v2.domain_picker import DomainPicker
from services.v2.domain_scoped_picker import (
    DomainScopedToolPicker,
    ScopedDecision,
    ScopedToolHit,
    _extract_method,
    _is_mutating,
    _extract_modifier,
)


# ---- Fakes ----

@dataclass
class _Choice:
    message: object


@dataclass
class _Message:
    content: str


@dataclass
class _Resp:
    choices: list


class _FakeCompletions:
    def __init__(self, response_text: str = "{}", raise_exc: Optional[Exception] = None):
        self.response_text = response_text
        self.raise_exc = raise_exc

    async def create(self, **kwargs):
        if self.raise_exc:
            raise self.raise_exc
        return _Resp(choices=[_Choice(message=_Message(content=self.response_text))])


class _FakeLLMClient:
    def __init__(self, response_text: str = "{}", raise_exc: Optional[Exception] = None):
        self.chat = type("X", (), {"completions": _FakeCompletions(response_text, raise_exc)})()


class _FakeTool:
    def __init__(self, op_id: str, method: str = "GET"):
        self.tool_id = op_id
        self.method = method


class _FakeRegistry:
    def __init__(self, tool_ids: list[str]):
        self._tool_ids = set(tool_ids)
        self.tools = [_FakeTool(t) for t in tool_ids]

    def has_tool(self, tool_id: str) -> bool:
        return tool_id in self._tool_ids


# ---- Tests ----


def test_extract_method():
    assert _extract_method("get_MasterData") == "GET"
    assert _extract_method("post_AddCase") == "POST"
    assert _extract_method("delete_VehicleCalendar_id") == "DELETE"
    assert _extract_method("put_Vehicles_id") == "PUT"


def test_is_mutating():
    assert _is_mutating("delete_X") is True
    assert _is_mutating("post_X") is True
    assert _is_mutating("put_X") is True
    assert _is_mutating("patch_X") is True
    assert _is_mutating("get_X") is False


def test_extract_modifier_categories():
    assert "by ID" in _extract_modifier("delete_VehicleCalendar_id")
    assert "agregacija" in _extract_modifier("get_Expenses_Agg")
    assert "bulk delete" in _extract_modifier("delete_X_DeleteByCriteria")
    assert "specifičan dokument" in _extract_modifier("delete_X_id_documents_documentId")
    assert "lista dokumenata" in _extract_modifier("get_X_id_documents")
    assert "metadata" in _extract_modifier("get_X_id_metadata")


@pytest.mark.asyncio
async def test_empty_query_returns_error():
    picker = DomainScopedToolPicker(
        _FakeLLMClient(), "gpt-4o-mini",
        _FakeRegistry([]), DomainPicker(_FakeLLMClient(), "gpt-4o-mini"),
    )
    d = await picker.pick("", "vehicle_info")
    assert d.error == "empty_query"


@pytest.mark.asyncio
async def test_missing_domain_returns_error():
    picker = DomainScopedToolPicker(
        _FakeLLMClient(), "gpt-4o-mini",
        _FakeRegistry([]), DomainPicker(_FakeLLMClient(), "gpt-4o-mini"),
    )
    d = await picker.pick("test query", "")
    assert d.error == "missing_domain"


@pytest.mark.asyncio
async def test_picks_tool_within_domain():
    """Multi-tool domain → LLM picker path (bypass single-tool shortcut)."""
    response = json.dumps({
        "top_picks": [{
            "tool_id": "get_VehicleCalendar",
            "confidence": 0.95,
            "reasoning": "user asks for reservations"
        }],
        "is_mutating": False,
    })
    # Use reservations domain which has multiple tools — bypasses shortcut
    registry = _FakeRegistry([
        "get_VehicleCalendar", "delete_VehicleCalendar_id",
        "post_VehicleCalendar", "get_LatestVehicleCalendar",
    ])
    domain_picker = DomainPicker(_FakeLLMClient(), "gpt-4o-mini")
    domain_picker.load()

    picker = DomainScopedToolPicker(
        _FakeLLMClient(response), "gpt-4o-mini", registry, domain_picker,
    )
    d = await picker.pick("moje rezervacije", "reservations")
    assert d.top_pick is not None
    assert d.top_pick.tool_id == "get_VehicleCalendar"
    assert d.has_high_confidence


@pytest.mark.asyncio
async def test_single_tool_domain_shortcut_skips_llm():
    """When domain has only 1 tool, return it directly without LLM call.

    Critical fix from V3 e2e benchmark: vehicle_info has only get_MasterData,
    but LLM was returning empty top_picks. Shortcut prevents the bug.
    """
    # vehicle_info has only MasterData entity → 1 tool in real registry
    registry = _FakeRegistry(["get_MasterData"])
    domain_picker = DomainPicker(_FakeLLMClient(), "gpt-4o-mini")
    domain_picker.load()

    # LLM client should NEVER be called — confirm by passing exception
    picker = DomainScopedToolPicker(
        _FakeLLMClient(raise_exc=AssertionError("LLM should not be called!")),
        "gpt-4o-mini", registry, domain_picker,
    )
    d = await picker.pick("kilometraža", "vehicle_info")
    assert d.top_pick is not None
    assert d.top_pick.tool_id == "get_MasterData"
    assert d.has_high_confidence
    assert d.error is None


@pytest.mark.asyncio
async def test_drops_hallucinated_tool_id():
    """If LLM returns a tool not in registry, it must be dropped."""
    response = json.dumps({
        "top_picks": [
            {"tool_id": "get_DoesNotExist", "confidence": 0.95},
            {"tool_id": "get_MasterData", "confidence": 0.5},
        ],
    })
    registry = _FakeRegistry(["get_MasterData"])
    domain_picker = DomainPicker(_FakeLLMClient(), "gpt-4o-mini")
    domain_picker.load()

    picker = DomainScopedToolPicker(
        _FakeLLMClient(response), "gpt-4o-mini", registry, domain_picker,
    )
    d = await picker.pick("test", "vehicle_info")
    assert len(d.top_picks) == 1
    assert d.top_picks[0].tool_id == "get_MasterData"


@pytest.mark.asyncio
async def test_detects_mutating_method_from_tool_id():
    """Defense-in-depth: even if LLM forgets to mark is_mutating, we detect from method."""
    response = json.dumps({
        "top_picks": [{"tool_id": "post_AddMileage", "confidence": 0.9}],
        "is_mutating": False,  # LLM lies
    })
    registry = _FakeRegistry(["post_AddMileage"])
    domain_picker = DomainPicker(_FakeLLMClient(), "gpt-4o-mini")
    domain_picker.load()

    picker = DomainScopedToolPicker(
        _FakeLLMClient(response), "gpt-4o-mini", registry, domain_picker,
    )
    d = await picker.pick("unesi 30000 km", "mileage_reports")
    assert d.is_mutating is True  # detected from method


@pytest.mark.asyncio
async def test_clarify_question_passed_through():
    """LLM path passes clarify_question — uses multi-tool registry to skip shortcut."""
    response = json.dumps({
        "top_picks": [{"tool_id": "post_VehicleCalendar", "confidence": 0.9}],
        "is_mutating": True,
        "needs_clarify": True,
        "clarify_question": "Za koji period?",
        "missing_info": ["dates"],
    })
    # Multi-tool registry to bypass single-tool shortcut
    registry = _FakeRegistry([
        "post_VehicleCalendar", "get_VehicleCalendar",
        "delete_VehicleCalendar_id",
    ])
    domain_picker = DomainPicker(_FakeLLMClient(), "gpt-4o-mini")
    domain_picker.load()

    picker = DomainScopedToolPicker(
        _FakeLLMClient(response), "gpt-4o-mini", registry, domain_picker,
    )
    d = await picker.pick("trebam auto", "reservations")
    assert d.needs_clarify is True
    assert d.clarify_question == "Za koji period?"
    assert "dates" in d.missing_info


@pytest.mark.asyncio
async def test_handles_invalid_json():
    registry = _FakeRegistry([])
    domain_picker = DomainPicker(_FakeLLMClient(), "gpt-4o-mini")
    domain_picker.load()

    picker = DomainScopedToolPicker(
        _FakeLLMClient("not json"), "gpt-4o-mini", registry, domain_picker,
        max_retries=0,
    )
    d = await picker.pick("test", "vehicle_info")
    assert d.error == "json_parse_failed"


@pytest.mark.asyncio
async def test_handles_llm_exception():
    registry = _FakeRegistry([])
    domain_picker = DomainPicker(_FakeLLMClient(), "gpt-4o-mini")
    domain_picker.load()

    picker = DomainScopedToolPicker(
        _FakeLLMClient(raise_exc=RuntimeError("Azure 503")),
        "gpt-4o-mini", registry, domain_picker, max_retries=0,
    )
    d = await picker.pick("test", "vehicle_info")
    assert d.error and "RuntimeError" in d.error


def test_scoped_decision_top_pick_none_when_empty():
    d = ScopedDecision(top_picks=[])
    assert d.top_pick is None
    assert not d.has_high_confidence


def test_scoped_decision_high_confidence_threshold():
    d = ScopedDecision(top_picks=[
        ScopedToolHit(tool_id="get_X", method="GET", confidence=0.85),
    ])
    assert d.has_high_confidence

    d2 = ScopedDecision(top_picks=[
        ScopedToolHit(tool_id="get_X", method="GET", confidence=0.84),
    ])
    assert not d2.has_high_confidence
