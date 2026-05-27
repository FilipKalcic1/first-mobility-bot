"""Export a CLEAN tool registry for the MobilityOne backend team (Filip 2026-05-26).

The two M1 request docs (docs/M1_ZAHTJEV_params / _filter) reference an attachment
`tool_registry.json` — "popis svih endpointa i njihovih parametara". Our internal
config/tool_data.json carries a lot of bot-internal noise (anchors, intent_summary,
use_when, personas, context_key, filter operators) that would only confuse the M1
team. This strips it to the endpoint + parameter surface they actually need:

    {operation_id, method, service, path,
     parameters: [{name, type, format, location, required, description, enum}]}

Pure read of config/tool_data.json → writes tool_registry.json at repo root.
Usage: python scripts/export_m1_registry.py [--out tool_registry.json]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "config" / "tool_data.json"


def _clean_param(pdef: dict) -> dict:
    """Keep only the OpenAPI-relevant surface of one parameter."""
    out = {
        "name": pdef.get("name"),
        "type": pdef.get("param_type") or "string",
        "location": pdef.get("location"),
        "required": bool(pdef.get("required")),
    }
    if pdef.get("format"):
        out["format"] = pdef["format"]
    desc = (pdef.get("description") or "").strip()
    if desc:
        out["description"] = desc
    if pdef.get("enum_values"):
        out["enum"] = pdef["enum_values"]
    if (pdef.get("param_type") or "").lower() == "array" and pdef.get("items_type"):
        out["items_type"] = pdef["items_type"]
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=REPO / "tool_registry.json")
    args = ap.parse_args()

    data = json.loads(SRC.read_text(encoding="utf-8"))
    tools_in = data.get("tools") or {}

    tools_out = []
    for op_id, e in tools_in.items():
        params = [
            _clean_param(pd)
            for pd in (e.get("parameters") or {}).values()
            if isinstance(pd, dict)
        ]
        tools_out.append({
            "operation_id": e.get("operation_id") or op_id,
            "method": e.get("method"),
            "service": e.get("service"),
            "path": e.get("path"),
            "parameters": params,
        })
    tools_out.sort(key=lambda t: (t.get("service") or "", t.get("path") or "", t.get("method") or ""))

    payload = {
        "_meta": {
            "purpose": "MobilityOne endpoint + parameter surface for the backend team. "
                       "Reference for docs/M1_ZAHTJEV_params + _filter. Bot-internal fields "
                       "(anchors, intent_summary, personas) intentionally excluded.",
            "tool_count": len(tools_out),
            "source": "config/tool_data.json",
        },
        "tools": tools_out,
    }
    args.out.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    print(f"Wrote {args.out} — {len(tools_out)} tools, "
          f"{sum(len(t['parameters']) for t in tools_out)} params.")


if __name__ == "__main__":
    main()
