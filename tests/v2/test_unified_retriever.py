"""Unit tests for UnifiedRetriever."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.v2.unified_retriever import UnifiedRetriever


class _FakeRecognition:
    def __init__(self, candidates):
        self._candidates = candidates

    async def recognize(self, query):
        return SimpleNamespace(
            tool_id=None,
            candidates=self._candidates,
            error=None,
        )


class _FakeRegistry:
    def __init__(self, tools):
        self._tools = tools

    @property
    def tools(self):
        return self._tools

    def tool_id_of(self, tool):
        if isinstance(tool, dict):
            return tool.get("operation_id") or tool.get("tool_id")
        return getattr(tool, "operation_id", None)


def _make_candidate(tid, score=0.8, method="GET", purpose=""):
    return SimpleNamespace(
        tool_id=tid, anchor_score=score, method=method, purpose=purpose,
    )


@pytest.fixture
def tkb_path():
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8",
    ) as f:
        json.dump({
            "tools": {
                "get_MasterData": {
                    "intent_summary": "Snapshot vozila",
                    "returns": {"TotalKm": "km", "registracija": "plate"},
                },
            }
        }, f, ensure_ascii=False)
        path = Path(f.name)
    yield path
    path.unlink()


@pytest.mark.asyncio
async def test_retrieve_top_k_returns_dicts(tkb_path):
    rec = _FakeRecognition([
        _make_candidate("get_MasterData"),
        _make_candidate("get_VehicleCalendar"),
    ])
    reg = _FakeRegistry([
        {"operation_id": "get_MasterData", "method": "GET",
         "summary": "vehicle snapshot", "required_params": []},
        {"operation_id": "get_VehicleCalendar", "method": "GET",
         "summary": "reservation list", "required_params": []},
    ])
    retr = UnifiedRetriever(rec, reg, tkb_path=tkb_path)
    retr.load()
    out = await retr("kolika km", 30)
    assert len(out) == 2
    assert out[0]["tool_id"] == "get_MasterData"
    assert "TotalKm" in out[0]["returns"]


@pytest.mark.asyncio
async def test_retrieve_uses_tkb_when_available(tkb_path):
    rec = _FakeRecognition([_make_candidate("get_MasterData")])
    reg = _FakeRegistry([
        {"operation_id": "get_MasterData", "method": "GET",
         "summary": "boring", "required_params": []},
    ])
    retr = UnifiedRetriever(rec, reg, tkb_path=tkb_path)
    out = await retr("x")
    assert out[0]["intent_summary"] == "Snapshot vozila"


@pytest.mark.asyncio
async def test_retrieve_falls_back_to_summary_when_no_tkb():
    # Empty TKB file
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8",
    ) as f:
        json.dump({"tools": {}}, f)
        empty_path = Path(f.name)
    try:
        rec = _FakeRecognition([_make_candidate("get_RareTool")])
        reg = _FakeRegistry([
            {"operation_id": "get_RareTool", "method": "GET",
             "summary": "rare summary text", "required_params": ["id"]},
        ])
        retr = UnifiedRetriever(rec, reg, tkb_path=empty_path)
        out = await retr("rare query")
        assert out[0]["intent_summary"] == "rare summary text"
        assert out[0]["required_params"] == ["id"]
    finally:
        empty_path.unlink()


@pytest.mark.asyncio
async def test_retrieve_caps_at_top_k(tkb_path):
    rec = _FakeRecognition([
        _make_candidate(f"get_T{i}") for i in range(50)
    ])
    reg = _FakeRegistry([
        {"operation_id": f"get_T{i}", "method": "GET", "summary": "x", "required_params": []}
        for i in range(50)
    ])
    retr = UnifiedRetriever(rec, reg, tkb_path=tkb_path)
    out = await retr("anything", top_k=15)
    assert len(out) == 15


@pytest.mark.asyncio
async def test_retrieve_handles_missing_registry_entry(tkb_path):
    rec = _FakeRecognition([_make_candidate("get_GhostTool")])
    reg = _FakeRegistry([])  # no matching tool in registry
    retr = UnifiedRetriever(rec, reg, tkb_path=tkb_path)
    out = await retr("ghost")
    # Still returns something — fallback to tool_id as intent
    assert len(out) == 1
    assert out[0]["tool_id"] == "get_GhostTool"


def test_load_is_idempotent(tkb_path):
    rec = _FakeRecognition([])
    reg = _FakeRegistry([])
    retr = UnifiedRetriever(rec, reg, tkb_path=tkb_path)
    retr.load()
    n1 = len(retr._tkb)
    retr.load()
    n2 = len(retr._tkb)
    assert n1 == n2 == 1


def test_missing_tkb_path_does_not_raise():
    rec = _FakeRecognition([])
    reg = _FakeRegistry([])
    fake_path = Path("/no/such/file.json")
    retr = UnifiedRetriever(rec, reg, tkb_path=fake_path)
    retr.load()
    assert retr._tkb == {}
