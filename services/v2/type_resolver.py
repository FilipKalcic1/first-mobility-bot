"""Resolve `*TypeId` foreign-key params from the user's words (Filip 2026-05-27).

A `*TypeId` param (ExpenseTypeId, VehicleTypeId, …) is a foreign key into a
`/…Types` table. The user speaks a word ("gorivo"); the API wants the id (3).
The `/…Types` GET endpoint returns rows of `{Id, Name}`, so the bot fetches it
and matches the user's word to a Name.

PURE module:
  - `build_typeid_map(tools)` reads the registry → {param: get_tool_id}.
  - `rows_to_pairs(rows)` normalizes an API response → [(id, name)].
  - `match(text, pairs)` does the word→id matching.
The engine injects the live fetch (`executor.execute(get_XTypes)`); this module
never does I/O, so it is fully unit-testable without a network/M1 token.
"""
from __future__ import annotations

import re

from services.text_normalizer import normalize_diacritics


def _norm(s: str) -> str:
    """Lowercase, strip Croatian diacritics, collapse whitespace.

    Delegates to the canonical translate-table normalizer — the previous
    NFKD-only implementation left `đ` intact (U+0111 has no canonical
    decomposition), so a type name like "Građevinski" never matched the
    user's diacritic-less "gradevinski"."""
    s = normalize_diacritics((s or "").strip().lower())
    return re.sub(r"\s+", " ", s).strip()


def build_typeid_map(tools) -> dict:
    """{param_name_lower: get_tool_id} for every `*TypeId` param that has a GET
    endpoint exposing `{Id, Name}`. Two sources, preferred in order:
      1. `/Lookup/{ParamName}` — the backend's purpose-built FK-resolution
         family, named by the EXACT param (e.g. /Lookup/ExpenseTypeId). Covers
         even params with no `/…Types` table (Acquiring/Activity/Partner).
      2. `/{Base}s` — the entity types table (e.g. ExpenseTypeId → /ExpenseTypes).
    Data-driven — grows automatically as the backend adds endpoints.

    `tools` = registry tools (list of dicts, or dict keyed by op_id)."""
    tool_list = list(tools.values()) if isinstance(tools, dict) else list(tools or [])
    by_lookup: dict = {}        # 'expensetypeid' -> 'get_Lookup_ExpenseTypeId'
    by_types_base: dict = {}    # 'expensetypes'  -> 'get_ExpenseTypes'
    typeid_params: set = set()
    for t in tool_list:
        if not isinstance(t, dict):
            continue
        for pn, pd in (t.get("parameters") or {}).items():
            if isinstance(pd, dict) and pn.endswith("TypeId"):
                typeid_params.add(pn)
        if (t.get("method") or "").upper() != "GET":
            continue
        oks = [str(k).lower() for k in (t.get("output_keys") or [])]
        if "id" not in oks or "name" not in oks:
            continue
        path = (t.get("path") or "").strip("/")
        if path.lower().startswith("lookup/") and path.count("/") == 1:
            by_lookup[path.split("/", 1)[1].lower()] = t.get("operation_id")
        elif "/" not in path and "{" not in path:
            by_types_base[path.lower()] = t.get("operation_id")
    out: dict = {}
    for pn in typeid_params:
        key = pn.lower()
        tool = by_lookup.get(key) or by_types_base.get((pn[:-2] + "s").lower())
        if tool:
            out[key] = tool
    return out


_LIST_KEYS = ("Result", "Results", "Data", "data", "Items", "items", "value")


def rows_to_pairs(rows) -> list:
    """Extract `[(Id, Name)]` from a `/…Types` API response (list or wrapped)."""
    if isinstance(rows, dict):
        for k in _LIST_KEYS:
            if isinstance(rows.get(k), list):
                rows = rows[k]
                break
        else:
            rows = [rows] if rows.get("Id") is not None else []
    if not isinstance(rows, list):
        return []
    pairs = []
    for r in rows:
        if isinstance(r, dict) and r.get("Id") is not None and r.get("Name"):
            pairs.append((r["Id"], str(r["Name"])))
    return pairs


def match(text: str, pairs: list) -> tuple:
    """Return `(matched_id, pairs)`. `matched_id` is set iff exactly ONE name
    matches the text — exact-normalized first, else a normalized substring
    (the type name appears as a word in the text, or vice-versa). When there is
    no unique match, `matched_id` is None and `pairs` serves as the pick-list.
    `(None, [])` when there are no rows."""
    if not pairs:
        return None, []
    nt = _norm(text)
    if nt:
        exact = [pid for pid, name in pairs if _norm(name) == nt]
        if len(exact) == 1:
            return exact[0], pairs
        contains = [
            pid for pid, name in pairs
            if _norm(name) and (_norm(name) in nt or nt in _norm(name))
        ]
        if len(contains) == 1:
            return contains[0], pairs
    return None, pairs
