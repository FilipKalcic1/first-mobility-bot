"""
Diagnose FAISS raw ranks for benchmark paraphrases.
For each query, find where the correct tool ranks in raw FAISS results (all 950).
This tells us the theoretical ceiling — no amount of post-processing can bring
a tool into top-20 if FAISS ranks it at position 500.
"""
import asyncio
import io
import json
import sys
import time
from pathlib import Path
from collections import Counter

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))


async def main():
    para_path = Path(__file__).parent / "tool_recognition_paraphrases.json"
    with open(para_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    doc_path = project_root / "config" / "tool_documentation.json"
    with open(doc_path, "r", encoding="utf-8") as f:
        tool_docs = json.load(f)

    print("Initializing FAISSVectorStore...", flush=True)
    from services.faiss_vector_store import FAISSVectorStore
    store = FAISSVectorStore()
    await store.initialize(tool_documentation=tool_docs)
    print(f"  {store._index.ntotal} tools indexed", flush=True)

    ranks = []  # (tool_id, paraphrase, rank, score, top1_tool, top1_score)
    rank_buckets = Counter()  # bucket -> count

    total = 0
    for tool_entry in data["tools"]:
        expected_tool = tool_entry["tool_id"]
        for paraphrase in tool_entry["paraphrases"]:
            total += 1

            # Search ALL 950 tools
            results = await store.search(paraphrase, top_k=950)
            tool_ids = [r.tool_id for r in results]

            if expected_tool in tool_ids:
                rank = tool_ids.index(expected_tool) + 1
                score = results[tool_ids.index(expected_tool)].score
            else:
                rank = 999
                score = 0.0

            top1_tool = tool_ids[0] if tool_ids else "NONE"
            top1_score = results[0].score if results else 0.0

            ranks.append({
                "tool_id": expected_tool,
                "query": paraphrase[:80],
                "rank": rank,
                "score": round(score, 4),
                "top1_tool": top1_tool,
                "top1_score": round(top1_score, 4),
            })

            # Bucket
            if rank <= 1:
                rank_buckets["1"] += 1
            elif rank <= 5:
                rank_buckets["2-5"] += 1
            elif rank <= 20:
                rank_buckets["6-20"] += 1
            elif rank <= 50:
                rank_buckets["21-50"] += 1
            elif rank <= 100:
                rank_buckets["51-100"] += 1
            elif rank <= 200:
                rank_buckets["101-200"] += 1
            else:
                rank_buckets["201+"] += 1

            if total % 30 == 0:
                print(f"  [{total}/290]", flush=True)

    print(f"\n{'='*70}")
    print(f"RAW FAISS RANK DISTRIBUTION (before any boosts/diversity)")
    print(f"{'='*70}")
    n = len(ranks)
    cumulative = 0
    for bucket in ["1", "2-5", "6-20", "21-50", "51-100", "101-200", "201+"]:
        count = rank_buckets.get(bucket, 0)
        cumulative += count
        print(f"  Rank {bucket:>7s}: {count:3d} queries ({count/n:.1%})  cumulative: {cumulative/n:.1%}")

    # What's the theoretical ceiling?
    in_top5 = sum(1 for r in ranks if r["rank"] <= 5)
    in_top20 = sum(1 for r in ranks if r["rank"] <= 20)
    in_top50 = sum(1 for r in ranks if r["rank"] <= 50)
    in_top100 = sum(1 for r in ranks if r["rank"] <= 100)

    print(f"\n  THEORETICAL CEILINGS (raw FAISS, no boosts):")
    print(f"  Top-5:   {in_top5}/{n} = {in_top5/n:.1%}  (absolute max with perfect reranking within top-5)")
    print(f"  Top-20:  {in_top20}/{n} = {in_top20/n:.1%}  (max achievable with any boost/diversity)")
    print(f"  Top-50:  {in_top50}/{n} = {in_top50/n:.1%}")
    print(f"  Top-100: {in_top100}/{n} = {in_top100/n:.1%}")

    # Entity analysis: same-entity vs different-entity confusion
    same_entity_count = 0
    diff_entity_count = 0
    for r in ranks:
        if r["rank"] > 5:
            expected_entity = r["tool_id"].split("_")[1].lower() if len(r["tool_id"].split("_")) >= 2 else ""
            top1_entity = r["top1_tool"].split("_")[1].lower() if len(r["top1_tool"].split("_")) >= 2 else ""
            if expected_entity == top1_entity:
                same_entity_count += 1
            else:
                diff_entity_count += 1

    print(f"\n  For queries NOT in top-5 ({n - in_top5}):")
    print(f"    Same entity in top-1: {same_entity_count} (right entity, wrong tool)")
    print(f"    Diff entity in top-1: {diff_entity_count} (wrong entity entirely)")

    # Score gap analysis
    score_gaps = []
    for r in ranks:
        if r["rank"] > 1:
            gap = r["top1_score"] - r["score"]
            score_gaps.append(gap)

    if score_gaps:
        import numpy as np
        gaps = np.array(score_gaps)
        print(f"\n  Score gap (top-1 score - correct tool score) for non-top-1 queries:")
        print(f"    Mean: {gaps.mean():.4f}")
        print(f"    Median: {np.median(gaps):.4f}")
        print(f"    P25: {np.percentile(gaps, 25):.4f}")
        print(f"    P75: {np.percentile(gaps, 75):.4f}")
        print(f"    P90: {np.percentile(gaps, 90):.4f}")

    # Save full data
    out_path = Path(__file__).parent / "faiss_rank_diagnosis.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"ranks": ranks, "buckets": dict(rank_buckets)}, f, indent=2, ensure_ascii=False)
    print(f"\nSaved to {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
