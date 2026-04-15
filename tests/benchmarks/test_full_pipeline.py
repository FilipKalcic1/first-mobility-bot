"""
Full pipeline FORENSIC benchmark — tests through UnifiedRouter.route() (the REAL system).

Unlike test_tool_recognition.py which only tests UnifiedSearch.search() (candidate
generation), this benchmark tests the ENTIRE routing pipeline:
  - Pattern matching (greetings, exit signals)                  → DETERMINISTIC
  - QueryRouter ML fast path (TF-IDF classifier)               → FAST_PATH
  - Mediation (LLM rerank of ML candidates)                    → MEDIATION
  - Full LLM routing (UnifiedSearch + LLM tool selection)       → FULL_SEARCH

Cross-references each query against raw UnifiedSearch results to answer:
  1. Which tier handled each query?
  2. Was the expected tool in UnifiedSearch top-20 but LLM missed it?
  3. Is clarify triggered appropriately for ambiguous vs distinguishable queries?
  4. What is the LLM rerank accuracy when the tool IS in the candidate set?

WARNING: Each query costs ~$0.003-0.005 in Azure OpenAI tokens.
         290 queries ≈ $1.00-1.50.

Reads:  tests/benchmarks/tool_recognition_paraphrases.json
Writes: tests/benchmarks/last_run_full_pipeline.json
        tests/benchmarks/last_run_forensic_details.json

Run with:
    python -X utf8 tests/benchmarks/test_full_pipeline.py
"""
import asyncio
import json
import os
import sys
import time
import statistics
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

import logging
logging.basicConfig(level=logging.WARNING)
for name in ("services", "openai", "httpx", "urllib3", "faiss"):
    logging.getLogger(name).setLevel(logging.WARNING)

PARAPHRASES_PATH = PROJECT_ROOT / "tests" / "benchmarks" / "tool_recognition_paraphrases.json"
RESULTS_PATH = PROJECT_ROOT / "tests" / "benchmarks" / "last_run_full_pipeline.json"
FORENSIC_PATH = PROJECT_ROOT / "tests" / "benchmarks" / "last_run_forensic_details.json"

# Import ambiguity classification from the UnifiedSearch-only benchmark
from tests.benchmarks.test_tool_recognition import _ENTITY_STEMS, _classify_ambiguity


async def bootstrap_router():
    """Initialize ToolRegistry + UnifiedSearch + UnifiedRouter. Returns both."""
    from services.registry import ToolRegistry
    from services.unified_search import initialize_unified_search, get_unified_search
    from services.unified_router import UnifiedRouter

    print("Bootstrapping ToolRegistry...")
    registry = ToolRegistry()
    ok = await registry.initialize(swagger_sources=[])
    if not ok:
        raise RuntimeError("ToolRegistry.initialize returned False")
    print(f"  Registry ready: {registry._store.count()} tools")

    print("Bootstrapping UnifiedSearch (loads FAISS, TFI, BM25)...")
    us = await initialize_unified_search(registry=registry)
    print("  UnifiedSearch ready")

    print("Bootstrapping UnifiedRouter...")
    router = UnifiedRouter(registry=registry)
    await router.initialize()
    print("  UnifiedRouter ready")
    return router, us


