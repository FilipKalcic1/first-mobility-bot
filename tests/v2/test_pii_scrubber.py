"""Tests for L0.5 PIIScrubber."""
from __future__ import annotations

from services.v2.pii_scrubber import PIIScrubber, Redaction


def test_empty_input_is_safe():
    s = PIIScrubber()
    r = s.scrub("")
    assert r.scrubbed_text == ""
    assert r.redactions == []


def test_no_pii_unchanged():
    s = PIIScrubber()
    text = "kolika mi je kilometraža"
    r = s.scrub(text)
    assert r.scrubbed_text == text
    assert r.redactions == []


def test_phone_number_redacted():
    s = PIIScrubber()
    r = s.scrub("Moj broj je 385955087196")
    assert "385955087196" not in r.scrubbed_text
    assert "[PHONE_REDACTED]" in r.scrubbed_text
    assert any(red.kind == "PHONE" for red in r.redactions)


def test_phone_boundary_does_not_redact_14plus_digit_number():
    """Digit boundary (Filip 2026-05-26): a 14+ digit run is not phone-shaped
    and must stay intact — previously the regex matched its first 12 digits."""
    s = PIIScrubber()
    r = s.scrub("Broj 12345678901234 ostaje")
    assert "12345678901234" in r.scrubbed_text
    assert not any(red.kind == "PHONE" for red in r.redactions)


def test_oib_redacted():
    s = PIIScrubber()
    r = s.scrub("Moj OIB je 12345678901")
    assert "12345678901" not in r.scrubbed_text
    assert "[OIB_REDACTED]" in r.scrubbed_text
    assert any(red.kind == "OIB" for red in r.redactions)


def test_jmbg_redacted():
    s = PIIScrubber()
    r = s.scrub("JMBG 1234567890123 evo ti")
    assert "1234567890123" not in r.scrubbed_text
    assert "[JMBG_REDACTED]" in r.scrubbed_text


def test_email_redacted():
    s = PIIScrubber()
    r = s.scrub("Pošalji na ivo.ivic@example.com molim")
    assert "ivo.ivic@example.com" not in r.scrubbed_text
    assert "[EMAIL_REDACTED]" in r.scrubbed_text


def test_iban_redacted():
    s = PIIScrubber()
    r = s.scrub("IBAN je HR1210010051863000160")
    assert "HR1210010051863000160" not in r.scrubbed_text
    assert "[IBAN_REDACTED]" in r.scrubbed_text


def test_credit_card_redacted():
    s = PIIScrubber()
    r = s.scrub("Kartica 4532-1234-5678-9012")
    assert "4532-1234-5678-9012" not in r.scrubbed_text
    assert "[CARD_REDACTED]" in r.scrubbed_text


def test_multiple_pii_in_one_query():
    s = PIIScrubber()
    r = s.scrub("Moj broj 385955087196 a OIB mi je 12345678901")
    assert "385955087196" not in r.scrubbed_text
    assert "12345678901" not in r.scrubbed_text
    kinds = {red.kind for red in r.redactions}
    assert "PHONE" in kinds
    assert "OIB" in kinds


def test_short_numbers_not_redacted():
    """5 km, year 2025, etc. — too short to be PII."""
    s = PIIScrubber()
    r = s.scrub("Vozio sam 1500 km u 2025 godini")
    assert r.scrubbed_text == "Vozio sam 1500 km u 2025 godini"
    assert r.redactions == []


def test_redaction_records_kind_and_length():
    s = PIIScrubber()
    r = s.scrub("Email: ana@firma.hr")
    assert len(r.redactions) == 1
    assert r.redactions[0].kind == "EMAIL"
    assert r.redactions[0].original_length == len("ana@firma.hr")


def test_phone_with_plus_prefix_redacted():
    s = PIIScrubber()
    r = s.scrub("Pozvao me +385955087196")
    assert "385955087196" not in r.scrubbed_text
    assert "[PHONE_REDACTED]" in r.scrubbed_text


def test_iban_takes_precedence_over_other_patterns():
    """IBAN starts with 2 letters; mustn't be eaten by digit patterns."""
    s = PIIScrubber()
    r = s.scrub("HR1210010051863000160 je moj račun")
    assert "[IBAN_REDACTED]" in r.scrubbed_text
    # No phone redaction inside the IBAN
    assert sum(1 for red in r.redactions if red.kind == "IBAN") == 1


def test_query_with_only_pii_yields_only_placeholders():
    s = PIIScrubber()
    r = s.scrub("385955087196 12345678901")
    assert "385955087196" not in r.scrubbed_text
    assert "12345678901" not in r.scrubbed_text
    # Both got replaced
    assert len(r.redactions) == 2
