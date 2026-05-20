"""Parity invariants for `config/tool_data.json` — the single source of truth.

Post Phase 2 consolidation (2026-05-15), the bot routes via ONE config file
that replaces what used to be three: `processed_tool_registry.json` +
`tool_knowledge_base.json` + `tool_anchor_enrichments.json` (latter two now
under `.archive.json`). This test asserts the structural invariants the
runtime relies on:

  1. Every tool has all 4 content fields (intent_summary, use_when,
     do_not_use_when, anchors)
  2. intent_summary is a non-empty string
  3. Anchor entries are non-empty lists of non-empty strings
  4. Each tool has >=5 anchors (sanity floor — anchor retrieval needs
     vocabulary diversity to match paraphrases)
  5. No duplicate anchor phrases within a tool's list
  6. tool_data.json operation_ids match processed_tool_registry.json
     (catches the case where someone re-runs sync_tools.py to get new
     tools from Swagger but forgets to hand-edit tool_data.json).

The STRICTER content-quality invariants (intent_summary fully unique
across tools, entity-noun present in intent) are NOT enforced —
current hand-curated data doesn't satisfy them and an LLM-regen attempt
was verified to regress quality (see scripts/regenerate_tool_data.py
docstring for postmortem).

When this test fails: hand-edit config/tool_data.json. Full runbook in
scripts/sync_tools.py.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TOOL_DATA_PATH = ROOT / "config" / "tool_data.json"
REGISTRY_PATH = ROOT / "config" / "processed_tool_registry.json"

MIN_ANCHORS_PER_TOOL = 5
REQUIRED_CONTENT_FIELDS = ("intent_summary", "use_when", "do_not_use_when", "anchors")


@pytest.fixture(scope="module")
def tool_data() -> dict:
    """The single source of truth — operation_id → full tool entry."""
    doc = json.loads(TOOL_DATA_PATH.read_text(encoding="utf-8"))
    tools = doc.get("tools", {})
    assert tools, f"tool_data at {TOOL_DATA_PATH} has no tools — read failed?"
    return tools


@pytest.fixture(scope="module")
def registry_ids() -> set[str]:
    data = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    ids = {t["operation_id"] for t in data.get("tools", []) if t.get("operation_id")}
    assert ids, f"registry at {REGISTRY_PATH} has no tools — read failed?"
    return ids


# ---------------------------------------------------------------------------
# 1. operation_id parity between tool_data and registry (catches stale merge)
# ---------------------------------------------------------------------------


def test_tool_data_operation_ids_match_registry(registry_ids, tool_data):
    td_ids = set(tool_data.keys())
    missing = registry_ids - td_ids
    extra = td_ids - registry_ids
    parts = []
    if missing:
        parts.append(
            f"in registry but NOT in tool_data: {len(missing)} tool(s). "
            f"First 10: {sorted(missing)[:10]}"
        )
    if extra:
        parts.append(
            f"in tool_data but NOT in registry: {len(extra)} tool(s). "
            f"First 10: {sorted(extra)[:10]}"
        )
    if parts:
        parts.append("FIX: run `python scripts/build_tool_data.py` then re-run this test.")
    assert not (missing or extra), " | ".join(parts)


# ---------------------------------------------------------------------------
# 2. Required content fields present (catches incomplete merge / regen)
# ---------------------------------------------------------------------------


def test_every_tool_has_all_required_content_fields(tool_data):
    bad: list[str] = []
    for op_id, t in tool_data.items():
        if not isinstance(t, dict):
            bad.append(f"{op_id}: not a dict (got {type(t).__name__})")
            continue
        missing = [f for f in REQUIRED_CONTENT_FIELDS if f not in t]
        if missing:
            bad.append(f"{op_id}: missing {missing}")
    assert not bad, (
        f"{len(bad)} tool(s) have incomplete content. "
        f"Required: {list(REQUIRED_CONTENT_FIELDS)}. "
        f"FIX: rerun `python scripts/regenerate_tool_data.py`. "
        f"First 5: {bad[:5]}"
    )


def test_every_tool_has_non_empty_intent_summary(tool_data):
    """intent_summary feeds the router prompt — empty = invisible tool."""
    bad: list[str] = []
    for op_id, t in tool_data.items():
        s = t.get("intent_summary") if isinstance(t, dict) else None
        if not isinstance(s, str) or not s.strip():
            bad.append(op_id)
    assert not bad, (
        f"{len(bad)} tool(s) have empty/missing intent_summary. "
        f"First 5: {bad[:5]}"
    )


def test_every_tool_has_non_empty_use_when(tool_data):
    bad = [op for op, t in tool_data.items() if not (t.get("use_when") or [])]
    assert not bad, (
        f"{len(bad)} tool(s) have empty use_when. "
        f"First 5: {bad[:5]}"
    )


def test_every_tool_has_non_empty_do_not_use_when(tool_data):
    bad = [op for op, t in tool_data.items() if not (t.get("do_not_use_when") or [])]
    assert not bad, (
        f"{len(bad)} tool(s) have empty do_not_use_when. "
        f"First 5: {bad[:5]}"
    )


# ---------------------------------------------------------------------------
# 3. Anchor shape — non-empty list of non-empty strings, >= MIN per tool,
#    no duplicates within a tool's list
# ---------------------------------------------------------------------------


def test_every_anchor_entry_is_non_empty_list_of_non_empty_strings(tool_data):
    bad: list[str] = []
    for op_id, t in tool_data.items():
        phrases = t.get("anchors")
        if not isinstance(phrases, list) or not phrases:
            bad.append(f"{op_id}: not a non-empty list (got {type(phrases).__name__})")
            continue
        for i, p in enumerate(phrases):
            if not isinstance(p, str) or not p.strip():
                bad.append(f"{op_id}[{i}]: not a non-empty string ({p!r})")
                break
    assert not bad, (
        f"{len(bad)} tool(s) have malformed anchor entries. "
        f"First 5: {bad[:5]}"
    )


def test_every_tool_has_at_least_min_anchors(tool_data):
    too_few = {
        op: len(t.get("anchors") or [])
        for op, t in tool_data.items()
        if isinstance(t.get("anchors"), list) and len(t["anchors"]) < MIN_ANCHORS_PER_TOOL
    }
    assert not too_few, (
        f"{len(too_few)} tool(s) have fewer than {MIN_ANCHORS_PER_TOOL} anchors. "
        f"Anchor retrieval needs vocabulary diversity to match paraphrases. "
        f"First 5: {dict(list(too_few.items())[:5])}"
    )


def test_no_duplicate_anchor_phrases_within_a_tool(tool_data):
    """Duplicates waste embedding budget and bias cosine retrieval."""
    dups: list[str] = []
    for op_id, t in tool_data.items():
        phrases = t.get("anchors")
        if not isinstance(phrases, list):
            continue
        normalized = [p.strip().lower() for p in phrases if isinstance(p, str)]
        if len(normalized) != len(set(normalized)):
            seen = set()
            for p in normalized:
                if p in seen:
                    dups.append(f"{op_id}: duplicate {p!r}")
                    break
                seen.add(p)
    assert not dups, (
        f"{len(dups)} tool(s) have duplicate anchor phrases. "
        f"First 5: {dups[:5]}"
    )