async def run_benchmark(router, us, data: dict) -> tuple:
    total_tools = len(data["tools"])
    total_queries = sum(len(t["paraphrases"]) for t in data["tools"])

    # Pre-classify ambiguity for each tool
    ambiguity_map = {}
    for tool_entry in data["tools"]:
        ambiguity_map[tool_entry["tool_id"]] = _classify_ambiguity(tool_entry)

    # Minimal user_context for routing
    user_context = {
        "person_id": "benchmark-user-001",
        "tenant_id": "benchmark-tenant",
        "vehicle": {
            "id": "v-bench-001",
            "name": "Test Vozilo",
            "plate": "ZG9999BM",
        }
    }

    results = {
        "seed": data.get("seed"),
        "sample_size": total_tools,
        "paraphrases_per_tool": data.get("paraphrases_per_tool"),
        "total_queries": total_queries,
        "exact_hits": 0,
        "clarify_aware_hits": 0,
        "search_ceiling": 0,
        "llm_rerank_hits": 0,
        "llm_degradation": 0,
        "tier_stats": {
            "deterministic": 0,
            "fast_path": 0,
            "mediation": 0,
            "full_search": 0,
            "fallback": 0,
        },
        "tier_hits": {
            "deterministic": 0,
            "fast_path": 0,
            "mediation": 0,
            "full_search": 0,
            "fallback": 0,
        },
        "action_stats": {},
        "per_tool": [],
    }

    tier_latencies = {
        "deterministic": [],
        "fast_path": [],
        "mediation": [],
        "full_search": [],
        "fallback": [],
    }

    forensic_records = []

    query_idx = 0
    for tool_entry in data["tools"]:
        expected = tool_entry["tool_id"]
        is_ambiguous = ambiguity_map[expected]
        tool_hits = 0
        tool_clarify_aware = 0
        n_paraphrases = len(tool_entry["paraphrases"])

        for paraphrase in tool_entry["paraphrases"]:
            query_idx += 1

            # --- Full pipeline (route) ---
            t_start = time.perf_counter()
            try:
                decision = await asyncio.wait_for(
                    router.route(
                        query=paraphrase,
                        user_context=user_context,
                        conversation_state=None
                    ),
                    timeout=30.0
                )
            except asyncio.TimeoutError:
                print(f"  [TIMEOUT {query_idx}/{total_queries}] {expected}", flush=True)
                forensic_records.append({
                    "expected": expected, "query": paraphrase,
                    "error": "timeout", "is_ambiguous": is_ambiguous,
                })
                continue
            except Exception as e:
                err_msg = str(e)[:100]
                print(f"  [ERR {query_idx}/{total_queries}] {expected}: {err_msg}", flush=True)
                forensic_records.append({
                    "expected": expected, "query": paraphrase,
                    "error": err_msg, "is_ambiguous": is_ambiguous,
                })
                continue
            latency_ms = (time.perf_counter() - t_start) * 1000

            # --- Raw UnifiedSearch (cross-reference, no LLM cost) ---
            try:
                search_response = await us.search(paraphrase, top_k=20)
                search_ids = [r.tool_id for r in search_response.results]
                search_scores = [r.score for r in search_response.results]
            except Exception:
                search_ids = []
                search_scores = []

            selected_tool = decision.tool
            action = decision.action
            confidence = decision.confidence
            tier = decision.routing_tier

            # --- Tier stats ---
            if tier in results["tier_stats"]:
                results["tier_stats"][tier] += 1
            tier_latencies.setdefault(tier, []).append(latency_ms)

            # --- Action stats ---
            results["action_stats"][action] = results["action_stats"].get(action, 0) + 1

            # --- Exact hit ---
            is_hit = (selected_tool == expected) if selected_tool else False
            if is_hit:
                results["exact_hits"] += 1
                tool_hits += 1
                if tier in results["tier_hits"]:
                    results["tier_hits"][tier] += 1

            # --- Clarify-aware scoring ---
            if is_hit:
                results["clarify_aware_hits"] += 1
                tool_clarify_aware += 1
            elif action == "clarify" and is_ambiguous:
                results["clarify_aware_hits"] += 1
                tool_clarify_aware += 1

            # --- Search cross-reference ---
            in_top1 = expected in search_ids[:1]
            in_top5 = expected in search_ids[:5]
            in_top20 = expected in search_ids[:20]
            search_rank = search_ids.index(expected) if expected in search_ids else -1

            if in_top20:
                results["search_ceiling"] += 1
                if is_hit:
                    results["llm_rerank_hits"] += 1

            if in_top1 and not is_hit:
                results["llm_degradation"] += 1

            # --- Forensic record ---
            forensic_records.append({
                "expected": expected,
                "query": paraphrase,
                "selected": selected_tool,
                "action": action,
                "confidence": round(confidence, 4),
                "routing_tier": tier,
                "reasoning": decision.reasoning[:200] if decision.reasoning else "",
                "is_ambiguous": is_ambiguous,
                "is_hit": is_hit,
                "in_search_top1": in_top1,
                "in_search_top5": in_top5,
                "in_search_top20": in_top20,
                "search_rank": search_rank,
                "search_top5": [(tid, round(s, 4)) for tid, s in zip(search_ids[:5], search_scores[:5])],
                "latency_ms": round(latency_ms, 1),
            })

            # Progress every 10 queries
            if query_idx % 10 == 0:
                pct = results["exact_hits"] / query_idx * 100
                ca_pct = results["clarify_aware_hits"] / query_idx * 100
                print(f"  [{query_idx}/{total_queries}] "
                      f"Exact={results['exact_hits']}/{query_idx} ({pct:.1f}%) "
                      f"Clarify-aware={results['clarify_aware_hits']}/{query_idx} ({ca_pct:.1f}%)",
                      flush=True)

        results["per_tool"].append({
            "tool_id": expected,
            "index": tool_entry.get("index"),
            "hits": tool_hits,
            "clarify_aware_hits": tool_clarify_aware,
            "n": n_paraphrases,
            "is_ambiguous": is_ambiguous,
        })

    results["tier_latencies"] = {}
    for tier, lats in tier_latencies.items():
        if lats:
            results["tier_latencies"][tier] = {
                "count": len(lats),
                "p50_ms": round(statistics.median(lats), 1),
                "p95_ms": round(sorted(lats)[int(len(lats) * 0.95)], 1) if len(lats) >= 2 else round(lats[0], 1),
                "mean_ms": round(statistics.mean(lats), 1),
            }

    return results, forensic_records


