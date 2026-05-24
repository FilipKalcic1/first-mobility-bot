"""One-off: correct context-param MISCLASSIFICATION in the current registry.

WHY (Filip 2026-05-24 audit): the swagger heuristic mis-tagged 55 params as
`dependency_source=context` that are NOT entity-id references — value fields
(Date/Code/Status/EntryType/Comment/Visibility/...) and non-string params
(int/array/bool). The executor then auto-injects an identity UUID string into
those slots → 422 / data corruption (14 of them are REQUIRED → guaranteed bad
call). The parser is now guarded (services/registry/swagger_parser.py:
_context_value_appropriate), but a live re-sync is blocked, so this corrects the
ALREADY-GENERATED registry files in place, using the SAME rule.

Effect of a demote: `dependency_source` context → user_input, `context_key`
cleared. The schema-builder then exposes the param to the LLM (so a stated value
is extracted) and the executor no longer injects a wrong UUID. Required ones
become user_input-required → asked (or honest refuse) instead of silently wrong.

Idempotent — re-running changes nothing. Usage:
    python scripts/fix_context_misclassification.py            # apply
    python scripts/fix_context_misclassification.py --dry-run  # report only
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from services.registry.swagger_parser import _context_value_appropriate  # noqa: E402
from services.v2.atomic_io import atomic_write_json  # noqa: E402

import json  # noqa: E402

TOOL_DATA = REPO / "config" / "tool_data.json"
PROCESSED = REPO / "config" / "processed_tool_registry.json"


def _iter_tools(blob):
    """Yield tool dicts from either {tools: {id: tool}} or {tools: [tool,...]}."""
    tools = blob.get("tools") if isinstance(blob, dict) else blob
    if isinstance(tools, dict):
        return list(tools.values())
    if isinstance(tools, list):
        return tools
    return []


def _fix_blob(blob) -> list:
    """Demote misclassified context params in place. Returns list of changes."""
    changes = []
    for tool in _iter_tools(blob):
        op = tool.get("operation_id") or "?"
        for name, p in (tool.get("parameters") or {}).items():
            if not isinstance(p, dict):
                continue
            if (p.get("dependency_source") or "user_input") != "context":
                continue
            if _context_value_appropriate(name, p.get("param_type")):
                continue
            # Demote: this context value would be a wrong-typed / value-field UUID.
            changes.append((op, name, p.get("param_type"), p.get("context_key")))
            p["dependency_source"] = "user_input"
            p["context_key"] = None
            if "preferred_operator" in p:
                p["preferred_operator"] = None
            if "is_filterable" in p:
                p["is_filterable"] = False
    return changes


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    total = 0
    for path in (TOOL_DATA, PROCESSED):
        if not path.exists():
            print(f"SKIP (missing): {path}")
            continue
        blob = json.loads(path.read_text(encoding="utf-8"))
        changes = _fix_blob(blob)
        total += len(changes)
        print(f"\n{path.name}: {len(changes)} params demoted context → user_input")
        for op, name, pt, ck in changes[:40]:
            print(f"   {op}.{name}  (type={pt}, was context_key={ck})")
        if changes and not args.dry_run:
            atomic_write_json(path, blob)
            print(f"   → written.")
        elif changes:
            print("   → dry-run, NOT written.")

    print(f"\nTOTAL params demoted: {total} ({'dry-run' if args.dry_run else 'applied'})")


if __name__ == "__main__":
    main()
