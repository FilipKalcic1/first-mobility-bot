"""End-to-end router accuracy benchmark (GAP 3, Filip 2026-05-20).

WHY THIS EXISTS — `scripts/bench_router_accuracy.py` measures only the
ANCHOR stage (is expected_tool in the cosine top-50?). It does NOT measure
the final LLM pick. So its "~45% p@1" is an anchor proxy, not the real
end-to-end accuracy. This script closes that gap: it wires the REAL
production router (CatalogScoper + AnchorIndex + LLMRouter — the same
classes the engine factory uses) and reports how often the router's
FINAL tool choice equals the ground-truth expected_tool.

Pipeline per query (identical to production request path):
    scope(persona, {method}, drop_internal=True)  → tool_filter
        ↓
    LLMRouter.route(query, tool_filter=...)        → picked tool_id
        ↓
    compare picked == expected_tool

Two failure modes are reported separately (diagnostic):
    scope_miss  — expected_tool not even in the scoped candidate set
                  (persona/method/subset excluded it — a routing-config issue)
    route_miss  — expected was a candidate but the LLM picked another tool
                  (an anchor/intent_summary disambiguation issue)

NOTE: this REQUIRES Azure (embeddings + chat). It CANNOT run from the
Claude sandbox (DNS blocked) — Filip runs it locally with a populated
.env. It was written by mirroring the engine factory wiring
(services/v2/engine.py make_v2_engine_for_production) but has NOT been
executed; verify on first run.

Usage:
    python scripts/bench_router_e2e.py
        [--tool-data config/tool_data.json]
        [--benchmark-file tests/benchmarks/objective_benchmark_100.json]
        [--limit N]              # debug: first N queries
        [--no-method-filter]     # harder: router sees all methods in persona scope
        [--tenant TENANT_ID]     # default None → _default tool_subset

Cost: ~$0.01 embeddings + ~N*1 gpt-4o-mini calls (N = #queries).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_BENCH_FILE = REPO / "tests" / "benchmarks" / "objective_benchmark_100.json"
DEFAULT_TOOL_DATA = REPO / "config" / "tool_data.json"
ANCHOR_CACHE = REPO / "tests" / "benchmarks" / "router_anchor_cache.json"

_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}


def method_of(tool_id: str, tools: dict) -> str:
    """HTTP method for a tool_id; fall back to op_id prefix."""
    entry = tools.get(tool_id) or {}
    m = (entry.get("method") or "").upper()
    if m:
        return m
    prefix = tool_id.split("_", 1)[0].upper() if "_" in tool_id else ""
    return prefix if prefix in _METHODS else "UNKNOWN"


def _print_segment(title: str, seg: dict) -> None:
    """seg[label] = [correct, total]."""
    if not seg:
        return
    print(f"\n  --- {title} ---")
    print(f"    {'label':<14} {'n':>4} {'p@1 (end-to-end)':>20}")
    for label, (c, n) in sorted(seg.items(), key=lambda kv: -kv[1][1]):
        if n:
            print(f"    {label:<14} {n:>4} {c:>3}/{n:<3} = {100 * c / n:>5.1f}%")


async def main_async(args) -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.path.insert(0, str(REPO))

    if not args.tool_data.exists():
        print(f"ERROR: {args.tool_data} not found", file=sys.stderr)
        sys.exit(1)
    if not args.benchmark_file.exists():
        print(f"ERROR: {args.benchmark_file} not found", file=sys.stderr)
        sys.exit(1)

    # --- ground truth ---
    bench = json.loads(args.benchmark_file.read_text(encoding="utf-8"))
    queries = [q for q in (bench.get("queries") or []) if q.get("expected_tool")]
    if args.limit:
        queries = queries[: args.limit]
    print(f"Loaded {len(queries)} labelled queries from {args.benchmark_file.name}.")

    # --- tool_data + derived shapes (mirror engine factory) ---
    tool_data = json.loads(args.tool_data.read_text(encoding="utf-8"))
    tool_entries = tool_data.get("tools", {})
    if not tool_entries:
        print("ERROR: tool_data has no 'tools'", file=sys.stderr)
        sys.exit(1)

    registry_dict = {
        "tools": list(tool_entries.values()),
        "dependency_graph": tool_data.get("dependency_graph") or [],
    }
    tkb_dict = {
        op: {
            "intent_summary": e.get("intent_summary", ""),
            "use_when": e.get("use_when") or [],
            "do_not_use_when": e.get("do_not_use_when") or [],
            "method": e.get("method", "GET"),
        }
        for op, e in tool_entries.items()
    }
    anchors_dict = {
        op: list(e.get("anchors") or [])
        for op, e in tool_entries.items()
        if e.get("anchors")
    }

    # --- wire the REAL router (same classes as production) ---
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

    async def _embed_fn(texts: list) -> list:
        r = await embed_client.embeddings.create(input=texts, model=embed_dep)
        return [d.embedding for d in r.data]

    anchor_index = AnchorIndex(
        anchors_data=anchors_dict,
        cache_path=ANCHOR_CACHE,
        embedding_deployment=embed_dep,
    )
    router = LLMRouter(
        anchor_index=anchor_index,
        schema_builder=ToolSchemaBuilder.from_registry(registry_dict),
        registry=registry_dict,
        tkb=tkb_dict,
        llm_client=chat_client,
        embed_fn=_embed_fn,
        deployment_name=chat_dep,
    )
    print("Building anchor index (embeds all anchors once)...")
    await router.initialize()
    scoper = CatalogScoper(tool_data=tool_data, tenants_dir=REPO / "config" / "tenants")

    # --- run each query through the full pipeline ---
    correct = scope_miss = route_miss = 0
    by_method: dict = {}
    by_persona: dict = {}
    route_misses: list = []
    scope_misses: list = []

    for i, q in enumerate(queries, 1):
        expected = q["expected_tool"]
        persona = q.get("persona") or "driver"
        method = method_of(expected, tool_entries)
        methods = (
            None if args.no_method_filter or method not in _METHODS
            else frozenset({method})
        )

        scope = scoper.scope(
            tenant_id=args.tenant, persona=persona,
            methods=methods, drop_internal=True,
        )

        by_method.setdefault(method, [0, 0])[1] += 1
        by_persona.setdefault(persona, [0, 0])[1] += 1

        if expected not in scope:
            scope_miss += 1
            scope_misses.append((q["query"], expected, persona, method, len(scope)))
            continue

        res = await router.route(
            query=q["query"], identity_summary="",
            conversation_history=[], tool_filter=scope,
        )
        picked = res.tool_id

        if picked == expected:
            correct += 1
            by_method[method][0] += 1
            by_persona[persona][0] += 1
        else:
            route_miss += 1
            route_misses.append((q["query"], expected, picked, res.error))

        if i % 10 == 0:
            print(f"  ... {i}/{len(queries)} done")

    # --- report ---
    n = len(queries)
    print(f"\n=== END-TO-END ROUTER ACCURACY ({args.benchmark_file.name}) ===")
    print(f"  tool_data:        {args.tool_data.name}")
    print(f"  method filter:    {'OFF (harder)' if args.no_method_filter else 'ON (simulates action pick)'}")
    print(f"  total queries:    {n}")
    if n:
        print(f"  CORRECT (p@1):    {correct}/{n} = {100 * correct / n:.1f}%   <-- real end-to-end")
        print(f"  scope_miss:       {scope_miss}/{n} = {100 * scope_miss / n:.1f}%   (expected tool excluded by persona/method/subset)")
        print(f"  route_miss:       {route_miss}/{n} = {100 * route_miss / n:.1f}%   (in scope, but LLM picked another)")

    _print_segment("By HTTP method", by_method)
    _print_segment("By persona", by_persona)

    if scope_misses:
        print("\n  --- scope_miss sample (expected tool not in candidate set) ---")
        for query, exp, per, meth, nscope in scope_misses[:10]:
            print(f"    '{query[:45]}' exp={exp} persona={per} method={meth} scope_size={nscope}")

    if route_misses:
        print("\n  --- route_miss sample (LLM picked wrong) ---")
        for query, exp, got, err in route_misses[:10]:
            print(f"    '{query[:45]}' exp={exp} got={got or '(none)'} err={err or '-'}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tool-data", type=Path, default=DEFAULT_TOOL_DATA)
    ap.add_argument("--benchmark-file", type=Path, default=DEFAULT_BENCH_FILE)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--no-method-filter", action="store_true")
    ap.add_argument("--tenant", type=str, default=None)
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()
