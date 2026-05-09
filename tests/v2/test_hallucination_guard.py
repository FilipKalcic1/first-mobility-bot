"""Unit tests for hallucination_guard."""
from __future__ import annotations

from services.v2.hallucination_guard import check


def test_safe_when_response_matches_api():
    out = check("Tvoja kilometraža je 34 521 km.", {"TotalKm": 34521})
    assert out.is_safe is True


def test_safe_when_partial_match():
    out = check("Tvoja km: 34521.", {"TotalKm": "34521"})
    assert out.is_safe is True


def test_hallucinated_number_flagged():
    out = check(
        "Imaš 34 521 km i 3 rezervacije za sutra.",
        {"TotalKm": 34521},  # totalCount/3 NOT in api_data
    )
    # 34 521 matches; 3 is too small to flag (ordinal-like)
    # Adjust test — use a clearly hallucinated number
    out2 = check(
        "Imaš 34 521 km. Tvoj saldo je 12 345 EUR.",
        {"TotalKm": 34521},
    )
    assert out2.is_safe is False
    assert any("12 345" in c or "12345" in c for c in out2.suspicious_claims)


def test_hallucinated_plate_flagged():
    out = check(
        "Vozilo XY999AB ima km 34521.",
        {"TotalKm": 34521, "registracija": "DA053F"},
    )
    assert out.is_safe is False
    assert any("XY999AB" in c for c in out.suspicious_claims)


def test_correct_plate_passes():
    out = check(
        "Vozilo DA053F ima km 34521.",
        {"TotalKm": 34521, "registracija": "DA053F"},
    )
    assert out.is_safe is True


def test_empty_response_safe():
    out = check("", {})
    assert out.is_safe is True


def test_no_api_data_skips_check():
    out = check("Imaš 34521 km.", None)
    assert out.is_safe is True  # can't verify; trust


def test_short_numbers_not_flagged_as_hallucination():
    """Ordinal-like '1', '2', '3' must not flag as suspicious."""
    out = check("Imaš 1 rezervaciju.", {"items": ["one"]})
    assert out.is_safe is True


def test_date_hallucination_flagged():
    out = check(
        "Sljedeći servis: 2027-12-31.",
        {"NextServiceDate": "2026-08-15"},
    )
    assert out.is_safe is False
    assert any("2027-12-31" in c for c in out.suspicious_claims)


def test_nested_data_searched():
    out = check(
        "Vozilo: DA053F, marka: Škoda.",
        {"vehicle": {"plate": "DA053F", "make": "Škoda"}},
    )
    assert out.is_safe is True


def test_dataclass_default():
    from services.v2.hallucination_guard import HallucinationCheck
    h = HallucinationCheck(is_safe=True)
    assert h.suspicious_claims == []
    assert h.reason == ""
