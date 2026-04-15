"""
V6.1 migration: normalize context_key semantics.

Finding (see lab scan): 140 parameters with dependency_source="context" carry
a context_key that semantically contradicts the parameter's own name, e.g.
    post_AddMileage.VehicleId  ->  context_key="person_id"
    post_Companies.TenantId    ->  context_key="person_id"
    post_OrgUnits.CompanyId    ->  context_key="person_id"

The generator appears to have defaulted every context param to "person_id".
This migration rewrites context_key so it matches the parameter's semantic
entity (vehicle_id, tenant_id, company_id, orgunit_id, person_id).

Runs in place on config/processed_tool_registry.json (after migrate_registry_v6.py).

Usage:
    python scripts/migrate_registry_v6.py
    python scripts/migrate_registry_v6_1.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "config" / "processed_tool_registry.json"

# Ordered rules: first match wins. More specific regexes go first.
ENTITY_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"tenant", re.I),                               "tenant_id"),
    (re.compile(r"(parent)?orgunit|department", re.I),          "orgunit_id"),
    (re.compile(r"company", re.I),                              "company_id"),
    (re.compile(r"(vehicle|equipment)[a-z]*id", re.I),          "vehicle_id"),
    (re.compile(r"(person|driver|user)[a-z]*id", re.I),         "person_id"),
]

# Parameters that contain an entity noun but are plain business IDs
# (Case, Pool, Schedule, etc.) - do not touch those; only rewrite when
# one of the rules above fires.


def classify(pname: str) -> str | None:
    for rx, canonical in ENTITY_RULES:
        if rx.search(pname):
            return canonical
    return None


def main() -> int:
    if not TARGET.exists():
        raise SystemExit(
            f"FATAL: {TARGET} missing. Run migrate_registry_v6.py first."
        )

    raw = json.loads(TARGET.read_text(encoding="utf-8"))
    tools = raw["tools"] if isinstance(raw, dict) and "tools" in raw else raw

    fixed: dict[str, int] = {}
    touched_examples: list[str] = []

    for tool in tools:
        params = tool.get("parameters") or {}
        for pname, pdef in params.items():
            if not isinstance(pdef, dict):
                continue
            if pdef.get("dependency_source") != "context":
                continue

            canonical = classify(pname)
            if canonical is None:
                continue
            current = pdef.get("context_key")
            if current == canonical:
                continue

            pdef["context_key"] = canonical
            cls_key = f"{current!s} -> {canonical}"
            fixed[cls_key] = fixed.get(cls_key, 0) + 1
            if len(touched_examples) < 10:
                touched_examples.append(
                    f"{tool.get('operation_id')}.{pname}: {current!s} -> {canonical}"
                )

    TARGET.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")

    total = sum(fixed.values())
    print(f"OK  rewrote {TARGET}")
    print(f"    total context_key fixes: {total}")
    for cls_key, count in sorted(fixed.items(), key=lambda kv: -kv[1]):
        print(f"      {count:4d}  {cls_key}")
    print("    sample:")
    for line in touched_examples:
        print(f"      {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
