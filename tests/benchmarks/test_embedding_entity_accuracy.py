"""
Test embedding-based entity classifier accuracy on benchmark paraphrases.
Uses FAISS entity centroids (pre-computed from ada-002 tool embeddings)
to classify entity from query embedding.
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


def extract_entity_from_tool_id(tool_id: str) -> str:
    parts = tool_id.split("_")
    if len(parts) >= 2:
        return parts[1].lower()
    return ""


async def main():
    para_path = Path(__file__).parent / "tool_recognition_paraphrases.json"
    with open(para_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Load tool documentation for FAISS init
    doc_path = project_root / "config" / "tool_documentation.json"
    with open(doc_path, "r", encoding="utf-8") as f:
        tool_docs = json.load(f)

    print("Initializing FAISSVectorStore...")
    t0 = time.time()

    from services.faiss_vector_store import FAISSVectorStore
    store = FAISSVectorStore()
    await store.initialize(tool_documentation=tool_docs)
    print(f"  Initialized in {time.time() - t0:.1f}s")
    print(f"  Entity centroids: {len(store._entity_centroids)}")

    total = 0
    correct = 0
    wrong = 0
    none_count = 0
    misses = []
    entity_stats = Counter()
    entity_totals = Counter()

    # Test different confidence thresholds
    threshold_correct = {t: 0 for t in [0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60]}
    threshold_wrong = {t: 0 for t in threshold_correct}
    threshold_none = {t: 0 for t in threshold_correct}

    all_scores = []  # (expected, predicted, score, query)

    for tool_entry in data["tools"]:
        tool_id = tool_entry["tool_id"]
        expected_entity = extract_entity_from_tool_id(tool_id)

        for paraphrase in tool_entry["paraphrases"]:
            total += 1
            entity_totals[expected_entity] += 1

            # Get query embedding
            embedding = await store.get_query_embedding(paraphrase)
            if embedding is None:
                none_count += 1
                for t in threshold_none:
                    threshold_none[t] += 1
                continue

            # Classify with default threshold (0.45)
            result, confidence = store.classify_entity_by_embedding(embedding)

            all_scores.append((expected_entity, result, confidence, paraphrase[:60]))

            # Check at all thresholds
            for t in threshold_correct:
                _, t_conf = store.classify_entity_by_embedding(embedding, min_confidence=t)
                r, _ = store.classify_entity_by_embedding(embedding, min_confidence=t)
                if r is None:
                    threshold_none[t] += 1
                elif r == expected_entity:
                    threshold_correct[t] += 1
                else:
                    threshold_wrong[t] += 1

            if result is None:
                none_count += 1
            elif result == expected_entity:
                correct += 1
                entity_stats[expected_entity] += 1
            else:
                wrong += 1
                misses.append({
                    "tool_id": tool_id,
                    "expected": expected_entity,
                    "predicted": result,
                    "confidence": round(confidence, 3),
                    "query": paraphrase[:80],
                })

            if total % 30 == 0:
                print(f"  [{total}/290] correct={correct} wrong={wrong} none={none_count}", flush=True)

    print(f"\n{'='*70}")
    print(f"EMBEDDING ENTITY CLASSIFIER ACCURACY ON BENCHMARK PARAPHRASES")
    print(f"{'='*70}")
    print(f"Total:          {total}")
    print(f"Correct:        {correct}/{total} = {correct/total:.1%}")
    print(f"Wrong entity:   {wrong}/{total} = {wrong/total:.1%}")
    print(f"No prediction:  {none_count}/{total} = {none_count/total:.1%}")
    print(f"\nFor comparison: LLM=~35%, TF-IDF=2.1%")

    # Threshold analysis
    print(f"\n{'='*70}")
    print(f"THRESHOLD ANALYSIS")
    print(f"{'='*70}")
    for t in sorted(threshold_correct.keys()):
        c = threshold_correct[t]
        w = threshold_wrong[t]
        n = threshold_none[t]
        acc = c / total if total else 0
        print(f"  threshold={t:.2f}: correct={c} ({c/total:.1%})  wrong={w} ({w/total:.1%})  none={n} ({n/total:.1%})")

    # Per-entity breakdown (top and bottom)
    print(f"\n{'='*70}")
    print(f"PER-ENTITY BREAKDOWN")
    print(f"{'='*70}")
    for entity in sorted(entity_totals.keys()):
        t = entity_totals[entity]
        c = entity_stats.get(entity, 0)
        print(f"  {entity:35s}  {c}/{t} = {c/t:.0%}")

    # Sample misses
    print(f"\n{'='*70}")
    print(f"SAMPLE MISSES (first 20)")
    print(f"{'='*70}")
    for m in misses[:20]:
        print(f"  exp={m['expected']:20s} pred={m['predicted']:20s} conf={m['confidence']:.3f} | {m['query']}")

    # Save
    out_path = Path(__file__).parent / "embedding_entity_accuracy.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "total": total,
            "correct": correct,
            "wrong": wrong,
            "none": none_count,
            "accuracy": correct / total if total else 0,
            "thresholds": {str(t): {"correct": threshold_correct[t], "wrong": threshold_wrong[t], "none": threshold_none[t]} for t in threshold_correct},
            "misses": misses,
        }, f, indent=2, ensure_ascii=False)
    print(f"\nSaved to {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
