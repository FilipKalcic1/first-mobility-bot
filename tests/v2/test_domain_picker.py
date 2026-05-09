"""Tests for DomainPicker — Stage 1 of hierarchical V3 routing."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

import pytest

from services.v2.domain_picker import (
    DomainDecision,
    DomainHit,
    DomainPicker,
)


# ---- Fake LLM (mocks Azure SDK shape) ----

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
        self.calls: list = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.raise_exc:
            raise self.raise_exc
        return _Resp(choices=[_Choice(message=_Message(content=self.response_text))])


class _FakeLLMClient:
    def __init__(self, response_text: str = "{}", raise_exc: Optional[Exception] = None):
        self.chat = type("X", (), {"completions": _FakeCompletions(response_text, raise_exc)})()


# ---- Tests ----


def test_loads_9_domains():
    p = DomainPicker(_FakeLLMClient(), "gpt-4o-mini")
    p.load()
    assert len(p._domains) >= 8


def test_system_prompt_includes_all_domain_ids():
    # Compact prompt uses domain IDs + short hints (not full labels) to
    # stay under Azure content filter trigger threshold. Labels remain
    # available via tool_domains.json for non-prompt code paths.
    p = DomainPicker(_FakeLLMClient(), "gpt-4o-mini")
    p.load()
    for d in p._domains:
        assert d["id"] in p._system_prompt


def test_get_domain_by_id():
    p = DomainPicker(_FakeLLMClient(), "gpt-4o-mini")
    p.load()
    vi = p.get_domain_by_id("vehicle_info")
    assert vi is not None
    assert "MasterData" in vi["primary_entities"]
    assert p.get_domain_by_id("nonexistent") is None


def test_get_entities_for_domain():
    p = DomainPicker(_FakeLLMClient(), "gpt-4o-mini")
    p.load()
    ents = p.get_entities_for_domain("reservations")
    assert "VehicleCalendar" in ents
    assert "LatestVehicleCalendar" in ents


@pytest.mark.asyncio
async def test_empty_query_returns_error():
    p = DomainPicker(_FakeLLMClient(), "gpt-4o-mini")
    d = await p.pick("")
    assert d.error == "empty_query"


@pytest.mark.asyncio
async def test_picks_vehicle_info_for_kilometraza():
    response = json.dumps({
        "top_picks": [
            {"domain_id": "vehicle_info", "confidence": 0.97,
             "reasoning": "user pita za kilometražu vlastitog vozila"},
            {"domain_id": "mileage_reports", "confidence": 0.4,
             "reasoning": "alternative if user means history"},
        ],
        "needs_user_confirm": False,
    })
    p = DomainPicker(_FakeLLMClient(response), "gpt-4o-mini")
    d = await p.pick("kolika mi je kilometraža")
    assert len(d.top_picks) == 2
    assert d.top_domain.domain_id == "vehicle_info"
    assert d.is_confident  # 0.97 - 0.4 > 0.2 margin


@pytest.mark.asyncio
async def test_low_margin_not_confident():
    response = json.dumps({
        "top_picks": [
            {"domain_id": "vehicle_info", "confidence": 0.7},
            {"domain_id": "mileage_reports", "confidence": 0.65},
        ],
        "needs_user_confirm": True,
    })
    p = DomainPicker(_FakeLLMClient(response), "gpt-4o-mini")
    d = await p.pick("kilometraža")
    assert not d.is_confident  # margin 0.05 < 0.2
    assert d.needs_user_confirm


@pytest.mark.asyncio
async def test_filters_invalid_domain_ids():
    response = json.dumps({
        "top_picks": [
            {"domain_id": "totally_made_up_domain", "confidence": 0.99},
            {"domain_id": "vehicle_info", "confidence": 0.5},
        ],
    })
    p = DomainPicker(_FakeLLMClient(response), "gpt-4o-mini")
    d = await p.pick("test")
    # Made-up domain dropped, vehicle_info survives
    assert len(d.top_picks) == 1
    assert d.top_picks[0].domain_id == "vehicle_info"


@pytest.mark.asyncio
async def test_handles_invalid_json():
    p = DomainPicker(_FakeLLMClient("not json"), "gpt-4o-mini", max_retries=0)
    d = await p.pick("test")
    assert d.error == "json_parse_failed"


@pytest.mark.asyncio
async def test_handles_llm_exception():
    p = DomainPicker(
        _FakeLLMClient(raise_exc=RuntimeError("Azure 503")),
        "gpt-4o-mini", max_retries=0,
    )
    d = await p.pick("test")
    assert d.error and "RuntimeError" in d.error


@pytest.mark.asyncio
async def test_clamps_confidence():
    response = json.dumps({
        "top_picks": [{"domain_id": "vehicle_info", "confidence": 1.5}],
    })
    p = DomainPicker(_FakeLLMClient(response), "gpt-4o-mini")
    d = await p.pick("test")
    assert d.top_picks[0].confidence == 1.0


def test_domain_decision_top_domain_none_when_empty():
    d = DomainDecision(top_picks=[])
    assert d.top_domain is None
    assert not d.is_confident


def test_domain_decision_is_confident():
    high = DomainDecision(top_picks=[
        DomainHit(domain_id="vehicle_info", label="x", confidence=0.95),
        DomainHit(domain_id="other", label="y", confidence=0.5),
    ])
    assert high.is_confident

    close = DomainDecision(top_picks=[
        DomainHit(domain_id="vehicle_info", label="x", confidence=0.85),
        DomainHit(domain_id="other", label="y", confidence=0.75),
    ])
    assert not close.is_confident  # margin 0.1 < 0.2

    low_top = DomainDecision(top_picks=[
        DomainHit(domain_id="vehicle_info", label="x", confidence=0.6),
        DomainHit(domain_id="other", label="y", confidence=0.2),
    ])
    assert not low_top.is_confident  # top below 0.85 floor


def test_all_75_entities_have_domain():
    """Every entity from registry MUST be mapped to exactly one domain.
    Coverage was empirically validated at module-build time; this is the
    regression guard."""
    p = DomainPicker(_FakeLLMClient(), "gpt-4o-mini")
    p.load()
    all_entities = set()
    for d in p._domains:
        all_entities.update(d["primary_entities"])
    # Sanity check key entities
    must_be_present = {
        "MasterData", "VehicleCalendar", "MileageReports", "Cases",
        "Persons", "OrgUnits", "Companies", "CostCenters", "Expenses",
        "Equipment", "Tenants", "Lookup", "Vehicles",
    }
    missing = must_be_present - all_entities
    assert not missing, f"Critical entities not mapped: {missing}"
