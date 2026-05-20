"""Honest benchmark for the new L3 LLM router (Phase 5).

Runs two passes:
1. TKB-examples sweep — 950 queries derived from each tool's TKB
   `examples[0].input`. These are NEAR-CANONICAL for each tool; the
   accuracy here is the optimistic ceiling.
2. Held-out objective benchmark — 100 queries from
   tests/benchmarks/objective_benchmark_100.json. These queries were NOT
   used in Phase 1 data regeneration; their accuracy is the honest signal
   for production.

Outputs:
- tests/benchmarks/router_results_tkb.json
- tests/benchmarks/router_results_heldout.json
- Console: strict %, per-domain %, latency p50/p95, errors

Cost: ~$0.50 ($0.0005/call × 1050 queries). Time: ~10-15 min.

Run: python -X utf8 -u scripts/bench_router.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from openai import AsyncAzureOpenAI

from config import get_settings
from services.router.anchor_index import AnchorIndex
from services.router.tool_schema_builder import ToolSchemaBuilder
from services.router.llm_router import LLMRouter


def _load(p):
    return json.loads((REPO / p).read_text(encoding="utf-8"))


async def _build_router():
    s = get_settings()
    client = AsyncAzureOpenAI(
        azure_endpoint=s.AZURE_OPENAI_ENDPOINT,
        api_key=s.AZURE_OPENAI_API_KEY,
        api_version=s.AZURE_OPENAI_API_VERSION,
        max_retries=0,
        timeout=30.0,
    )

    async def embed_fn(texts):
        r = await client.embeddings.create(
            input=texts, model=s.AZURE_OPENAI_EMBEDDING_DEPLOYMENT,
        )
        return [d.embedding for d in r.data]

    tool_data = _load("config/tool_data.json")
    tool_entries = tool_data.get("tools", {})
    registry = {
        "tools": list(tool_entries.values()),
        "dependency_graph": tool_data.get("dependency_graph") or [],
    }
    tkb = {
        op_id: {
            "intent_summary": entry.get("intent_summary", ""),
            "use_when": entry.get("use_when") or [],
            "do_not_use_when": entry.get("do_not_use_when") or [],
            "method": entry.get("method", "GET"),
        }
        for op_id, entry in tool_entries.items()
    }
    anchors_data = {
        op_id: list(entry.get("anchors") or [])
        for op_id, entry in tool_entries.items()
        if entry.get("anchors")
    }

    idx = AnchorIndex(
        anchors_data=anchors_data,
        cache_path=REPO / "tests" / "benchmarks" / "router_anchor_cache.json",
        embedding_deployment=s.AZURE_OPENAI_EMBEDDING_DEPLOYMENT,
    )
    schema_builder = ToolSchemaBuilder.from_registry(registry)
    router = LLMRouter(
        anchor_index=idx, schema_builder=schema_builder,
        registry=registry, tkb=tkb,
        llm_client=client, embed_fn=embed_fn,
        deployment_name=s.AZURE_OPENAI_DEPLOYMENT_NAME,
    )
    await router.initialize()
    return router


async def _run_pass(
    router, queries: list[dict], label: str, out_path: Path,
    concurrency: int = 8,
):
    print(f"\n=== {label}: {len(queries)} queries ===", flush=True)
    sem = asyncio.Semaphore(concurrency)
    results: list[dict] = []
    t_start = time.time()

    async def one(i, q):
        async with sem:
            t0 = time.perf_counter()
            try:
                r = await router.route(
                    query=q["query"], identity_summary="(test user)",
                )
                results.append({
                    "id": q.get("id", i),
                    "query": q["query"],
                    "expected_tool": q.get("expected_tool"),
                    "expected_domain": q.get("expected_domain"),
                    "v3_tool_id": r.tool_id,
                    "v3_params": r.params,
                    "v3_confidence": r.confidence,
                    "v3_anchor_score": r.anchor_score,
                    "v3_error": r.error,
                    "latency_ms": int((time.perf_counter() - t0) * 1000),
                })
            except Exception as e:  # noqa: BLE001
                results.append({
                    "id": q.get("id", i),
                    "query": q["query"],
                    "expected_tool": q.get("expected_tool"),
                    "v3_error": f"{type(e).__name__}: {str(e)[:80]}",
                })
            if (len(results)) % 50 == 0:
                elapsed = time.time() - t_start
                rate = len(results) / max(elapsed, 0.01)
                eta = (len(queries) - len(results)) / max(rate, 0.01)
                print(
                    f"  {len(results)}/{len(queries)} | {rate:.1f}/s | ETA {eta:.0f}s",
                    flush=True,
                )

    await asyncio.gather(*[one(i, q) for i, q in enumerate(queries)])

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps({"queries": results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    _print_metrics(label, results)


def _print_metrics(label: str, results: list[dict]):
    strict_ok = sum(
        1 for r in results
        if r.get("expected_tool") and r.get("v3_tool_id") == r.get("expected_tool")
    )
    measured = sum(1 for r in results if r.get("expected_tool"))
    errors = sum(1 for r in results if r.get("v3_error"))
    no_call = sum(
        1 for r in results
        if (r.get("v3_error") or "").startswith("no_tool_call")
    )
    halluc = sum(
        1 for r in results
        if (r.get("v3_error") or "").startswith("hallucinated_tool")
    )
    latencies = sorted([r.get("latency_ms", 0) for r in results if r.get("latency_ms")])
    p50 = latencies[len(latencies) // 2] if latencies else 0
    p95 = latencies[int(len(latencies) * 0.95)] if latencies else 0

    pct = strict_ok * 100 / max(measured, 1)
    print(f"\n{label} METRICS", flush=True)
    print(f"  STRICT: {strict_ok}/{measured} = {pct:.1f}%", flush=True)
    print(f"  Errors: {errors} (no_tool_call={no_call}, halluc={halluc})", flush=True)
    print(f"  Latency: p50={p50}ms p95={p95}ms", flush=True)

    # Per-domain (objective_benchmark queries have expected_domain)
    by_domain = defaultdict(lambda: {"strict": 0, "total": 0})
    for r in results:
        dom = r.get("expected_domain") or "?"
        if r.get("expected_tool"):
            by_domain[dom]["total"] += 1
            if r.get("v3_tool_id") == r.get("expected_tool"):
                by_domain[dom]["strict"] += 1
    if any(c["total"] for c in by_domain.values()):
        print(f"  Per-domain:", flush=True)
        for dom, c in sorted(by_domain.items(), key=lambda x: -x[1]["total"]):
            if not c["total"]:
                continue
            pct_d = c["strict"] * 100 // max(c["total"], 1)
            print(f"    {dom:<22} {c['strict']}/{c['total']} ({pct_d}%)", flush=True)


async def main():
    print("Loading registry + TKB + building router...", flush=True)
    router = await _build_router()

    # Pass 1 — TKB-examples sweep (deprecated after Phase 1 union-merge dropped
    # the `examples` field as dead data). Kept here only if a stale archive
    # exists, otherwise the pass is skipped.
    archive = REPO / "config" / "tool_knowledge_base.archive.json"
    tkb_queries: list[dict] = []
    if archive.exists():
        tkb = json.loads(archive.read_text(encoding="utf-8")).get("tools", {})
        for op_id, entry in tkb.items():
            examples = entry.get("examples") or []
            first_input = None
            for ex in examples:
                if isinstance(ex, dict):
                    v = ex.get("input")
                    if v:
                        first_input = v
                        break
                elif isinstance(ex, str) and ex:
                    first_input = ex
                    break
            if first_input:
                tkb_queries.append({"query": first_input, "expected_tool": op_id})

    if tkb_queries:
        await _run_pass(
            router, tkb_queries, "TKB-EXAMPLES SWEEP (from archive)",
            REPO / "tests" / "benchmarks" / "router_results_tkb.json",
            concurrency=2,
        )
    else:
        print("Pass 1 (TKB-EXAMPLES SWEEP) skipped — no archive with examples present.", flush=True)

    # Pass 2 — held-out objective benchmark (honest signal)
    obj = _load("tests/benchmarks/objective_benchmark_100.json")
    held_out = [q for q in obj["queries"] if q.get("expected_tool")]
    await _run_pass(
        router, held_out, "HELD-OUT OBJECTIVE BENCH",
        REPO / "tests" / "benchmarks" / "router_results_heldout.json",
        concurrency=2,
    )

    print("\n=== DONE ===", flush=True)
    print(
        f"Baseline (Phase 7, pre-rewrite): 9.1% strict on 950 tools.",
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
