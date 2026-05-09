"""Unit tests for crisis_detector — mental health signal interception."""
from __future__ import annotations

import pytest

from services.v2.crisis_detector import detect


# ---- ACUTE — must trigger ----

@pytest.mark.parametrize("text", [
    "ubit ću se",
    "ubit cu se",
    "ubili ćemo se",  # plural form
    "ne želim više živjeti",
    "ne zelim vise zivjeti",  # without diacritics
    "život nema smisla više",
    "zivot nema smisla",
    "ne vidim izlaz iz ovoga",
    "bolje da umrem",
    "bolje da me nema",
    "razmišljam o samoubojstvu",
    "samoubojica sam",
    "trebam si naškoditi",
    "želim povrijediti sebe",
    "I want to kill myself",
    "thinking about suicide",
    "want to end my life",
    "self-harm thoughts",
    "I don't want to live",
])
def test_acute_signals_trigger(text):
    sig = detect(text)
    assert sig.detected is True
    assert sig.severity == "acute"
    assert "116 123" in sig.response
    assert "Plavi telefon" in sig.response


def test_acute_response_mentions_emergency_112():
    sig = detect("ubit ću se")
    assert "112" in sig.response


def test_acute_response_does_not_continue_fleet_flow():
    sig = detect("ubit ću se")
    # Response must be a stopping point, not lead to fleet API
    assert "vozilo" not in sig.response.lower()
    assert "rezervacij" not in sig.response.lower()
    assert "kilometr" not in sig.response.lower()


# ---- CONCERN — softer trigger ----

@pytest.mark.parametrize("text", [
    "ne mogu više",
    "očajan sam",
    "ocajna sam",
    "sve je sranje",
    "ne znam što ću sa sobom",
])
def test_concern_signals_trigger(text):
    sig = detect(text)
    assert sig.detected is True
    assert sig.severity == "concern"
    assert "116 123" in sig.response


def test_concern_response_invites_to_resume():
    sig = detect("ne mogu više")
    assert "vrat" in sig.response.lower() or "fleet" in sig.response.lower() or "nastavlja" in sig.response.lower()


# ---- FALSE POSITIVE GUARDS ----

@pytest.mark.parametrize("text", [
    "ubit ću tu lozinku ako mi se javi",
    "kill the server",
    "ubit ću servis prije deploya",
    "kill the engine, prazna baterija",
    "ubit ću tu aplikaciju",
])
def test_figurative_kill_does_not_trigger(text):
    sig = detect(text)
    assert sig.detected is False


@pytest.mark.parametrize("text", [
    "kolika mi je km",
    "moja registracija",
    "rezerviraj auto za sutra",
    "Bok",
    "udario sam autom",  # case-related, not crisis
    "pokvario se auto",
])
def test_normal_fleet_queries_dont_trigger(text):
    sig = detect(text)
    assert sig.detected is False
    assert sig.severity == "none"


def test_empty_query_no_trigger():
    sig = detect("")
    assert sig.detected is False


def test_whitespace_query_no_trigger():
    sig = detect("   ")
    assert sig.detected is False


# ---- Mixed case / partial match ----

def test_acute_phrase_mid_sentence():
    sig = detect("u zadnje vrijeme razmišljam ubit ću se zbog svega")
    assert sig.detected is True
    assert sig.severity == "acute"


def test_capitalization_irrelevant():
    sig = detect("UBIT ĆU SE")
    assert sig.detected is True


def test_matched_phrase_returned():
    sig = detect("ne želim više živjeti uopće")
    assert sig.matched_phrase is not None
    assert "živjeti" in sig.matched_phrase or "ne želim" in sig.matched_phrase


# ---- Severity priority ----

def test_acute_takes_precedence_over_concern():
    """Text with both 'ne mogu više' (concern) AND 'ubit ću se' (acute):
    must register as ACUTE, not concern."""
    sig = detect("ne mogu više, ubit ću se")
    assert sig.severity == "acute"
