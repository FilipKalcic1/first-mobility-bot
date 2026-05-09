"""Unit tests for meta_intents."""
from __future__ import annotations

import pytest

from services.v2.meta_intents import detect, MetaIntent


@pytest.mark.parametrize("text,kind", [
    ("tko si ti?", "self_identity"),
    ("ti si AI?", "self_identity"),
    ("are you a bot", "self_identity"),
    ("kojim si modelom", "self_identity"),
])
def test_self_identity_detected(text, kind):
    out = detect(text)
    assert out.detected
    assert out.kind == kind
    assert "AI" in out.response or "bot" in out.response.lower()


@pytest.mark.parametrize("text", [
    "hoću pravog čovjeka",
    "trebam ljudsku osobu",
    "spoji me s operaterom",
    "talk to a human",
])
def test_human_handoff(text):
    out = detect(text)
    assert out.detected
    assert out.kind == "human_handoff"
    assert "manager" in out.response.lower() or "podršku" in out.response.lower()


@pytest.mark.parametrize("text", [
    "jučer si mi rekao krivo",
    "to je greška",
    "nisi u pravu",
])
def test_bug_report(text):
    out = detect(text)
    assert out.detected
    assert out.kind == "bug_report"


@pytest.mark.parametrize("text", [
    "vrati to što si napravio",
    "poništi to",
    "cancel my last action",
])
def test_undo_request(text):
    out = detect(text)
    assert out.detected
    assert out.kind == "undo_request"
    assert "undo" in out.response.lower() or "otkaži" in out.response.lower()


@pytest.mark.parametrize("text", [
    "kako si?",
    "how are you",
    "šta ima",
])
def test_personal_chat(text):
    out = detect(text)
    assert out.detected
    assert out.kind == "personal_chat"


@pytest.mark.parametrize("text", [
    "glupi bot",
    "fuck",
])
def test_harassment_handled_gracefully(text):
    out = detect(text)
    assert out.detected
    assert out.kind == "harassment"
    # Response sets boundary, doesn't escalate
    assert len(out.response) > 20


@pytest.mark.parametrize("text", [
    "kakvo je vrijeme",
    "what is the time",
    "koji je datum",
])
def test_weather_time_oos(text):
    out = detect(text)
    assert out.detected
    assert out.kind == "weather_time"


@pytest.mark.parametrize("text", [
    "kolika km",
    "moje rezervacije",
    "obriši rezervaciju za sutra",
    "Bok",
])
def test_normal_queries_not_meta(text):
    out = detect(text)
    assert out.detected is False


def test_empty_query():
    assert detect("").detected is False


def test_dataclass_default():
    m = MetaIntent(detected=False)
    assert m.kind == ""
    assert m.response is None
