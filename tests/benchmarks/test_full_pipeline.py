"""
Full Pipeline Benchmark — measures REAL production accuracy.

Uses UnifiedSearch.search() directly (same code path as production),
with LLM reranker and HyDE disabled for reproducibility and $0 cost.

Measures Top-1, Top-5, Top-10, Top-25 on:
  1. Adversarial benchmark (seed=99, 176 queries — no entity keywords)
  2. Production benchmark (example_queries_hr from tool_documentation)

This replaces the misleading run_combined_benchmark.py which used a
simplified TFI→FAISS without boost engine, BM25, exact match index,
entity inference, or proper score merging.
"""
import json
import sys
import os
import asyncio
import random
import time
from unittest.mock import patch, AsyncMock, MagicMock
from dataclasses import dataclass
from typing import Dict, Optional, Any, Set

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# Disable HyDE before importing (env var check happens at import time)
os.environ["HYDE_ENABLED"] = "false"


# ---------------------------------------------------------------------------
# Minimal mock registry — UnifiedSearch only needs .tools dict and .get_tool()
# ---------------------------------------------------------------------------
@dataclass
class MockToolDef:
    tool_id: str
    method: str
    path: str = ""
    summary: str = ""
    description: str = ""


class MockRegistry:
    def __init__(self, tool_ids):
        self._tools = {}
        for tid in tool_ids:
            parts = tid.split("_")
            method = parts[0].upper() if parts else "GET"
            self._tools[tid] = MockToolDef(tool_id=tid, method=method)

    @property
    def tools(self) -> Dict[str, MockToolDef]:
        return self._tools

    def get_tool(self, operation_id: str) -> Optional[MockToolDef]:
        return self._tools.get(operation_id)

    @property
    def is_ready(self) -> bool:
        return True

    @property
    def retrieval_tools(self) -> Set[str]:
        return {tid for tid in self._tools if tid.startswith("get_")}

    @property
    def mutation_tools(self) -> Set[str]:
        return {tid for tid in self._tools if not tid.startswith("get_")}


