"""Tests for L2a IntentTypeClassifier."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

import pytest

from services.v2.intent_type import (
    KIND_ACTION_OR_COMPLAINT, KIND_FLOW_REQUEST, KIND_OTHER,
    KIND_QUESTION_ABOUT_SELF, IntentTypeClassifier,
)


@dataclass
class _Choice:
    message: object


@dataclass
class _Msg:
    content: Optional[str]


@dataclass
class _Response:
    choices: list


class _FakeChat:
    def __init__(self, completions):
        self.completions = completions


class _FakeCompletions:
    def __init__(self):
        self.queue = []

    def push(self, raw):
        self.queue.append(raw)

    async def create(self, **kwargs):
        if not self.queue:
            raise RuntimeError("empty queue")
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return _Response(choices=[_Choice(message=_Msg(content=item))])


class _FakeLLM:
    def __init__(self, completions):
        self.chat = _FakeChat(completions)


def _classifier():
    c = _FakeCompletions()
    return IntentTypeClassifier(_FakeLLM(c), "gpt-4o-mini"), c


@pytest.mark.asyncio
async def test_question_about_self():
    cls, llm = _classifier()
    llm.push(json.dumps({"kind": "question_about_self", "confidence": 0.95}))
    r = await cls.classify("kolika mi je km")
    assert r.kind == KIND_QUESTION_ABOUT_SELF
    assert r.confidence == 0.95


@pytest.mark.asyncio
async def test_action_or_complaint():
    cls, llm = _classifier()
    llm.push(json.dumps({"kind": "action_or_complaint", "confidence": 0.9}))
    r = await cls.classify("auto mi ne radi kočnica")
    assert r.kind == KIND_ACTION_OR_COMPLAINT


@pytest.mark.asyncio
async def test_flow_request():
    cls, llm = _classifier()
    llm.push(json.dumps({"kind": "flow_request", "confidence": 0.92}))
    r = await cls.classify("rezerviraj auto sutra")
    assert r.kind == KIND_FLOW_REQUEST


@pytest.mark.asyncio
async def test_other_for_manager_query():
    cls, llm = _classifier()
    llm.push(json.dumps({"kind": "other", "confidence": 0.85}))
    r = await cls.classify("popis svih Fordova u prodaji")
    assert r.kind == KIND_OTHER


@pytest.mark.asyncio
async def test_low_confidence_falls_back_to_question_about_self():
    """Pass 4 USER critique: vozač never waits on basic info."""
    cls, llm = _classifier()
    llm.push(json.dumps({"kind": "other", "confidence": 0.4}))
    r = await cls.classify("ambiguous")
    assert r.kind == KIND_QUESTION_ABOUT_SELF
    assert "low_confidence_fallback" in r.reasoning


@pytest.mark.asyncio
async def test_empty_query_returns_other_no_llm_call():
    cls, llm = _classifier()
    r = await cls.classify("")
    assert r.kind == KIND_OTHER


@pytest.mark.asyncio
async def test_invalid_json_falls_back_safely():
    cls, llm = _classifier()
    llm.push("not json {{{{")
    r = await cls.classify("anything")
    # Parse fail → confidence 0 → safe fallback to question_about_self
    assert r.kind == KIND_QUESTION_ABOUT_SELF


@pytest.mark.asyncio
async def test_llm_exception_falls_back_safely():
    cls, llm = _classifier()
    llm.queue.append(ConnectionError("azure down"))
    r = await cls.classify("X")
    # LLM error → safe fallback to question_about_self
    assert r.kind == KIND_QUESTION_ABOUT_SELF


@pytest.mark.asyncio
async def test_unknown_kind_normalized_to_other():
    cls, llm = _classifier()
    llm.push(json.dumps({"kind": "fly_to_mars", "confidence": 0.95}))
    r = await cls.classify("X")
    # Above safe-fallback threshold so kind sticks; but invalid kind
    # gets normalized in parse step → other
    assert r.kind == KIND_OTHER


@pytest.mark.asyncio
async def test_confidence_clamped():
    cls, llm = _classifier()
    llm.push(json.dumps({"kind": "question_about_self", "confidence": 1.7}))
    r = await cls.classify("X")
    assert r.confidence == 1.0