def print_report(results: dict, forensic_records: list, data: dict):
    n = results["total_queries"]
    hits = results["exact_hits"]
    ca_hits = results["clarify_aware_hits"]

    print("\n" + "=" * 70)
    print("FULL PIPELINE FORENSIC BENCHMARK RESULTS")
    print("=" * 70)
    print(f"Sample:      {results['sample_size']} tools × "
          f"{results['paraphrases_per_tool']} paraphrases = {n} queries")
    print(f"Exact match: {hits}/{n} = {hits/n:.1%}")
    print(f"Clarify-aware: {ca_hits}/{n} = {ca_hits/n:.1%}")

    # ── Section A: Tier Breakdown ──
    print(f"\n{'─'*70}")
    print("SECTION A: TIER BREAKDOWN")
    print(f"{'─'*70}")
    for tier in ["deterministic", "fast_path", "mediation", "full_search", "fallback"]:
        count = results["tier_stats"].get(tier, 0)
        tier_hit = results["tier_hits"].get(tier, 0)
        if count == 0:
            print(f"  {tier:20s}: {count:4d} queries ({0:.1%})")
            continue
        pct = count / n
        acc = tier_hit / count
        lat = results.get("tier_latencies", {}).get(tier, {})
        lat_str = ""
        if lat:
            lat_str = f"  P50={lat['p50_ms']:.0f}ms  P95={lat['p95_ms']:.0f}ms"
        print(f"  {tier:20s}: {count:4d} queries ({pct:.1%})  "
              f"accuracy={tier_hit}/{count} ({acc:.1%}){lat_str}")

    # ── Section B: LLM Rerank Accuracy ──
    print(f"\n{'─'*70}")
    print("SECTION B: LLM RERANK ACCURACY (full_search tier)")
    print(f"{'─'*70}")
    ceiling = results["search_ceiling"]
    rerank_hits = results["llm_rerank_hits"]
    degradation = results["llm_degradation"]

    print(f"  Expected tool in UnifiedSearch top-20: {ceiling}/{n} ({ceiling/n:.1%}) [search ceiling]")
    if ceiling > 0:
        # Break down what happened to queries where tool WAS in top-20
        clarify_in_top20 = sum(
            1 for r in forensic_records
            if r.get("in_search_top20") and r.get("action") == "clarify"
        )
        wrong_in_top20 = ceiling - rerank_hits - clarify_in_top20
        print(f"  LLM selected correct tool:             {rerank_hits}/{ceiling} ({rerank_hits/ceiling:.1%})")
        print(f"  LLM returned clarify:                  {clarify_in_top20}/{ceiling} ({clarify_in_top20/ceiling:.1%})")
        print(f"  LLM selected wrong tool:               {wrong_in_top20}/{ceiling} ({wrong_in_top20/ceiling:.1%})")
    print(f"  LLM degradation (in top-1 but missed):  {degradation}")

    # Compare with US Top-1
    us_results_path = PROJECT_ROOT / "tests" / "benchmarks" / "last_run_results.json"
    if us_results_path.exists():
        with open(us_results_path, "r", encoding="utf-8") as f:
            us_data = json.load(f)
        us_n = us_data["total_queries"]
        us_top1 = us_data["top1_hits"]
        us_top5 = us_data["top5_hits"]
        us_top20 = us_data["top20_hits"]
        print(f"\n  Comparison with UnifiedSearch-only:")
        print(f"    US Top-1:       {us_top1}/{us_n} = {us_top1/us_n:.1%}")
        print(f"    US Top-5:       {us_top5}/{us_n} = {us_top5/us_n:.1%}")
        print(f"    US Top-20:      {us_top20}/{us_n} = {us_top20/us_n:.1%}")
        print(f"    Full Pipeline:  {hits}/{n} = {hits/n:.1%}")
        delta = (hits/n - us_top1/us_n) * 100
        print(f"    Delta vs US Top-1: {delta:+.1f}pp")

    # ── Section C: Clarify Analysis ──
    print(f"\n{'─'*70}")
    print("SECTION C: CLARIFY ANALYSIS (safety vs accuracy)")
    print(f"{'─'*70}")

    total_clarify = results["action_stats"].get("clarify", 0)
    amb_total = sum(1 for r in forensic_records if r.get("is_ambiguous") and "error" not in r)
    dist_total = sum(1 for r in forensic_records if not r.get("is_ambiguous") and "error" not in r)

    clarify_on_amb = sum(
        1 for r in forensic_records
        if r.get("action") == "clarify" and r.get("is_ambiguous")
    )
    clarify_on_dist = sum(
        1 for r in forensic_records
        if r.get("action") == "clarify" and not r.get("is_ambiguous")
    )

    print(f"  Total clarify actions:       {total_clarify}/{n} ({total_clarify/n:.1%})")
    print(f"  Ambiguous queries:           {amb_total}")
    print(f"    → clarify (correct):       {clarify_on_amb}/{amb_total} ({clarify_on_amb/amb_total:.1%})" if amb_total else "")
    print(f"  Distinguishable queries:     {dist_total}")
    print(f"    → clarify (over-trigger):  {clarify_on_dist}/{dist_total} ({clarify_on_dist/dist_total:.1%})" if dist_total else "")
    print()
    print(f"  Clarify-aware accuracy:      {ca_hits}/{n} = {ca_hits/n:.1%}")
    print(f"    (exact hits + correct clarify for ambiguous queries)")

    # Distinguishable-only exact accuracy
    dist_hits = sum(
        1 for r in forensic_records
        if r.get("is_hit") and not r.get("is_ambiguous")
    )
    if dist_total:
        print(f"  Distinguishable exact acc:   {dist_hits}/{dist_total} = {dist_hits/dist_total:.1%}")

    # ── Section D: Cross-Reference Matrix ──
    print(f"\n{'─'*70}")
    print("SECTION D: CROSS-REFERENCE MATRIX (UnifiedSearch vs Pipeline)")
    print(f"{'─'*70}")

    # Build matrix
    matrix = {}
    for bucket in ["top1", "top2-5", "top6-20", "not_in_top20"]:
        matrix[bucket] = {"exact_hit": 0, "clarify": 0, "wrong_tool": 0, "other": 0}

    for r in forensic_records:
        if "error" in r:
            continue
        rank = r.get("search_rank", -1)
        if rank == 0:
            bucket = "top1"
        elif 1 <= rank <= 4:
            bucket = "top2-5"
        elif 5 <= rank <= 19:
            bucket = "top6-20"
        else:
            bucket = "not_in_top20"

        if r.get("is_hit"):
            matrix[bucket]["exact_hit"] += 1
        elif r.get("action") == "clarify":
            matrix[bucket]["clarify"] += 1
        elif r.get("selected") and r.get("selected") != r.get("expected"):
            matrix[bucket]["wrong_tool"] += 1
        else:
            matrix[bucket]["other"] += 1

    header = f"  {'Search position':20s} | {'Exact hit':>10s} | {'Clarify':>10s} | {'Wrong tool':>10s} | {'Other':>10s}"
    print(header)
    print(f"  {'─'*20}-+-{'─'*10}-+-{'─'*10}-+-{'─'*10}-+-{'─'*10}")
    for bucket in ["top1", "top2-5", "top6-20", "not_in_top20"]:
        row = matrix[bucket]
        total_row = sum(row.values())
        print(f"  {bucket:20s} | {row['exact_hit']:10d} | {row['clarify']:10d} | {row['wrong_tool']:10d} | {row['other']:10d}  (n={total_row})")

    # Key insight
    top1_missed = matrix["top1"]["clarify"] + matrix["top1"]["wrong_tool"] + matrix["top1"]["other"]
    if top1_missed > 0:
        print(f"\n  KEY INSIGHT: {top1_missed} queries had correct tool at search #1 but pipeline missed")
        print(f"  → These are 'LLM degradation' queries where search was right but LLM picked wrong")

    # ── Action Distribution ──
    print(f"\n{'─'*70}")
    print("ACTION DISTRIBUTION")
    print(f"{'─'*70}")
    for action, count in sorted(results["action_stats"].items(), key=lambda x: -x[1]):
        print(f"  {action:20s}: {count:4d} ({count/n:.1%})")

    # ── Per-tool summary ──
    full_hits = sum(1 for t in results["per_tool"] if t["hits"] == t["n"])
    partial = sum(1 for t in results["per_tool"] if 0 < t["hits"] < t["n"])
    zero = sum(1 for t in results["per_tool"] if t["hits"] == 0)
    print(f"\n  Tools with 3/3 hits: {full_hits}")
    print(f"  Tools with 1-2/3:   {partial}")
    print(f"  Tools with 0/3:     {zero}")

    # ── Verdict ──
    print(f"\n{'='*70}")
    print("VERDICT")
    print(f"{'='*70}")
    pct = hits / n if n else 0
    ca_pct = ca_hits / n if n else 0
    if ca_pct >= 0.80:
        print(f"  PASS: Clarify-aware accuracy {ca_pct:.1%} >= 80%")
    else:
        print(f"  BELOW TARGET: Clarify-aware accuracy {ca_pct:.1%} < 80%")
    if pct >= 0.80:
        print(f"  PASS: Raw exact accuracy {pct:.1%} >= 80%")
    else:
        print(f"  NOTE: Raw exact accuracy {pct:.1%} < 80% (expected for paraphrased queries)")

    if ceiling > 0:
        rerank_pct = rerank_hits / ceiling
        if rerank_pct >= 0.50:
            print(f"  LLM rerank accuracy: {rerank_pct:.1%} (when tool in top-20)")
        else:
            print(f"  WARNING: LLM rerank accuracy only {rerank_pct:.1%} — LLM is not selecting correctly from candidates")


