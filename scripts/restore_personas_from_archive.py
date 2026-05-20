"""Phase A1 — Restore `personas` field from archived TKB into tool_data.json.

The Phase 1 union-merge (2026-05-15) dropped 5 TKB fields as "dead":
examples, returns, personas, mutation_safe, requires_confirm. That was a
mistake for `personas` — the field is needed to gate persona-restricted
routing (Phase A-F of the 2026-05-16 plan).

This is a PURE DATA MOVE — no LLM, no quality changes. It only copies
the existing (loose) persona tags from archive back into tool_data.json
so we have a baseline before Phase A2 retags them with a stricter prompt.

Adds two fields per tool entry:
  personas         : list[str]  (original, loose tagging from archive)
  personas_strict  : null       (placeholder — populated by A2)

Run: python -X utf8 scripts/restore_personas_from_archive.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

REPO = Path(__file__).resolve().parent.parent
ARCHIVE = REPO / "config" / "tool_knowledge_base.archive.json"
OUT = REPO / "config" / "tool_data.json"


def main() -> None:
    print(f"Loading {ARCHIVE.name} ...")
    archive = json.loads(ARCHIVE.read_text(encoding="utf-8"))
    archive_tools = archive.get("tools", {})
    print(f"  {len(archive_tools)} archive entries")

    print(f"Loading {OUT.name} ...")
    doc = json.loads(OUT.read_text(encoding="utf-8"))
    tools = doc.get("tools", {})
    print(f"  {len(tools)} tools")

    restored = 0
    missing_from_archive = []
    persona_counter = {}

    for op_id, entry in tools.items():
        arc = archive_tools.get(op_id, {})
        personas = arc.get("personas") or []
        entry["personas"] = list(personas)
        entry["personas_strict"] = None  # placeholder for Phase A2
        if personas:
            restored += 1
            for p in personas:
                persona_counter[p] = persona_counter.get(p, 0) + 1
        else:
            missing_from_archive.append(op_id)

    doc.setdefault("_meta", {})["personas_restored_at"] = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(),
    )
    doc["_meta"]["personas_restored_count"] = restored

    OUT.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print(f"Wrote {OUT.relative_to(REPO)}")
    print(f"  tools with personas restored: {restored}/{len(tools)}")
    print(f"  tools missing from archive:   {len(missing_from_archive)}")
    print()
    print("Persona distribution (multi-tag possible):")
    for p, n in sorted(persona_counter.items(), key=lambda x: -x[1]):
        print(f"  {p:<15} {n} tools")
    if missing_from_archive:
        print()
        print(f"First 5 missing from archive (got [] personas):")
        for op in missing_from_archive[:5]:
            print(f"  {op}")


if __name__ == "__main__":
    main()
