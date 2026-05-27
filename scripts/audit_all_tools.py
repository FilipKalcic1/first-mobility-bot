"""Exhaustive STATIC per-tool audit over ALL ~950 tools (Filip 2026-05-27).

Drives config/processed_tool_registry.json (the runtime registry the executor
uses) and, for EVERY tool, checks the invariants behind the 4 things we kept
reviewing — proving the MACHINERY handles each tool's spec:

  LOCATION   every param.location ∈ {path,query,body,header}; every {placeholder}
             has a path-param and vice-versa (→ executor routes + substitutes).
  SCHEMA     ToolSchemaBuilder builds the LLM schema without error; required=[]
             (anti-fabrication); Filter/UseANDFor hidden; types map to JSON.
  COERCION   every param_type is known; param_ui.parse_param_value handles a
             type-appropriate sample (int/number/date/bool) without error and
             returns a sane (non-None) value; _coerce_llm_params runs clean.
  PARAM-ASK  _compute_missing_required / _compute_optional / _user_friendly_
             optionals run without error; required array/object is detected
             (refuse path); render_param_question works for required scalars.

HONEST SCOPE: this is STATIC — it proves the bot can build/route/coerce/ask for
every tool per its registry spec. It does NOT call the live API, so it does NOT
prove the API accepts the call (that needs real values + M1 token + routing →
the live smoke test). "Machinery works for all 950" ✓; "all 950 pass live" ✗.

Usage: python scripts/audit_all_tools.py
Pure read of the registry. No network, no Azure.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
REG = REPO / "config" / "processed_tool_registry.json"
sys.path.insert(0, str(REPO))

_VALID_LOC = {"path", "query", "body", "header"}
_KNOWN_TYPES = {"string", "integer", "number", "float", "boolean", "array", "object"}
_PLACEHOLDER_RE = re.compile(r"{([^}]+)}")


def _sample(pd: dict):
    """A type-appropriate sample value for coercion testing, or None to skip."""
    pt = (pd.get("param_type") or "string").lower()
    fmt = (pd.get("format") or "").lower()
    if fmt in ("date", "date-time"):
        return "17.05.2026 09:00"
    return {"integer": "42", "number": "12,5", "boolean": "da", "string": "test"}.get(pt)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    import logging
    logging.basicConfig(level=logging.ERROR)

    from services.router.tool_schema_builder import ToolSchemaBuilder
    from services.v2 import param_ui
    from services.v2.engine import V2Engine

    reg = json.loads(REG.read_text(encoding="utf-8"))
    tools = reg.get("tools") or []
    n = len(tools)

    # tool_parameters dict the engine helpers expect: {op_id: {pname: pdef}}
    tool_params = {t["operation_id"]: (t.get("parameters") or {}) for t in tools}
    eng = object.__new__(V2Engine)
    eng.tool_parameters = tool_params

    builder = ToolSchemaBuilder.from_registry(
        {"tools": tools, "dependency_graph": reg.get("dependency_graph") or []}
    )

    fails = {k: [] for k in ("LOCATION", "SCHEMA", "COERCION", "PARAM-ASK")}

    for t in tools:
        op = t.get("operation_id")
        path = t.get("path") or ""
        params = t.get("parameters") or {}

        # ---- LOCATION ----
        for pn, pd in params.items():
            if not isinstance(pd, dict):
                continue
            if pd.get("location") not in _VALID_LOC:
                fails["LOCATION"].append(f"{op}.{pn}: location={pd.get('location')!r}")
        phs = set(_PLACEHOLDER_RE.findall(path))
        pathp = {pn for pn, pd in params.items()
                 if isinstance(pd, dict) and pd.get("location") == "path"}
        for ph in phs - pathp:
            fails["LOCATION"].append(f"{op}: placeholder {{{ph}}} has no path-param")
        for pp in pathp - phs:
            fails["LOCATION"].append(f"{op}.{pp}: path-param has no placeholder")

        # ---- SCHEMA ----
        try:
            sch = builder.build_for_tools([t], tkb={})
            if sch:
                fn = sch[0]["function"]["parameters"]
                if fn.get("required") != []:
                    fails["SCHEMA"].append(f"{op}: required != [] ({fn.get('required')})")
                props = fn.get("properties", {})
                leaked = [p for p in ("Filter", "UseANDFor") if p in props]
                if leaked:
                    fails["SCHEMA"].append(f"{op}: exposes {leaked}")
        except Exception as e:  # noqa: BLE001
            fails["SCHEMA"].append(f"{op}: {type(e).__name__}: {e}")

        # ---- COERCION / FORMAT ----
        try:
            for pn, pd in params.items():
                if not isinstance(pd, dict):
                    continue
                pt = (pd.get("param_type") or "string").lower()
                if pt not in _KNOWN_TYPES:
                    fails["COERCION"].append(f"{op}.{pn}: unknown param_type {pt!r}")
            sample = {}
            for pn, pd in params.items():
                if not isinstance(pd, dict):
                    continue
                if (pd.get("dependency_source") or "user_input") != "user_input":
                    continue
                sv = _sample(pd)
                if sv is None:
                    continue
                parsed = param_ui.parse_param_value(sv, pd)
                if parsed is None:
                    fails["COERCION"].append(
                        f"{op}.{pn}: clean sample {sv!r} → None (type={pd.get('param_type')})"
                    )
                sample[pn] = sv
            eng._coerce_llm_params(op, dict(sample))  # must not raise
        except Exception as e:  # noqa: BLE001
            fails["COERCION"].append(f"{op}: {type(e).__name__}: {e}")

        # ---- PARAM-ASK ----
        try:
            miss = eng._compute_missing_required(op, {})
            eng._compute_optional(op, {})
            eng._user_friendly_optionals(op, {})
            for pn in miss:
                pd = params.get(pn) or {}
                if (pd.get("param_type") or "string").lower() not in ("array", "object"):
                    param_ui.render_param_question(pn, pd)
        except Exception as e:  # noqa: BLE001
            fails["PARAM-ASK"].append(f"{op}: {type(e).__name__}: {e}")

    # ---- report ----
    print(f"\n=== AUDIT ALL TOOLS — static per-tool invariants ({n} tools) ===\n")
    for area in ("LOCATION", "SCHEMA", "COERCION", "PARAM-ASK"):
        bad = fails[area]
        ok = n - len({f.split(':')[0].split('.')[0] for f in bad})  # tools w/o any fail
        status = "✓ ALL OK" if not bad else f"✗ {len(bad)} issue(s)"
        print(f"  {area:10}: {status}")
        for line in bad[:15]:
            print(f"      - {line}")
        if len(bad) > 15:
            print(f"      … +{len(bad) - 15} more")
    total_issues = sum(len(v) for v in fails.values())
    print(f"\n  TOTAL issues: {total_issues}")
    print("  NOTE: static coverage only — proves machinery handles every tool's "
          "spec; does NOT prove live API acceptance (needs M1 token + smoke).")


if __name__ == "__main__":
    main()
