"""
Benchmark: raw FAISS search only, no boost engine, no BM25, no entity.
Isolates the FAISS search quality from the UnifiedSearch pipeline.
"""
import asyncio
import io
import json
import sys
import time
from pathlib import Path

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

    top1 = top5 = top20 = 0
    total = 0

    for tool_entry in data["tools"]:
        expected_tool = tool_entry["tool_id"]
        for paraphrase in tool_entry["paraphrases"]:
            total += 1
            results = await store.search(paraphrase, top_k=20)
            ids = [r.tool_id for r in results]

            if expected_tool in ids[:1]: top1 += 1
            if expected_tool in ids[:5]: top5 += 1
            if expected_tool in ids[:20]: top20 += 1

            if total % 50 == 0:
                print(f"  [{total}/290] Top-1={top1}/{total} Top-5={top5}/{total} Top-20={top20}/{total}", flush=True)

    print(f"\n{'='*60}")
    print(f"RAW FAISS V6.0 (no boost, no BM25, no entity)")
    print(f"{'='*60}")
    print(f"Top-1:  {top1}/{total} = {top1/total:.1%}")
    print(f"Top-5:  {top5}/{total} = {top5/total:.1%}")
    print(f"Top-20: {top20}/{total} = {top20/total:.1%}")

    # Also test with top_k=100
    print(f"\nWith top_k=100:")
    top5_100 = top20_100 = 0
    total2 = 0
    for tool_entry in data["tools"]:
        expected_tool = tool_entry["tool_id"]
        for paraphrase in tool_entry["paraphrases"]:
            total2 += 1
            results = await store.search(paraphrase, top_k=100)
            ids = [r.tool_id for r in results]
            if expected_tool in ids[:5]: top5_100 += 1
            if expected_tool in ids[:20]: top20_100 += 1
    print(f"Top-5:  {top5_100}/{total2} = {top5_100/total2:.1%}")
    print(f"Top-20: {top20_100}/{total2} = {top20_100/total2:.1%}")


if __name__ == "__main__":
    asyncio.run(main())
