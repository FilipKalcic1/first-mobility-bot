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
from collections import Counter
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

    # Show INFO logs (anchor_index.build emits progress here) + flush so we
    # can see exactly where a stall happens.
    import logging
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

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
    print(f"Loaded {len(queries)} labelled queries from {args.benchmark_file.name}.", flush=True)

    # --- tool_data + derived shapes (mirror engine factory) ---
    print(f"Loading {args.tool_data.name} (~3.8MB)...", flush=True)
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
    n_anchor_phrases = sum(len(v) for v in anchors_dict.values())
    print(f"tool_data: {len(tool_entries)} tools, {len(anchors_dict)} with anchors, "
          f"{n_anchor_phrases} anchor phrases total.", flush=True)

    # --- wire the REAL router (same classes as production) ---
    print("Importing router modules + reading .env settings...", flush=True)
    from config import get_settings
    from services.openai_client import get_openai_client, get_embedding_client
    from services.router.anchor_index import AnchorIndex
    from services.router.tool_schema_builder import ToolSchemaBuilder
    from services.router.llm_router import LLMRouter
    from services.router.catalog_scoper import CatalogScoper

    settings = get_settings()
    embed_client = get_embedding_client()
    print("  embedding client created; creating chat client...", flush=True)
    chat_client = get_openai_client()
    print("  chat client created.", flush=True)
    embed_dep = settings.AZURE_OPENAI_EMBEDDING_DEPLOYMENT
    chat_dep = settings.AZURE_OPENAI_DEPLOYMENT_NAME
    print(f"Azure clients ready (embed={embed_dep}, chat={chat_dep}, "
          f"endpoint={settings.AZURE_OPENAI_ENDPOINT}). Building schemas...", flush=True)

    _embed_batch_n = {"n": 0}

    async def _embed_fn(texts: list) -> list:
        _embed_batch_n["n"] += 1
        print(f"  embedding batch {_embed_batch_n['n']} ({len(texts)} phrases) "
              f"via Azure...", flush=True)
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
    print(f"Building anchor index — embedding {n_anchor_phrases} anchor phrases via Azure "
          f"(first run ~1-2 min; then cached to {ANCHOR_CACHE.name}). "
          f"If it hangs HERE with no further log line, Azure embeddings are "
          f"unreachable/throttled — check network + AZURE_OPENAI_* in .env.", flush=True)
    await router.initialize()
    print("Anchor index ready. Routing queries...", flush=True)
    scoper = CatalogScoper(tool_data=tool_data, tenants_dir=REPO / "config" / "tenants")

    # L2b driver-basics shortcut. Production runs this BEFORE the L3 router for
    # driver self-questions ("kolika km", "tablica", "marka", "potrošnja"). The
    # router-only number under-counts driver accuracy because those queries
    # never reach L3 in production — they're served by this anchor → get_MasterData.
    from services.v2.driver_basics import DriverBasicsAnchor

    class _BasicsEmbedder:
        async def embed(self, text):
            r = await embed_client.embeddings.create(input=[text], model=embed_dep)
            return r.data[0].embedding

    basics = DriverBasicsAnchor(_BasicsEmbedder())
    await basics.initialize()
    print("Driver-basics anchor ready. Running queries (L2b shortcut + L3)...", flush=True)

    # --- run each query through L2b shortcut → L3 router (mirrors production) ---
    correct = recall3 = scope_miss = route_miss = shortcut_hits = 0
    by_method: dict = {}
    by_persona: dict = {}
    route_misses: list = []
    scope_misses: list = []

    for i, q in enumerate(queries, 1):
        expected = q["expected_tool"]
        bench_persona = q.get("persona") or "driver"
        method = method_of(expected, tool_entries)
        by_method.setdefault(method, [0, 0])[1] += 1
        by_persona.setdefault(bench_persona, [0, 0])[1] += 1

        # L2b shortcut: driver self-question → get_MasterData, bypassing L3.
        # --no-l2b skips it entirely to measure the PURE L3 router capability
        # (the production intent-classifier gate is not modelled here, so the
        # shortcut over-fires on non-driver queries that merely contain
        # "km"/"vozilo" — --no-l2b removes that confound). Measurement-only.
        if not args.no_l2b:
            bm = await basics.match(q["query"])
            if bm.matched:
                shortcut_hits += 1
                if expected == "get_MasterData":
                    correct += 1
                    recall3 += 1
                    by_method[method][0] += 1
                    by_persona[bench_persona][0] += 1
                else:
                    route_miss += 1
                    route_misses.append((q["query"], expected, "get_MasterData(L2b)",
                                         "wrong_shortcut", [], None))
                continue

        # LAUNCH config: role filter OFF (persona=None). --role-on for FAZA 14.
        methods = (
            None if args.no_method_filter or method not in _METHODS
            else frozenset({method})
        )
        scope_persona = bench_persona if args.role_on else None
        scope = scoper.scope(
            tenant_id=args.tenant, persona=scope_persona,
            methods=methods, drop_internal=True,
        )

        if expected not in scope:
            scope_miss += 1
            scope_misses.append((q["query"], expected, bench_persona, method, len(scope)))
            continue

        res = await router.route(
            query=q["query"], identity_summary="",
            conversation_history=[], tool_filter=scope,
        )
        picked = res.tool_id
        all_cands = [tid for tid, _ in (res.top_candidates or [])]
        # Mirror engine card composition: LLM pick leads, anchor fills the rest
        # (deduped). recall@3 now measures what the user actually sees in the
        # 3-card picker after the LLM-pick-leads change.
        shown = ([picked] if picked in set(all_cands) else []) + [
            t for t in all_cands if t != picked
        ]
        top3 = shown[:3]

        if picked == expected:
            correct += 1
            recall3 += 1
            by_method[method][0] += 1
            by_persona[bench_persona][0] += 1
        else:
            if expected in top3:
                recall3 += 1  # cascade shows top-3; user could still pick it
            route_miss += 1
            route_misses.append((q["query"], expected, picked, res.error,
                                 top3, expected in set(all_cands)))

        if i % 10 == 0:
            print(f"  ... {i}/{len(queries)} done", flush=True)

    # --- report ---
    n = len(queries)
    print(f"\n=== END-TO-END ROUTER ACCURACY ({args.benchmark_file.name}) ===")
    print(f"  tool_data:        {args.tool_data.name}")
    print(f"  role filter:      {'ON (FAZA 14 hierarchy)' if args.role_on else 'OFF (persona=None — matches launch)'}")
    print(f"  method filter:    {'OFF (harder)' if args.no_method_filter else 'ON (simulates action pick)'}")
    print(f"  L2b shortcut:     {'OFF (pure L3)' if args.no_l2b else 'ON (driver self-Q → get_MasterData)'}")
    print(f"  total queries:    {n}")
    if n:
        print(f"  CORRECT (p@1):    {correct}/{n} = {100 * correct / n:.1f}%   <-- end-to-end (L2b shortcut + L3 top-1)")
        print(f"  RECALL@3:         {recall3}/{n} = {100 * recall3 / n:.1f}%   <-- expected in top-3 (what the cascade shows the user)")
        print(f"  L2b shortcut hits:{shortcut_hits}/{n} = {100 * shortcut_hits / n:.1f}%   (driver self-questions served before L3)")
        print(f"  scope_miss:       {scope_miss}/{n} = {100 * scope_miss / n:.1f}%   (expected tool not even a candidate)")
        print(f"  route_miss:       {route_miss}/{n} = {100 * route_miss / n:.1f}%   (in scope/shortcut, but wrong pick)")

        # EXACT route_miss taxonomy over the FULL list (not the [:14] sample
        # printed below). in_top50 is checked BEFORE the got-none branch: if the
        # anchor never surfaced the expected tool, that retrieval failure is the
        # upstream cause regardless of whether the LLM then declined.
        rm = Counter()
        for _q, _exp, _got, _err, _t3, _in50 in route_misses:
            if _err == "wrong_shortcut":
                cat = "wrong_shortcut_L2b"   # L2b fired on a non-MasterData query
            elif _in50 is False:
                cat = "retrieval_miss"        # anchor cosine never surfaced expected
            elif not _got or _got == "(none)":
                cat = "no_tool_call"          # LLM saw expected in top-50, declined
            else:
                cat = "wrong_pick"            # LLM saw it, picked another
            rm[cat] += 1
        print(
            "  route_miss breakdown: "
            f"no_tool_call={rm['no_tool_call']} "
            f"wrong_pick={rm['wrong_pick']} "
            f"retrieval_miss={rm['retrieval_miss']} "
            f"wrong_shortcut_L2b={rm['wrong_shortcut_L2b']} "
            f"(sum={sum(rm.values())} == route_miss={route_miss})"
        )

    _print_segment("By HTTP method", by_method)
    _print_segment("By persona", by_persona)

    if scope_misses:
        print("\n  --- scope_miss sample (expected tool not in candidate set) ---")
        for query, exp, per, meth, nscope in scope_misses[:10]:
            print(f"    '{query[:45]}' exp={exp} persona={per} method={meth} scope_size={nscope}")

    if route_misses:
        print("\n  --- route_miss sample (in scope/shortcut, wrong pick) ---")
        print("    (exp_in_top50: was the expected tool even among the 50 the LLM saw?)")
        for query, exp, got, err, top3, in_top50 in route_misses[:14]:
            flag = "n/a" if in_top50 is None else ("YES" if in_top50 else "NO")
            print(f"    '{query[:42]}' exp={exp} got={got or '(none)'} "
                  f"err={err or '-'} exp_in_top50={flag}")
            if top3:
                print(f"        top3: {top3}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tool-data", type=Path, default=DEFAULT_TOOL_DATA)
    ap.add_argument("--benchmark-file", type=Path, default=DEFAULT_BENCH_FILE)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--no-method-filter", action="store_true")
    ap.add_argument("--no-l2b", action="store_true",
                    help="Skip the L2b driver-basics shortcut — measure the "
                         "pure L3 router (measurement-only; does not change "
                         "production routing).")
    ap.add_argument("--tenant", type=str, default=None)
    ap.add_argument("--role-on", action="store_true",
                    help="Re-enable FAZA 14 persona hierarchy (default OFF = "
                         "persona=None, matching current launch config).")
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()
