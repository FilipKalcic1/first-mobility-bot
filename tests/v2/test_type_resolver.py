"""Unit tests for the *TypeId resolver (Filip 2026-05-27)."""
from __future__ import annotations

from services.v2 import type_resolver as tr


# ---- build_typeid_map ----

def test_build_map_links_typeid_param_to_types_endpoint():
    tools = [
        {"operation_id": "post_Expenses", "method": "POST", "path": "/Expenses",
         "parameters": {"ExpenseTypeId": {"name": "ExpenseTypeId", "param_type": "integer"}}},
        {"operation_id": "get_ExpenseTypes", "method": "GET", "path": "/ExpenseTypes",
         "output_keys": ["Id", "Name", "Description"], "parameters": {}},
    ]
    m = tr.build_typeid_map(tools)
    assert m == {"expensetypeid": "get_ExpenseTypes"}


def test_build_map_skips_typeid_without_clean_endpoint():
    # AcquiringTypeId has no /AcquiringTypes list endpoint → not mapped.
    tools = [
        {"operation_id": "post_X", "method": "POST", "path": "/X",
         "parameters": {"AcquiringTypeId": {"name": "AcquiringTypeId", "param_type": "integer"}}},
    ]
    assert tr.build_typeid_map(tools) == {}


def test_build_map_uses_lookup_endpoint():
    # No /PartnerTypes table, but /Lookup/PartnerTypeId exists → resolvable.
    tools = [
        {"operation_id": "post_Partners", "method": "POST", "path": "/Partners",
         "parameters": {"PartnerTypeId": {"name": "PartnerTypeId", "param_type": "integer"}}},
        {"operation_id": "get_Lookup_PartnerTypeId", "method": "GET",
         "path": "/Lookup/PartnerTypeId", "output_keys": ["Id", "Name"], "parameters": {}},
    ]
    assert tr.build_typeid_map(tools) == {"partnertypeid": "get_Lookup_PartnerTypeId"}


def test_build_map_prefers_lookup_over_types_table():
    tools = [
        {"operation_id": "post_X", "method": "POST", "path": "/X",
         "parameters": {"ExpenseTypeId": {"name": "ExpenseTypeId", "param_type": "integer"}}},
        {"operation_id": "get_ExpenseTypes", "method": "GET", "path": "/ExpenseTypes",
         "output_keys": ["Id", "Name"], "parameters": {}},
        {"operation_id": "get_Lookup_ExpenseTypeId", "method": "GET",
         "path": "/Lookup/ExpenseTypeId", "output_keys": ["Id", "Name"], "parameters": {}},
    ]
    assert tr.build_typeid_map(tools)["expensetypeid"] == "get_Lookup_ExpenseTypeId"


def test_build_map_requires_name_field_in_types_endpoint():
    tools = [
        {"operation_id": "post_X", "method": "POST", "path": "/X",
         "parameters": {"FooTypeId": {"name": "FooTypeId", "param_type": "integer"}}},
        # /FooTypes exists but has no Name → can't resolve word→id → skip.
        {"operation_id": "get_FooTypes", "method": "GET", "path": "/FooTypes",
         "output_keys": ["Id", "Code"], "parameters": {}},
    ]
    assert tr.build_typeid_map(tools) == {}


# ---- rows_to_pairs ----

def test_rows_to_pairs_plain_list():
    rows = [{"Id": 3, "Name": "Gorivo"}, {"Id": 1, "Name": "Servis"}, {"Id": 9}]
    assert tr.rows_to_pairs(rows) == [(3, "Gorivo"), (1, "Servis")]  # row w/o Name dropped


def test_rows_to_pairs_wrapped():
    assert tr.rows_to_pairs({"Result": [{"Id": 2, "Name": "Registracija"}]}) == [(2, "Registracija")]


def test_rows_to_pairs_empty():
    assert tr.rows_to_pairs(None) == []
    assert tr.rows_to_pairs({}) == []


# ---- match ----

_PAIRS = [(1, "Servis"), (2, "Registracija"), (3, "Gorivo")]


def test_match_exact():
    assert tr.match("gorivo", _PAIRS)[0] == 3
    assert tr.match("GORIVO", _PAIRS)[0] == 3            # case-insensitive


def test_match_word_inside_sentence():
    assert tr.match("dodaj trošak za gorivo", _PAIRS)[0] == 3


def test_match_diacritics_insensitive():
    assert tr.match("registracija", _PAIRS)[0] == 2
    assert tr.match("cetvrtak", [(5, "Četvrtak")])[0] == 5


def test_match_no_unique_returns_options():
    mid, opts = tr.match("nepoznato", _PAIRS)
    assert mid is None and opts == _PAIRS


def test_match_ambiguous_returns_none():
    # two names appear in the text → not unique → ask
    assert tr.match("servis i gorivo", _PAIRS)[0] is None


def test_match_empty_rows():
    assert tr.match("gorivo", []) == (None, [])