async def main():
    if not PARAPHRASES_PATH.exists():
        print(f"FATAL: Paraphrase ground truth not found at {PARAPHRASES_PATH}")
        sys.exit(1)

    print(f"Loading paraphrases from {PARAPHRASES_PATH}")
    with open(PARAPHRASES_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    print(f"  {len(data['tools'])} tools, "
          f"{sum(len(t['paraphrases']) for t in data['tools'])} paraphrases")

    t0 = time.time()
    router, us = await bootstrap_router()
    print(f"  Bootstrap took {time.time() - t0:.1f}s")

    print(f"\nRunning full pipeline forensic benchmark...")
    t1 = time.time()
    results, forensic_records = await run_benchmark(router, us, data)
    elapsed = time.time() - t1
    print(f"  Benchmark took {elapsed:.1f}s "
          f"({elapsed / results['total_queries'] * 1000:.0f}ms per query)")

    # Save raw results
    with open(RESULTS_PATH, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\nRaw results saved to {RESULTS_PATH}")

    # Save forensic details
    with open(FORENSIC_PATH, "w", encoding="utf-8") as f:
        json.dump(forensic_records, f, ensure_ascii=False, indent=2)
    print(f"Forensic details saved to {FORENSIC_PATH}")

    # Print full report
    print_report(results, forensic_records, data)


if __name__ == "__main__":
    asyncio.run(main())
