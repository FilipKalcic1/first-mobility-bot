"""Forensic probe: for each rank=-1 miss, find target's actual FAISS rank in full pool.

Read-only. Does not mutate state.

Answers: pool-widening suffices (rank < 200) vs. query expansion needed (rank >> 200)?
"""
from __future__ import annotations

import asyncio
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

RESULTS = ROOT / "tests" / "benchmarks" / "last_run_results.json"


async def main() -> int:
    from services.registry import ToolRegistry
    from services.unified_search import initialize_unified_search
    from services.faiss_vector_store import get_faiss_store
    from services.entity_detector import detect_entity

    print("Bootstrapping...")
    registry = ToolRegistry()
    await registry.initialize(swagger_sources=[])
    await initialize_unified_search(registry=registry)
    faiss = get_faiss_store()
    pool_size = len(faiss._tool_ids)
    print(f"  Ready: {pool_size} tools in FAISS index\n")

    data = json.loads(RESULTS.read_text(encoding="utf-8"))
    neg1 = [m for m in data["misses"] if m.get("expected_rank", -1) == -1]
    print(f"rank=-1 misses: {len(neg1)}\n")

    random.seed(0)
    sample = random.sample(neg1, 5)

    print(f"{'#':<3}{'target':<55}{'FAISS rank':<12}{'score':<10}{'entity':<15}")
    print("-" * 100)

    for i, m in enumerate(sample, 1):
        query = m["query"]
        target = m["expected"]
        entity = detect_entity(query) or "-"

        results = await faiss.search(query, top_k=pool_size)
        rank = next((idx + 1 for idx, r in enumerate(results) if r.tool_id == target), -1)
        score = next((r.score for r in results if r.tool_id == target), 0.0)

        print(f"{i:<3}{target[:53]:<55}{rank:<12}{score:<10.4f}{entity:<15}")
        print(f"   query: {query}")
        top1 = results[0] if results else None
        if top1:
            print(f"   top1:  {top1.tool_id}  ({top1.score:.4f})")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
