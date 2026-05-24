"""Unit tests for safe MobilityOne Filter-clause construction (Filip 2026-05-23).

Deterministic, no Azure. Covers clause shape (Field(op)Value), value
sanitization (allowlist + SQL blocklist + length cap), Croatian characters,
and multi-clause joining.
"""
from __future__ import annotations

from services.v2.filter_builder import (
    build_filter_clause, combine_filters, sanitize_filter_value,
)


def test_build_clause_basic():
    assert build_filter_clause("LicencePlate", "DA533") == "LicencePlate(=)DA533"


def test_build_clause_custom_operator():
    assert build_filter_clause("Status", "aktiv", operator="(contains)") == "Status(contains)aktiv"


def test_build_clause_empty_field_or_value():
    assert build_filter_clause("", "DA533") == ""
    assert build_filter_clause("LicencePlate", "") == ""


def test_sanitize_strips_sql_injection():
    out = sanitize_filter_value("DA533; DROP TABLE vehicles --")
    assert "DROP" not in out
    assert "--" not in out
    assert "DA533" in out


def test_sanitize_strips_disallowed_chars():
    out = sanitize_filter_value("DA<533>'\"")
    assert out == "DA533"


def test_sanitize_length_cap():
    assert len(sanitize_filter_value("A" * 1000)) == 500


def test_sanitize_keeps_croatian_chars():
    assert sanitize_filter_value("Škoda") == "Škoda"


def test_combine_filters_joins_with_and():
    a = build_filter_clause("LicencePlate", "DA533")
    b = build_filter_clause("Status", "aktiv")
    assert combine_filters(a, b) == "LicencePlate(=)DA533 and Status(=)aktiv"


def test_combine_filters_skips_empty():
    assert combine_filters("LicencePlate(=)DA533", "") == "LicencePlate(=)DA533"
    assert combine_filters("", "") == ""
