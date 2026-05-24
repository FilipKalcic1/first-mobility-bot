"""One-shot parameter EXTRACTION accuracy (Filip 2026-05-24).

Companion to bench_router_e2e.py (which measures WHICH tool). This measures, for
a tool-correct call, whether the bot pulls STATED values from a single message
into the RIGHT param, right value, right format. Two parts:

  Part A (LLM extraction, needs Azure) — kinds path_id/body_num: run the REAL
    router (scope → anchor cosine → LLM tool-call) and compare the extracted
    tool-call args (RouterResult.params) to expected_params. Only scored when
    the right tool was picked (else extraction is moot — reported as tool_miss).

  Part B (deterministic, no Azure) — kinds registration/registration_negative:
    run services.v2.entity_detector.detect_registration and check the plate
    (this is how the bot actually gets a Filter — NOT via the LLM tool-call).

Scope: ONE-SHOT extraction from the raw message. Param-asking (multi-turn fill
of missing required) and context injection (executor adds VehicleId/personId)
are deterministic follow-ups — NOT measured here, NOT counted as failures.

Usage:
    python scripts/bench_extraction.py
        [--benchmark-file tests/benchmarks/extraction_eval.json]
        [--tenant TENANT_ID]
Needs Azure for Part A (embeddings + chat). Part B always runs.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_BENCH = REPO / "tests" / "benchmarks" / "extraction_eval.json"
TOOL_DATA = REPO / "config" / "tool_data.json"
ANCHOR_CACHE = REPO / "tests" / "benchmarks" / "router_anchor_cache.json"
_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}


def _digits_int(v):
    """Normalize a numeric value to int by keeping digits only ('42.500'→42500,
    45→45, '45'→45). Returns None if no digits."""
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


def _run_part_b(queries) -> None:
    """Deterministic registration extraction via entity_detector."""
    from services.v2.entity_detector import detect_registration
    from services.v2.filter_builder import build_filter_clause

    pos = [q for q in queries if q.get("kind") == "registration"]
    neg = [q for q in queries if q.get("kind") == "registration_negative"]
    print("\n=== PART B — registration extraction (deterministic, entity_detector) ===")
    pos_ok = 0
    for q in pos:
        got = detect_registration(q["query"])
        exp = q["expected_plate"]
        ok = got == exp
        pos_ok += ok
        clause = build_filter_clause("LicencePlate", got) if got else ""
        mark = "OK " if ok else "BAD"
        print(f"  [{mark}] {q['query'][:42]:44} exp={exp!r} got={got!r}  filter={clause!r}")
    neg_ok = 0
    for q in neg:
        got = detect_registration(q["query"])
        ok = got is None
        neg_ok += ok
        mark = "OK " if ok else "FALSE-POSITIVE"
        print(f"  [{mark}] {q['query'][:42]:44} exp=None got={got!r}")
    if pos:
        print(f"  registration positives: {pos_ok}/{len(pos)} = {100*pos_ok/len(pos):.1f}%")
    if neg:
        print(f"  negatives (no false plate): {neg_ok}/{len(neg)} = {100*neg_ok/len(neg):.1f}%")


async def _run_part_a(queries, tenant) -> None:
    """LLM extraction via the real router for path_id/body_num kinds."""
    items = [q for q in queries if q.get("kind") in ("path_id", "body_num")]
    if not items:
        return
    print("\n=== PART A — LLM tool-call extraction (real router; needs Azure) ===")

    tool_data = json.loads(TOOL_DATA.read_text(encoding="utf-8"))
    tools = tool_data.get("tools", {})
    registry_dict = {"tools": list(tools.values()),
                     "dependency_graph": tool_data.get("dependency_graph") or []}
    tkb = {op: {"intent_summary": e.get("intent_summary", ""),
                "use_when": e.get("use_when") or [],
                "do_not_use_when": e.get("do_not_use_when") or [],
                "method": e.get("method", "GET")} for op, e in tools.items()}
    anchors = {op: list(e.get("anchors") or []) for op, e in tools.items() if e.get("anchors")}

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
    print("Building anchor index (real ada-002; ~1-2 min first run, then cached)...", flush=True)
    await router.initialize()
    scoper = CatalogScoper(tool_data=tool_data, tenants_dir=REPO / "config" / "tenants")

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
              f"→ {100*param_correct/total_params:.1f}% correct")
    print("\n  --- per query ---")
    for qid, query, verdict, info in rows:
        print(f"    [{verdict:10}] {qid} '{query[:38]}' {info[:80]}")


async def main_async(args) -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.path.insert(0, str(REPO))
    import logging
    logging.basicConfig(level=logging.WARNING)
    bench = json.loads(args.benchmark_file.read_text(encoding="utf-8"))
    queries = bench.get("queries") or []
    print(f"Loaded {len(queries)} extraction-eval queries from {args.benchmark_file.name}.")
    _run_part_b(queries)        # deterministic, always
    try:
        await _run_part_a(queries, args.tenant)   # needs Azure
    except Exception as e:  # noqa: BLE001
        print(f"\nPART A skipped/failed (Azure?): {type(e).__name__}: {e}", file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--benchmark-file", type=Path, default=DEFAULT_BENCH)
    ap.add_argument("--tenant", type=str, default=None)
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()