async def run_benchmark():
    # Load tool documentation
    with open("config/tool_documentation.json", "r", encoding="utf-8") as f:
        docs = json.load(f)

    tool_ids = list(docs.keys())
    print(f"Loaded {len(tool_ids)} tools from tool_documentation.json")

    # Reset singletons to ensure clean state
    import services.unified_search as us_module
    import services.faiss_vector_store as fvs_module
    import services.tool_family_index as tfi_module

    us_module._unified_search = None
    tfi_module._family_index = None

    # Initialize FAISS store (uses cached ada-002 embeddings)
    faiss_store = fvs_module.get_faiss_store()
    if not faiss_store.is_initialized():
        cached = faiss_store._load_cached_embeddings()
        faiss_store._embeddings = cached
        faiss_store._tool_ids = list(cached.keys())
        faiss_store._tool_methods = {}
        for tid in faiss_store._tool_ids:
            parts = tid.split("_")
            faiss_store._tool_methods[tid] = parts[0].upper() if parts else "GET"
        faiss_store._build_index()
        faiss_store._initialized = True
    print(f"FAISS index: {len(faiss_store._tool_ids)} tools, dim={fvs_module.EMBEDDING_DIM}")

    # Initialize UnifiedSearch with mock registry
    registry = MockRegistry(tool_ids)
    from services.unified_search import UnifiedSearch
    search = UnifiedSearch(registry=registry)

    # Patch rerank_with_llm to be a no-op (skip LLM calls, $0)
    async def mock_rerank(query, candidates, top_k=20, tool_documentation=None):
        # Return candidates as-is (no reranking)
        return [MagicMock(tool_id=c['tool_id'], score=c['score']) for c in candidates[:top_k]]

    await search.initialize()
    print(f"UnifiedSearch initialized (exact match index: {len(search._exact_match_index)} entries)")
    print(f"BM25 index: {'built' if search._bm25_index and search._bm25_index.is_built else 'not built'}")

    # =========================================================================
    # TEST SET 1: Adversarial benchmark (seed=99, 176 queries)
    # =========================================================================
    with open("tests/benchmarks/tool_recognition_paraphrases.seed99.json", "r", encoding="utf-8") as f:
        bench = json.load(f)

    adv_queries = []
    for te in bench["tools"]:
        for q in te["paraphrases"]:
            adv_queries.append((q, te["tool_id"]))

    print(f"\n{'='*80}")
    print(f"  ADVERSARIAL BENCHMARK (seed=99, {len(adv_queries)} queries)")
    print(f"{'='*80}")

    adv_stats = {"top1": 0, "top5": 0, "top10": 0, "top25": 0,
                 "entity_detected": 0, "exact_match": 0, "not_found": 0}
    adv_failures = []

    t0 = time.time()
    with patch("services.llm_reranker.rerank_with_llm", side_effect=mock_rerank):
        for i, (query, expected) in enumerate(adv_queries):
            if i % 20 == 0:
                print(f"  [{i}/{len(adv_queries)}]...", end="\r")

            response = await search.search(query, top_k=25)
            result_ids = [r.tool_id.lower() for r in response.results]
            exp_lower = expected.lower()

            if response.detected_entity:
                adv_stats["entity_detected"] += 1

            pos = None
            for j, rid in enumerate(result_ids):
                if rid == exp_lower:
                    pos = j
                    break

            if pos is not None:
                if pos == 0:
                    adv_stats["top1"] += 1
                if pos < 5:
                    adv_stats["top5"] += 1
                if pos < 10:
                    adv_stats["top10"] += 1
                if pos < 25:
                    adv_stats["top25"] += 1
            else:
                adv_stats["not_found"] += 1
                if len(adv_failures) < 15:
                    adv_failures.append({
                        "query": query,
                        "expected": expected,
                        "entity": response.detected_entity,
                        "top3": [r.tool_id for r in response.results[:3]],
                    })

    adv_time = time.time() - t0
    n = len(adv_queries)

    print(f"\n  Results ({adv_time:.1f}s, {adv_time/n*1000:.0f}ms/query):")
    print(f"  {'Metric':<25} {'Count':<10} {'Accuracy':<10}")
    print(f"  {'-'*45}")
    print(f"  {'Top-1':<25} {adv_stats['top1']}/{n}      {100*adv_stats['top1']/n:.1f}%")
    print(f"  {'Top-5':<25} {adv_stats['top5']}/{n}      {100*adv_stats['top5']/n:.1f}%")
    print(f"  {'Top-10':<25} {adv_stats['top10']}/{n}      {100*adv_stats['top10']/n:.1f}%")
    print(f"  {'Top-25':<25} {adv_stats['top25']}/{n}      {100*adv_stats['top25']/n:.1f}%")
    print(f"  {'Entity detected':<25} {adv_stats['entity_detected']}/{n}      {100*adv_stats['entity_detected']/n:.1f}%")
    print(f"  {'Not in top-25':<25} {adv_stats['not_found']}/{n}      {100*adv_stats['not_found']/n:.1f}%")

    if adv_failures:
        print(f"\n  SAMPLE ADVERSARIAL FAILURES (top {len(adv_failures)}):")
        for f in adv_failures[:10]:
            print(f"    Q: {f['query'][:65]}")
            print(f"       Expected: {f['expected']}, Entity: {f['entity']}")
            print(f"       Top-3: {f['top3']}")

    # =========================================================================
    # TEST SET 2: Production benchmark (example_queries_hr, primary tools)
    # =========================================================================
    random.seed(42)
    sampled = random.sample(tool_ids, min(200, len(tool_ids)))

    def is_primary(tid):
        p = tid.split("_")
        if len(p) >= 4 and any(x in ("documents", "documentId", "thumb", "metadata") for x in p):
            return False
        if "Lookup" in tid:
            return False
        return True

    prod_queries = []
    for tid in sampled:
        if not is_primary(tid):
            continue
        doc = docs[tid]
        qs = doc.get("example_queries_hr", doc.get("example_queries", []))
        for q in qs[:2]:
            prod_queries.append((q, tid))

    print(f"\n{'='*80}")
    print(f"  PRODUCTION BENCHMARK (primary tools, {len(prod_queries)} queries)")
    print(f"{'='*80}")

    prod_stats = {"top1": 0, "top5": 0, "top10": 0, "top25": 0,
                  "entity_detected": 0, "entity_correct": 0,
                  "exact_match": 0, "not_found": 0}

    # Track component contribution
    component_stats = {
        "tfi_resolved": 0, "tfi_exact": 0, "tfi_family": 0,
        "faiss_top1": 0, "boost_helped": 0, "boost_hurt": 0,
    }

    t0 = time.time()
    prod_failures = []
    with patch("services.llm_reranker.rerank_with_llm", side_effect=mock_rerank):
        for i, (query, expected) in enumerate(prod_queries):
            if i % 20 == 0:
                print(f"  [{i}/{len(prod_queries)}]...", end="\r")

            response = await search.search(query, top_k=25)
            result_ids = [r.tool_id.lower() for r in response.results]
            exp_lower = expected.lower()

            # Entity detection analysis
            if response.detected_entity:
                prod_stats["entity_detected"] += 1
                # Check if entity is correct
                exp_parts = expected.split("_")
                exp_entity = exp_parts[1].lower() if len(exp_parts) >= 2 else ""
                if response.detected_entity.lower() == exp_entity.lower():
                    prod_stats["entity_correct"] += 1

            pos = None
            for j, rid in enumerate(result_ids):
                if rid == exp_lower:
                    pos = j
                    break

            if pos is not None:
                if pos == 0:
                    prod_stats["top1"] += 1
                if pos < 5:
                    prod_stats["top5"] += 1
                if pos < 10:
                    prod_stats["top10"] += 1
                if pos < 25:
                    prod_stats["top25"] += 1
            else:
                prod_stats["not_found"] += 1
                if len(prod_failures) < 20:  # Capture first 20 failures
                    prod_failures.append({
                        "query": query,
                        "expected": expected,
                        "entity": response.detected_entity,
                        "top3": [r.tool_id for r in response.results[:3]],
                    })

    prod_time = time.time() - t0
    m = len(prod_queries)

    print(f"\n  Results ({prod_time:.1f}s, {prod_time/m*1000:.0f}ms/query):")
    print(f"  {'Metric':<25} {'Count':<10} {'Accuracy':<10}")
    print(f"  {'-'*45}")
    print(f"  {'Top-1':<25} {prod_stats['top1']}/{m}      {100*prod_stats['top1']/m:.1f}%")
    print(f"  {'Top-5':<25} {prod_stats['top5']}/{m}      {100*prod_stats['top5']/m:.1f}%")
    print(f"  {'Top-10':<25} {prod_stats['top10']}/{m}      {100*prod_stats['top10']/m:.1f}%")
    print(f"  {'Top-25':<25} {prod_stats['top25']}/{m}      {100*prod_stats['top25']/m:.1f}%")
    print(f"  {'Entity detected':<25} {prod_stats['entity_detected']}/{m}      {100*prod_stats['entity_detected']/m:.1f}%")
    print(f"  {'Entity correct':<25} {prod_stats['entity_correct']}/{m}      {100*prod_stats['entity_correct']/m:.1f}%")
    print(f"  {'Not in top-25':<25} {prod_stats['not_found']}/{m}      {100*prod_stats['not_found']/m:.1f}%")

    # =========================================================================
    # FAILURE ANALYSIS
    # =========================================================================
    if prod_failures:
        print(f"\n{'='*80}")
        print(f"  SAMPLE FAILURES (first {len(prod_failures)} production misses)")
        print(f"{'='*80}")
        for f in prod_failures[:10]:
            print(f"  Q: {f['query'][:60]}")
            print(f"     Expected: {f['expected']}")
            print(f"     Entity:   {f['entity']}")
            print(f"     Top-3:    {f['top3']}")
            print()

    # =========================================================================
    # FINAL VERDICT
    # =========================================================================
    print(f"\n{'='*80}")
    print(f"  FINAL VERDICT — Full Pipeline (no LLM reranker, no HyDE)")
    print(f"{'='*80}")
    print(f"  {'Benchmark':<20} {'Top-1':<10} {'Top-5':<10} {'Top-10':<10} {'Top-25':<10}")
    print(f"  {'-'*60}")
    print(f"  {'Adversarial':<20} {100*adv_stats['top1']/n:.1f}%     {100*adv_stats['top5']/n:.1f}%     {100*adv_stats['top10']/n:.1f}%     {100*adv_stats['top25']/n:.1f}%")
    print(f"  {'Production':<20} {100*prod_stats['top1']/m:.1f}%     {100*prod_stats['top5']/m:.1f}%     {100*prod_stats['top10']/m:.1f}%     {100*prod_stats['top25']/m:.1f}%")
    print()

    # Interpretation
    prod_top25_pct = 100 * prod_stats["top25"] / m
    adv_top25_pct = 100 * adv_stats["top25"] / n

    if prod_top25_pct >= 70:
        print(f"  PRODUCTION Top-25 = {prod_top25_pct:.1f}% — LLM router should handle the rest.")
        print(f"  System is viable. Focus on improving Top-1 for latency.")
    elif prod_top25_pct >= 50:
        print(f"  PRODUCTION Top-25 = {prod_top25_pct:.1f}% — Marginal. Needs improvement.")
        print(f"  Dual index (example queries) and entity penalty fix recommended.")
    else:
        print(f"  PRODUCTION Top-25 = {prod_top25_pct:.1f}% — Fundamental retrieval problem.")
        print(f"  Consider: embedding model upgrade, example-query index, or architecture change.")

    print(f"\n  ADVERSARIAL Top-25 = {adv_top25_pct:.1f}%")
    print(f"  (Old FAISS-only with 3-large: Top-1=34.1%, Top-5=57.4%)")
    print(f"{'='*80}")


if __name__ == "__main__":
    asyncio.run(run_benchmark())
