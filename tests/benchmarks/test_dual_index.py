"""
Dual Index Benchmark — measures accuracy improvement from
adding example-query FAISS index alongside tool-description index.

Approach:
1. Load both indexes (tool descriptions + example queries)
2. For each test query, search both indexes
3. Merge results: max score per tool_id, weighted
4. Compare: tool-only vs dual vs production pipeline
"""
import json
import sys
import os
import asyncio
import random
import time
import numpy as np

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

os.environ["HYDE_ENABLED"] = "false"


async def run_benchmark():
    import faiss

    # Load tool documentation
    with open("config/tool_documentation.json", "r", encoding="utf-8") as f:
        docs = json.load(f)

    # Load tool embeddings (ada-002, 1536-dim)
    with open(".cache/tool_embeddings.json", "r", encoding="utf-8") as f:
        tool_embs = json.load(f)

    # Load example query embeddings (ada-002, 1536-dim)
    with open(".cache/example_query_embeddings_ada002.json", "r", encoding="utf-8") as f:
        eq_data = json.load(f)

    dim = 1536
    eq_queries = eq_data["queries"]
    eq_tool_ids = eq_data["tool_ids"]
    eq_embeddings = eq_data["embeddings"]

    print(f"Tool index: {len(tool_embs)} tools, dim={dim}")
    print(f"Example query index: {len(eq_embeddings)} queries, dim={dim}")

    # Build tool FAISS index
    tool_ids_list = list(tool_embs.keys())
    tool_matrix = np.array([tool_embs[tid] for tid in tool_ids_list], dtype=np.float32)
    faiss.normalize_L2(tool_matrix)
    tool_index = faiss.IndexFlatIP(dim)
    tool_index.add(tool_matrix)

    # Build example query FAISS index
    eq_matrix = np.array(eq_embeddings, dtype=np.float32)
    faiss.normalize_L2(eq_matrix)
    eq_index = faiss.IndexFlatIP(dim)
    eq_index.add(eq_matrix)

    # Get query embedding function
    from services.faiss_vector_store import FAISSVectorStore
    store = FAISSVectorStore()
    store._openai_client = None  # Will be initialized on first call

    # =========================================================================
    # Load adversarial test set
    # =========================================================================
    with open("tests/benchmarks/tool_recognition_paraphrases.seed99.json", "r", encoding="utf-8") as f:
        bench = json.load(f)

    adv_queries = []
    for te in bench["tools"]:
        for q in te["paraphrases"]:
            adv_queries.append((q, te["tool_id"]))

    # =========================================================================
    # Load production test set
    # =========================================================================
    random.seed(42)
    sampled = random.sample(list(docs.keys()), min(200, len(docs)))

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

    # =========================================================================
    # Run benchmarks
    # =========================================================================
    def measure_accuracy(result_ids, expected, top_ks=(1, 5, 10, 25)):
        exp_lower = expected.lower()
        results = {}
        for k in top_ks:
            results[f"top{k}"] = exp_lower in [r.lower() for r in result_ids[:k]]
        return results

    async def search_dual(query, top_k=25, tool_weight=0.4, eq_weight=0.6):
        """Search both indexes and merge results."""
        query_emb = await store._get_query_embedding(query)
        if query_emb is None:
            return []

        qvec = np.array([query_emb], dtype=np.float32)
        faiss.normalize_L2(qvec)

        # Search tool index
        tool_distances, tool_indices = tool_index.search(qvec, min(top_k * 3, len(tool_ids_list)))
        tool_scores = {}
        for dist, idx in zip(tool_distances[0], tool_indices[0]):
            if idx >= 0:
                tid = tool_ids_list[idx]
                tool_scores[tid] = float(dist) * tool_weight

        # Search example query index
        eq_distances, eq_indices = eq_index.search(qvec, min(top_k * 5, len(eq_queries)))
        eq_scores = {}
        for dist, idx in zip(eq_distances[0], eq_indices[0]):
            if idx >= 0:
                tid = eq_tool_ids[idx]
                score = float(dist) * eq_weight
                if tid not in eq_scores or score > eq_scores[tid]:
                    eq_scores[tid] = score

        # Merge: max per tool_id
        merged = {}
        for tid in set(list(tool_scores.keys()) + list(eq_scores.keys())):
            ts = tool_scores.get(tid, 0)
            es = eq_scores.get(tid, 0)
            merged[tid] = max(ts, es)

        # Sort by score
        sorted_tools = sorted(merged.items(), key=lambda x: x[1], reverse=True)
        return [tid for tid, _ in sorted_tools[:top_k]]

    async def search_tool_only(query, top_k=25):
        """Search tool index only."""
        query_emb = await store._get_query_embedding(query)
        if query_emb is None:
            return []

        qvec = np.array([query_emb], dtype=np.float32)
        faiss.normalize_L2(qvec)

        distances, indices = tool_index.search(qvec, top_k)
        results = []
        for dist, idx in zip(distances[0], indices[0]):
            if idx >= 0:
                results.append(tool_ids_list[idx])
        return results

    async def search_eq_only(query, top_k=25):
        """Search example query index only."""
        query_emb = await store._get_query_embedding(query)
        if query_emb is None:
            return []

        qvec = np.array([query_emb], dtype=np.float32)
        faiss.normalize_L2(qvec)

        distances, indices = eq_index.search(qvec, min(top_k * 5, len(eq_queries)))
        seen = {}
        for dist, idx in zip(distances[0], indices[0]):
            if idx >= 0:
                tid = eq_tool_ids[idx]
                if tid not in seen:
                    seen[tid] = float(dist)

        sorted_tools = sorted(seen.items(), key=lambda x: x[1], reverse=True)
        return [tid for tid, _ in sorted_tools[:top_k]]

    # =========================================================================
    # Adversarial benchmark
    # =========================================================================
    print(f"\n{'='*80}")
    print(f"  ADVERSARIAL BENCHMARK ({len(adv_queries)} queries)")
    print(f"{'='*80}")

    configs = [
        ("Tool-only", search_tool_only),
        ("Example-only", search_eq_only),
        ("Dual (0.4/0.6)", lambda q, top_k=25: search_dual(q, top_k, 0.4, 0.6)),
        ("Dual (0.3/0.7)", lambda q, top_k=25: search_dual(q, top_k, 0.3, 0.7)),
        ("Dual (0.5/0.5)", lambda q, top_k=25: search_dual(q, top_k, 0.5, 0.5)),
    ]

    for config_name, search_fn in configs:
        stats = {f"top{k}": 0 for k in (1, 5, 10, 25)}
        t0 = time.time()

        for i, (query, expected) in enumerate(adv_queries):
            if i % 40 == 0:
                print(f"  [{config_name}] [{i}/{len(adv_queries)}]...", end="\r")

            result_ids = await search_fn(query, top_k=25)
            acc = measure_accuracy(result_ids, expected)
            for k, v in acc.items():
                if v:
                    stats[k] += 1

        elapsed = time.time() - t0
        n = len(adv_queries)
        print(f"  {config_name:<20} Top-1: {stats['top1']}/{n} ({100*stats['top1']/n:.1f}%)  "
              f"Top-5: {stats['top5']}/{n} ({100*stats['top5']/n:.1f}%)  "
              f"Top-10: {stats['top10']}/{n} ({100*stats['top10']/n:.1f}%)  "
              f"Top-25: {stats['top25']}/{n} ({100*stats['top25']/n:.1f}%)  "
              f"[{elapsed:.1f}s]")

    # =========================================================================
    # Production benchmark
    # =========================================================================
    print(f"\n{'='*80}")
    print(f"  PRODUCTION BENCHMARK ({len(prod_queries)} queries)")
    print(f"{'='*80}")

    for config_name, search_fn in configs:
        stats = {f"top{k}": 0 for k in (1, 5, 10, 25)}
        t0 = time.time()

        for i, (query, expected) in enumerate(prod_queries):
            if i % 40 == 0:
                print(f"  [{config_name}] [{i}/{len(prod_queries)}]...", end="\r")

            result_ids = await search_fn(query, top_k=25)
            acc = measure_accuracy(result_ids, expected)
            for k, v in acc.items():
                if v:
                    stats[k] += 1

        elapsed = time.time() - t0
        m = len(prod_queries)
        print(f"  {config_name:<20} Top-1: {stats['top1']}/{m} ({100*stats['top1']/m:.1f}%)  "
              f"Top-5: {stats['top5']}/{m} ({100*stats['top5']/m:.1f}%)  "
              f"Top-10: {stats['top10']}/{m} ({100*stats['top10']/m:.1f}%)  "
              f"Top-25: {stats['top25']}/{m} ({100*stats['top25']/m:.1f}%)  "
              f"[{elapsed:.1f}s]")

    print(f"\n{'='*80}")


if __name__ == "__main__":
    asyncio.run(run_benchmark())
