"""One-shot parameter EXTRACTION accuracy (Filip 2026-05-24, +format 2026-05-25).

Companion to bench_router_e2e.py (which measures WHICH tool). This measures, for
a tool-correct call, whether the bot pulls STATED values from a single message
into the RIGHT param, right value, right FORMAT.

  PART A — LLM extraction (needs Azure), kinds path_id/body_num: run the REAL
    router (scope -> anchor cosine -> LLM tool-call) and compare extracted args
    (RouterResult.params) to expected_params. Only scored when the right tool
    was picked (else extraction is moot -> reported as tool_miss). Numeric
    compare is digit-normalized (45 == "45").

  PART B — DATE/RANGE FORMAT (needs Azure), kind datetime (Filip 2026-05-25):
    measures whether the LLM-extracted date/range value is in CORRECT ISO 8601.
    Scored FORMAT-AWARE: CORRECT / WRONG_FORMAT (right instant, wrong shape e.g.
    "17.05.2026") / WRONG_VALUE (different instant) / MISSING. The router is
    FORCED to expected_tool (singleton tool_filter) to ISOLATE extraction-format
    from routing accuracy — answers "given the right tool, does it format dates
    right?". Uses the REAL tool_schema_builder, so it reflects production (which
    does NOT pass `format` to the LLM schema, and llm_router does NOT inject
    today's date). Absolute vs relative dates reported separately:
      - absolute ("17.05.2026") -> tests pure ISO conversion quality
      - relative ("sutra"/"u petak") -> tests resolvability; the LLM has no date
        context on this path, so these expose a structural gap (param_ui coercion
        resolves them, but only on the param-ASK path, not LLM extraction).

Scope: ONE-SHOT extraction from the raw message. Param-asking (multi-turn fill
of missing required) and context injection (executor adds VehicleId/personId)
are deterministic follow-ups — NOT measured here, NOT counted as failures.

Usage:
    python scripts/bench_extraction.py
        [--benchmark-file tests/benchmarks/extraction_eval.json]
        [--tenant TENANT_ID]
Needs Azure (embeddings + chat).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime, time as dt_time, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_BENCH = REPO / "tests" / "benchmarks" / "extraction_eval.json"
TOOL_DATA = REPO / "config" / "tool_data.json"
ANCHOR_CACHE = REPO / "tests" / "benchmarks" / "router_anchor_cache.json"
_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}


def _digits_int(v):
    """Normalize a numeric value to int by keeping digits only ('42.500'->42500,
    45->45, '45'->45). Returns None if no digits."""
    s = re.sub(r"[^\d]", "", str(v))
    return int(s) if s else None


def _find_arg(args: dict, name: str):
    """Case-insensitive lookup of `name` in the extracted args dict."""
    if name in args:
        return args[name], True
    low = name.lower()
    for k, v in args.items():
        if k.lower() == low:
            return v, True
    return None, False


def _method_of(tool_id, tools):
    e = tools.get(tool_id) or {}
    m = (e.get("method") or "").upper()
    if m:
        return m
    p = tool_id.split("_", 1)[0].upper() if "_" in tool_id else ""
    return p if p in _METHODS else "UNKNOWN"


# --------------------------------------------------------------------------
# Date/range FORMAT scoring helpers (Part B)
# --------------------------------------------------------------------------
_WD = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
       "friday": 4, "saturday": 5, "sunday": 6}
_HR_DT_RE = re.compile(
    r"(\d{1,2})\.(\d{1,2})\.(\d{4})\.?(?:\s+(\d{1,2})(?::(\d{2}))?)?"
)


def _next_weekday(today, target_wd: int):
    ahead = (target_wd - today.weekday()) % 7
    if ahead == 0:
        ahead = 7
    return today + timedelta(days=ahead)


def _resolve_expected_iso(spec: dict, now: datetime) -> str:
    """spec is {'iso': '...'} (absolute) or {'rel': {base, time?}} (relative,
    resolved against `now`). Returns canonical ISO 'YYYY-MM-DDTHH:MM:SS'."""
    if "iso" in spec:
        return spec["iso"]
    rel = spec.get("rel") or {}
    base = rel.get("base", "today")
    today = now.date()
    if base == "today":
        d = today
    elif base == "tomorrow":
        d = today + timedelta(days=1)
    elif base == "day_after":
        d = today + timedelta(days=2)
    elif base.startswith("next_"):
        d = _next_weekday(today, _WD.get(base[5:], 0))
    else:
        d = today
    t = rel.get("time")
    if t:
        hh, mm = t.split(":")
        tt = dt_time(int(hh), int(mm))
    else:
        tt = dt_time(0, 0)
    return datetime.combine(d, tt).isoformat(timespec="seconds")


def _norm_iso(s):
    """Parse an ISO-8601-ish string to datetime, or None. Tolerates a space
    date/time separator and a trailing Z."""
    s = str(s).strip()
    if not s:
        return None
    s = s.rstrip("Zz").strip()
    s = re.sub(r"(\d{4}-\d{2}-\d{2})[ ]+", r"\1T", s)
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _parse_hr_dt(s):
    """Parse a Croatian 'DD.MM.YYYY [HH:MM]' value to datetime, or None.
    Used to detect WRONG_FORMAT (right instant in a non-ISO shape)."""
    m = _HR_DT_RE.search(str(s))
    if not m:
        return None
    d, mo, y, hh, mm = m.groups()
    try:
        return datetime(int(y), int(mo), int(d),
                        int(hh) if hh else 0, int(mm) if mm else 0)
    except ValueError:
        return None


def _classify_dt(expected_iso: str, got) -> str:
    """CORRECT / WRONG_FORMAT / WRONG_VALUE / MISSING.

    If the query specified no time (expected at midnight) only the date must
    match; a spurious time is not penalized. WRONG_FORMAT = same instant but
    not ISO (e.g. '17.05.2026' or left as 'sutra' -> unparseable shape)."""
    if got is None or str(got).strip() == "":
        return "MISSING"
    exp = datetime.fromisoformat(expected_iso)
    has_time = (exp.hour, exp.minute, exp.second) != (0, 0, 0)

    def _match(dt) -> bool:
        return dt == exp if has_time else dt.date() == exp.date()

    iso_dt = _norm_iso(got)
    if iso_dt is not None:
        return "CORRECT" if _match(iso_dt) else "WRONG_VALUE"
    hr_dt = _parse_hr_dt(got)
    if hr_dt is not None:
        return "WRONG_FORMAT" if _match(hr_dt) else "WRONG_VALUE"
    return "WRONG_FORMAT"  # unparseable shape (e.g. the LLM left "sutra")


# --------------------------------------------------------------------------
# Pipeline build (shared by Part A + Part B; anchor index built ONCE)
# --------------------------------------------------------------------------
async def _build_pipeline():
    tool_data = json.loads(TOOL_DATA.read_text(encoding="utf-8"))
    tools = tool_data.get("tools", {})
    registry_dict = {"tools": list(tools.values()),
                     "dependency_graph": tool_data.get("dependency_graph") or []}
    tkb = {op: {"intent_summary": e.get("intent_summary", ""),
                "use_when": e.get("use_when") or [],
                "do_not_use_when": e.get("do_not_use_when") or [],
                "method": e.get("method", "GET")} for op, e in tools.items()}
    anchors = {op: list(e.get("anchors") or [])
               for op, e in tools.items() if e.get("anchors")}

    from config import get_settings
    from services.openai_client import get_openai_client, get_embedding_client
    from services.router.anchor_index import AnchorIndex
    from services.router.tool_schema_builder import ToolSchemaBuilder
    from services.router.llm_router import LLMRouter
    from services.router.catalog_scoper import CatalogScoper

    settings = get_settings()
    embed_client = get_embedding_client()
    chat_client = get_openai_client()
    embed_dep = settings.AZURE_OPENAI_EMBEDDING_DEPLOYMENT
    chat_dep = settings.AZURE_OPENAI_DEPLOYMENT_NAME

    async def _embed_fn(texts):
        r = await embed_client.embeddings.create(input=texts, model=embed_dep)
        return [d.embedding for d in r.data]

    anchor_index = AnchorIndex(anchors_data=anchors, cache_path=ANCHOR_CACHE,
                               embedding_deployment=embed_dep)
    router = LLMRouter(anchor_index=anchor_index,
                       schema_builder=ToolSchemaBuilder.from_registry(registry_dict),
                       registry=registry_dict, tkb=tkb, llm_client=chat_client,
                       embed_fn=_embed_fn, deployment_name=chat_dep)
    print("Building anchor index (real ada-002; ~1-2 min first run, then cached)...",
          flush=True)
    await router.initialize()
    scoper = CatalogScoper(tool_data=tool_data,
                           tenants_dir=REPO / "config" / "tenants")
    return router, scoper, tools


async def _run_part_a(router, scoper, tools, queries, tenant) -> None:
    """LLM extraction via the real router for path_id/body_num kinds."""
    items = [q for q in queries if q.get("kind") in ("path_id", "body_num")]
    if not items:
        return
    print("\n=== PART A — LLM tool-call extraction (path_id/body_num) ===")

    tool_ok = 0
    param_correct = param_wrong = param_missing = 0
    complete = 0
    rows = []
    for q in items:
        exp_tool = q["expected_tool"]
        exp_params = q["expected_params"]
        method = _method_of(exp_tool, tools)
        scope = scoper.scope(tenant_id=tenant, persona=None,
                             methods=frozenset({method}) if method in _METHODS else None,
                             drop_internal=True)
        res = await router.route(query=q["query"], identity_summary="",
                                 conversation_history=[], tool_filter=scope)
        picked = res.tool_id
        args = res.params or {}
        if picked != exp_tool:
            rows.append((q["id"], q["query"], "TOOL_MISS", f"picked={picked or res.error}"))
            continue
        tool_ok += 1
        q_correct = q_wrong = q_missing = 0
        details = []
        for pname, pval in exp_params.items():
            got, present = _find_arg(args, pname)
            if not present:
                q_missing += 1; details.append(f"{pname}:MISSING")
            elif _digits_int(got) == _digits_int(pval):
                q_correct += 1
            else:
                q_wrong += 1; details.append(f"{pname}:WRONG(got={got!r})")
        param_correct += q_correct; param_wrong += q_wrong; param_missing += q_missing
        if q_wrong == 0 and q_missing == 0:
            complete += 1
            rows.append((q["id"], q["query"], "OK", f"args={args}"))
        else:
            rows.append((q["id"], q["query"], "PARAM_FAIL", "; ".join(details) + f" | args={args}"))

    n = len(items)
    total_params = param_correct + param_wrong + param_missing
    print(f"\n  total path_id/body_num queries: {n}")
    print(f"  tool picked correctly:          {tool_ok}/{n} = {100*tool_ok/n:.1f}%  (extraction only measurable here)")
    print(f"  COMPLETE calls (tool+all params): {complete}/{n} = {100*complete/n:.1f}%")
    if total_params:
        print(f"  param-level (among tool-correct): correct={param_correct} wrong={param_wrong} missing={param_missing} "
              f"-> {100*param_correct/total_params:.1f}% correct")
    print("\n  --- per query ---")
    for qid, query, verdict, info in rows:
        print(f"    [{verdict:10}] {qid} '{query[:38]}' {info[:80]}")


async def _run_part_b_datetime(router, tools, queries) -> None:
    """Date/range FORMAT accuracy. Router forced to expected_tool to isolate
    extraction-format from routing. Absolute vs relative reported separately."""
    items = [q for q in queries if q.get("kind") == "datetime"]
    if not items:
        return
    print("\n=== PART B — DATE/RANGE FORMAT (tool forced; isolates format from routing) ===")
    now = datetime.now()
    print(f"  run-time 'now' for relative dates: {now.isoformat(timespec='seconds')}")

    # group counters: [tool_ok, correct, wrong_format, wrong_value, missing, complete, n]
    groups = {"ABS": dict(tool_ok=0, CORRECT=0, WRONG_FORMAT=0, WRONG_VALUE=0,
                          MISSING=0, complete=0, n=0),
              "REL": dict(tool_ok=0, CORRECT=0, WRONG_FORMAT=0, WRONG_VALUE=0,
                          MISSING=0, complete=0, n=0)}
    rows = []
    for q in items:
        exp_tool = q["expected_tool"]
        expected = q["expected"]
        is_rel = any("rel" in spec for spec in expected.values())
        grp = groups["REL" if is_rel else "ABS"]
        grp["n"] += 1
        forced = frozenset({exp_tool})
        res = await router.route(query=q["query"], identity_summary="",
                                 conversation_history=[], tool_filter=forced)
        picked = res.tool_id
        args = res.params or {}
        if picked != exp_tool:
            rows.append((q["id"], q["query"], "TOOL_MISS", f"picked={picked or res.error}"))
            continue
        grp["tool_ok"] += 1
        verdicts = []
        all_correct = True
        for pname, spec in expected.items():
            exp_iso = _resolve_expected_iso(spec, now)
            got, present = _find_arg(args, pname)
            v = _classify_dt(exp_iso, got if present else None)
            grp[v] += 1
            if v != "CORRECT":
                all_correct = False
            verdicts.append(f"{pname}={v}(exp={exp_iso} got={got!r})")
        if all_correct:
            grp["complete"] += 1
            rows.append((q["id"], q["query"], "OK", "; ".join(verdicts)))
        else:
            rows.append((q["id"], q["query"], "FORMAT_FAIL", "; ".join(verdicts)))

    for label, g in (("ABSOLUTE dates", groups["ABS"]),
                     ("RELATIVE dates (sutra/u petak — no date context on LLM path)", groups["REL"])):
        n = g["n"]
        if not n:
            continue
        params_total = g["CORRECT"] + g["WRONG_FORMAT"] + g["WRONG_VALUE"] + g["MISSING"]
        print(f"\n  --- {label} ---")
        print(f"    queries: {n}   tool routed/forced ok: {g['tool_ok']}/{n}")
        print(f"    COMPLETE (all params correct ISO): {g['complete']}/{n}"
              + (f" = {100*g['complete']/n:.1f}%" if n else ""))
        if params_total:
            print(f"    param-level: CORRECT={g['CORRECT']} WRONG_FORMAT={g['WRONG_FORMAT']} "
                  f"WRONG_VALUE={g['WRONG_VALUE']} MISSING={g['MISSING']} "
                  f"-> {100*g['CORRECT']/params_total:.1f}% correctly formatted")
    print("\n  --- per query ---")
    for qid, query, verdict, info in rows:
        print(f"    [{verdict:11}] {qid} '{query[:40]}'")
        print(f"                  {info[:160]}")


async def main_async(args) -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.path.insert(0, str(REPO))
    import logging
    logging.basicConfig(level=logging.WARNING)
    bench = json.loads(args.benchmark_file.read_text(encoding="utf-8"))
    queries = bench.get("queries") or []
    print(f"Loaded {len(queries)} extraction-eval queries from {args.benchmark_file.name}.")
    try:
        router, scoper, tools = await _build_pipeline()   # needs Azure
        await _run_part_a(router, scoper, tools, queries, args.tenant)
        await _run_part_b_datetime(router, tools, queries)
    except Exception as e:  # noqa: BLE001
        print(f"\nBENCH skipped/failed (Azure?): {type(e).__name__}: {e}", file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--benchmark-file", type=Path, default=DEFAULT_BENCH)
    ap.add_argument("--tenant", type=str, default=None)
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()
