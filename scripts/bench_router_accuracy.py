"""FAZA 7D — Router accuracy benchmark (anchor retrieval stage only).

Loads `tests/benchmarks/objective_benchmark_100.json` (58 queries with
ground-truth `expected_tool`), embeds each query via Azure ada-002,
runs cosine over all anchors in a given tool_data file, reports:

  precision@1: how often expected_tool is the top-1 anchor match
  recall@50:   how often expected_tool is in the top-50 (LLM input set)

Pre/post comparison: run twice with --tool-data flag pointing at
different versions of tool_data.json (e.g., bak file vs current).

Usage:
  # Single run
  python scripts/bench_router_accuracy.py
      [--tool-data config/tool_data.json]
      [--limit N]           # debug: process first N queries

  # Pre/post comparison
  python scripts/bench_router_accuracy.py --tool-data config/tool_data.json.bak-2026-05-17
  python scripts/bench_router_accuracy.py --tool-data config/tool_data.json
  # Compare numbers manually

Cost: ~$0.01 per run (anchors embedded once + queries embedded).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
DEFAULT_BENCH_FILE = REPO / "tests" / "benchmarks" / "objective_benchmark_100.json"
DEFAULT_TOOL_DATA = REPO / "config" / "tool_data.json"

INTERNAL_HELPER_RE = re.compile(
    r"_(Distinct[A-Z][A-Za-z0-9]*|GroupBy|ProjectTo|metadata|"
    r"DeleteByCriteria|multipatch)$"
)


def is_internal(op_id: str) -> bool:
    return bool(INTERNAL_HELPER_RE.search(op_id))


def method_of(tool_id: str, tool_data_tools: dict) -> str:
    """Returns HTTP method for a tool_id, falling back to op_id prefix."""
    tool = tool_data_tools.get(tool_id) or {}
    method = (tool.get("method") or "").upper()
    if method:
        return method
    # Fall back: parse from op_id prefix (e.g., 'get_X' → 'GET')
    prefix = tool_id.split("_", 1)[0].upper() if "_" in tool_id else ""
    return prefix if prefix in {"GET", "POST", "PUT", "PATCH", "DELETE"} else "UNKNOWN"


def cosine(a: list, b: list) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


async def embed_batch(embedder, deployment: str, texts: list) -> list:
    """Embed a batch of strings via Azure ada-002. Returns list of vectors."""
    if not texts:
        return []
    BATCH = 100  # Azure batch limit
    results: list = []
    for i in range(0, len(texts), BATCH):
        chunk = texts[i:i + BATCH]
        r = await embedder.embeddings.create(input=chunk, model=deployment)
        results.extend(list(d.embedding) for d in r.data)
    return results


async def build_anchor_index(
    embedder, deployment: str, tool_data: dict,
) -> dict:
    """Returns {tool_id: list[vector]} for all non-internal tools with
    at least one anchor."""
    tools = tool_data.get("tools", {})
    print(f"Building anchor index from {len(tools)} tools "
          f"(filtering internal helpers)...")

    # Flatten anchors into one big list with index mapping
    all_anchors: list = []
    anchor_to_tool: list = []  # parallel — index into all_anchors → tool_id
    for tid, t in tools.items():
        if is_internal(tid):
            continue
        if not isinstance(t, dict):
            continue
        anchors = t.get("anchors") or []
        for a in anchors:
            if isinstance(a, str) and a.strip():
                all_anchors.append(a)
                anchor_to_tool.append(tid)

    print(f"Embedding {len(all_anchors)} anchors via {deployment}...")
    vectors = await embed_batch(embedder, deployment, all_anchors)
    print(f"Embedded {len(vectors)} vectors.")

    # Group vectors back by tool_id
    by_tool: dict = {}
    for tid, vec in zip(anchor_to_tool, vectors):
        by_tool.setdefault(tid, []).append(vec)
    return by_tool


def top_k_tools(
    query_vec: list, anchors_by_tool: dict, k: int = 50,
) -> list:
    """For each tool, compute max-cosine over its anchors. Return top-K
    (tool_id, score) tuples sorted desc."""
    scores: list = []
    for tid, vecs in anchors_by_tool.items():
        best = -2.0
        for v in vecs:
            s = cosine(query_vec, v)
            if s > best:
                best = s
        scores.append((tid, best))
    scores.sort(key=lambda x: -x[1])
    return scores[:k]


def _print_segment_table(
    title: str, segments: dict, sort_by_n_desc: bool = True,
) -> None:
    """segments: {label: [(top1_correct, top10_correct, top50_correct, total)]}
    Renders a one-line-per-label table."""
    if not segments:
        return
    print(f"\n  --- {title} ---")
    print(f"    {'label':<14} {'n':>4} {'p@1':>10} {'p@10':>10} {'r@50':>10}")
    items = sorted(
        segments.items(),
        key=lambda kv: -kv[1][3] if sort_by_n_desc else kv[0],
    )
    for label, (t1, t10, t50, n) in items:
        if n == 0:
            continue
        p1 = 100 * t1 / n
        p10 = 100 * t10 / n
        r50 = 100 * t50 / n
        print(
            f"    {label:<14} {n:>4} "
            f"{t1:>3}/{n:<3}={p1:>4.1f}% "
            f"{t10:>3}/{n:<3}={p10:>4.1f}% "
            f"{t50:>3}/{n:<3}={r50:>4.1f}%"
        )


async def main_async(
    tool_data_path: Path, bench_path: Path, limit: int | None,
):
    sys.stdout.reconfigure(encoding="utf-8")

    if not tool_data_path.exists():
        print(f"ERROR: {tool_data_path} not found", file=sys.stderr)
        sys.exit(1)
    if not bench_path.exists():
        print(f"ERROR: {bench_path} not found", file=sys.stderr)
        sys.exit(1)

    # Load benchmark queries (with ground truth)
    bench = json.loads(bench_path.read_text(encoding="utf-8"))
    all_queries = bench.get("queries") or []
    queries = [q for q in all_queries if q.get("expected_tool")]
    print(f"Loaded {len(queries)} queries with expected_tool labels "
          f"from {bench_path.name}.")
    if limit:
        queries = queries[:limit]
        print(f"Limited to first {limit} queries.")

    # Load tool_data + embedding client
    tool_data = json.loads(tool_data_path.read_text(encoding="utf-8"))
    tools_dict = tool_data.get("tools", {})

    sys.path.insert(0, str(REPO))
    try:
        from services.openai_client import get_embedding_client
    except ImportError as e:
        print(f"Cannot import embedding client: {e}", file=sys.stderr)
        sys.exit(1)

    embedder = get_embedding_client()
    deployment = os.environ.get(
        "AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "text-embedding-ada-002",
    )

    # Build anchor index
    anchors_by_tool = await build_anchor_index(embedder, deployment, tool_data)

    # Embed all queries (batch for efficiency)
    query_texts = [q["query"] for q in queries]
    print(f"\nEmbedding {len(query_texts)} benchmark queries...")
    query_vecs = await embed_batch(embedder, deployment, query_texts)

    # Score each query + collect per-segment stats
    correct_top1 = 0
    correct_top10 = 0
    correct_top50 = 0
    missed: list = []
    # segments[label] = [top1_correct, top10_correct, top50_correct, total]
    by_method: dict = {}
    by_persona: dict = {}
    # per-tool: who got it / missed it (for top-20 worst report)
    per_tool_miss: dict = {}  # tool_id → miss_count
    per_tool_total: dict = {}  # tool_id → total_count
    for q, qv in zip(queries, query_vecs):
        expected = q["expected_tool"]
        persona = q.get("persona") or "unknown"
        method = method_of(expected, tools_dict)

        top = top_k_tools(qv, anchors_by_tool, k=50)
        top_ids = [tid for tid, _ in top]

        per_tool_total[expected] = per_tool_total.get(expected, 0) + 1
        by_method.setdefault(method, [0, 0, 0, 0])[3] += 1
        by_persona.setdefault(persona, [0, 0, 0, 0])[3] += 1

        if not top_ids:
            missed.append((q["query"], expected, "no_results"))
            per_tool_miss[expected] = per_tool_miss.get(expected, 0) + 1
            continue
        if top_ids[0] == expected:
            correct_top1 += 1
            by_method[method][0] += 1
            by_persona[persona][0] += 1
        if expected in top_ids[:10]:
            correct_top10 += 1
            by_method[method][1] += 1
            by_persona[persona][1] += 1
        if expected in top_ids:
            correct_top50 += 1
            by_method[method][2] += 1
            by_persona[persona][2] += 1
        else:
            missed.append((q["query"], expected, top_ids[:3]))
            per_tool_miss[expected] = per_tool_miss.get(expected, 0) + 1

    n = len(queries)
    print(f"\n=== Results for {bench_path.name} (anchors: {tool_data_path.name}) ===")
    print(f"  Total queries:     {n}")
    if n > 0:
        print(f"  precision@1:       {correct_top1}/{n} = {100 * correct_top1 / n:.1f}%")
        print(f"  precision@10:      {correct_top10}/{n} = {100 * correct_top10 / n:.1f}%")
        print(f"  recall@50:         {correct_top50}/{n} = {100 * correct_top50 / n:.1f}%")
        print(f"  Missed @50:        {len(missed)}")

    _print_segment_table("By HTTP method", by_method)
    _print_segment_table("By persona", by_persona)

    # Top-20 worst tools (highest miss rate among tools with >=1 miss)
    if per_tool_miss:
        print(f"\n  --- Top-20 worst tools (by miss count, recall@50) ---")
        items = sorted(
            per_tool_miss.items(),
            key=lambda kv: (-kv[1], kv[0]),
        )[:20]
        print(f"    {'tool_id':<55} {'miss':>5} {'total':>6}")
        for tid, miss in items:
            total = per_tool_total.get(tid, 0)
            print(f"    {tid:<55} {miss:>5} {total:>6}")

    if missed:
        print(f"\n  --- Sample missed queries (first 10) ---")
        for q, expected, top3 in missed[:10]:
            print(f"    '{q[:50]}' → expected {expected}, top3: {top3}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--tool-data", type=Path, default=DEFAULT_TOOL_DATA,
        help="Path to tool_data.json variant (e.g., backup for before/after)",
    )
    ap.add_argument(
        "--benchmark-file", type=Path, default=DEFAULT_BENCH_FILE,
        help=f"Path to benchmark JSON (default {DEFAULT_BENCH_FILE.name})",
    )
    ap.add_argument(
        "--limit", type=int, default=None,
        help="Process only first N queries (debug)",
    )
    args = ap.parse_args()
    asyncio.run(main_async(args.tool_data, args.benchmark_file, args.limit))


if __name__ == "__main__":
    main()
