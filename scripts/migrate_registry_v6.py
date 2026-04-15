"""
Migrate processed_tool_registry.json in place — V6 alias schema.

Changes:
  * get_MasterData: personId.required=True, output_key_aliases={"Id": "VehicleId"}
  * get_Persons:    Filter.dependency_source="context",
                    Filter.filter_template="Phone(=){phone}",
                    output_key_aliases={"Id": "PersonId"}
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "config" / "processed_tool_registry.json"
DST = SRC  # in-place migration


def find_tool(tools: list, op_id: str) -> dict:
    for t in tools:
        if t.get("operation_id") == op_id:
            return t
    raise SystemExit(f"FATAL: operation_id '{op_id}' not found in {SRC}")


def main() -> int:
    if not SRC.exists():
        raise SystemExit(f"FATAL: source registry not found at {SRC}")

    with SRC.open("r", encoding="utf-8") as f:
        registry = json.load(f)

    tools = registry.get("tools") if isinstance(registry, dict) else registry
    if not isinstance(tools, list):
        raise SystemExit("FATAL: registry shape unrecognised (expected list or {'tools': [...]})")

    # --- get_MasterData ---
    md = find_tool(tools, "get_MasterData")
    md_params = md.setdefault("parameters", {})
    if "personId" not in md_params:
        raise SystemExit("FATAL: get_MasterData has no 'personId' parameter")
    md_params["personId"]["required"] = True
    md["output_key_aliases"] = {"Id": "VehicleId"}

    # --- get_Persons ---
    persons = find_tool(tools, "get_Persons")
    p_params = persons.setdefault("parameters", {})
    if "Filter" not in p_params:
        raise SystemExit("FATAL: get_Persons has no 'Filter' parameter")
    p_params["Filter"]["dependency_source"] = "context"
    p_params["Filter"]["filter_template"] = "Phone(=){phone}"
    persons["output_key_aliases"] = {"Id": "PersonId"}

    DST.parent.mkdir(parents=True, exist_ok=True)
    with DST.open("w", encoding="utf-8") as f:
        json.dump(registry, f, indent=2, ensure_ascii=False)

    print(f"OK  wrote {DST}")
    print(f"    get_MasterData: personId.required=true, aliases={md['output_key_aliases']}")
    print(f"    get_Persons   : Filter.dependency_source=context, template={p_params['Filter']['filter_template']}, aliases={persons['output_key_aliases']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
