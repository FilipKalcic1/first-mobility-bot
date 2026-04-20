"""
Combined TFI+FAISS benchmark.
Simulates the actual production pipeline: TFI first, FAISS fallback.
Measures end-to-end accuracy on all test sets.
"""
import json, sys, os, asyncio, random, time
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.entity_detector import detect_entity
from services.tool_family_index import ToolFamilyIndex
from services.types.query_type import QueryType
from services.text_normalizer import normalize_diacritics
from services.config_loader import load_json
from services.faiss_vector_store import FAISSVectorStore

_VERB = load_json("domain", "http_verbs_hr.json")["verb_to_method"]
_SFX = load_json("per_tool", "suffix_boosts.json")["by_suffix"]


def detect_method(q):
    n = normalize_diacritics(q.lower())
    for v, m in _VERB.items():
        if v in n:
            return m
    return None


def detect_qt(q, method):
    n = normalize_diacritics(q.lower())
    if method == "delete":
        if any(s in n for s in ["kriterij", "uvjet", "po filtr", "bulk", "masovno"]):
            return QueryType.DELETE_CRITERIA
    sm = {
        "_agg": QueryType.AGGREGATION, "_groupby": QueryType.AGGREGATION,
        "_projectto": QueryType.PROJECTION, "_multipatch": QueryType.BULK_UPDATE,
    }
    for k, d in _SFX.items():
        if any(s in n for s in d["keywords"]):
            m = sm.get(k)
            if m:
                return m
    return QueryType.LIST


async def main():
    # Build TFI
    with open("config/tool_documentation.json", "r", encoding="utf-8") as f:
        docs = json.load(f)
    tfi = ToolFamilyIndex()
    tfi.build(list(docs.keys()))

    # Build FAISS
    store = FAISSVectorStore()
    cached = store._load_cached_embeddings()
    store._embeddings = cached
    store._tool_ids = list(cached.keys())
    store._tool_methods = {}
    for tid in store._tool_ids:
        parts = tid.split("_")
        store._tool_methods[tid] = parts[0].upper() if parts else "GET"
    store._build_index()
    store._initialized = True
    print(f"FAISS index: {len(store._tool_ids)} tools, dim=1536")

    # =========================================================================
    # TEST SET 1: Adversarial benchmark (seed=99)
    # =========================================================================
    with open("tests/benchmarks/tool_recognition_paraphrases.seed99.json", "r", encoding="utf-8") as f:
        bench = json.load(f)

    queries_adv = []
    for te in bench["tools"]:
        for q in te["paraphrases"]:
            queries_adv.append((q, te["tool_id"]))

    print(f"\nRunning adversarial benchmark ({len(queries_adv)} queries)...")

    adv_tfi_exact = 0
    adv_faiss_exact = 0
    adv_combined_top1 = 0
    adv_combined_top5 = 0
    adv_combined_top10 = 0
    adv_tfi_used = 0

    for i, (query, expected) in enumerate(queries_adv):
        if i % 20 == 0:
            print(f"  [{i}/{len(queries_adv)}]...", end="\r")

        # TFI attempt
        ent = detect_entity(query)
        meth = detect_method(query)
        qt = detect_qt(query, meth) if ent else QueryType.UNKNOWN
        tfi_tool = tfi.resolve(ent, qt, method=meth) if ent else None
        tfi_family = tfi.get_family_tools(ent) if ent else {}

        # FAISS search
        faiss_results = await store.search(query, top_k=20)

        # Combined: TFI candidates + FAISS candidates, TFI tool gets priority
        combined = []
        seen = set()

        # TFI tool first (highest priority)
        if tfi_tool:
            combined.append(tfi_tool.lower())
            seen.add(tfi_tool.lower())
            adv_tfi_used += 1

        # TFI family next
        for variant, tid in tfi_family.items():
            if tid.lower() not in seen:
                combined.append(tid.lower())
                seen.add(tid.lower())

        # FAISS results fill the rest
        for r in faiss_results:
            if r.tool_id.lower() not in seen:
                combined.append(r.tool_id.lower())
                seen.add(r.tool_id.lower())

        exp_lower = expected.lower()

        # TFI-only accuracy
        if tfi_tool and tfi_tool.lower() == exp_lower:
            adv_tfi_exact += 1

        # FAISS-only Top-1
        if faiss_results and faiss_results[0].tool_id.lower() == exp_lower:
            adv_faiss_exact += 1

        # Combined accuracy
        if combined and combined[0] == exp_lower:
            adv_combined_top1 += 1
        if exp_lower in combined[:5]:
            adv_combined_top5 += 1
        if exp_lower in combined[:10]:
            adv_combined_top10 += 1

    n = len(queries_adv)
    print(f"\n{'='*76}")
    print(f"  ADVERSARIAL BENCHMARK (seed=99, {n} queries)")
    print(f"{'='*76}")
    print(f"  {'Metric':<30} {'TFI Only':<15} {'FAISS Only':<15} {'Combined':<15}")
    print(f"  {'-'*70}")
    print(f"  {'Top-1':<30} {adv_tfi_exact}/{n} ({100*adv_tfi_exact/n:.1f}%)   {adv_faiss_exact}/{n} ({100*adv_faiss_exact/n:.1f}%)   {adv_combined_top1}/{n} ({100*adv_combined_top1/n:.1f}%)")
    print(f"  {'Top-5':<30} {'N/A':<15} {'N/A':<15} {adv_combined_top5}/{n} ({100*adv_combined_top5/n:.1f}%)")
    print(f"  {'Top-10':<30} {'N/A':<15} {'N/A':<15} {adv_combined_top10}/{n} ({100*adv_combined_top10/n:.1f}%)")
    print(f"  {'TFI resolved':<30} {adv_tfi_used}/{n} ({100*adv_tfi_used/n:.1f}%)")

    # =========================================================================
    # TEST SET 2: example_queries (primary tools only)
    # =========================================================================
    random.seed(42)
    sampled = random.sample(list(docs.keys()), min(200, len(docs)))

    # Filter to primary tools only
    def is_primary(tid):
        p = tid.split("_")
        if len(p) >= 4 and any(x in ("documents","documentId","thumb","metadata") for x in p):
            return False
        if "Lookup" in tid:
            return False
        return True

    queries_prod = []
    for tid in sampled:
        if not is_primary(tid):
            continue
        doc = docs[tid]
        qs = doc.get("example_queries_hr", doc.get("example_queries", []))
        for q in qs[:2]:
            queries_prod.append((q, tid))

    print(f"\nRunning production benchmark ({len(queries_prod)} primary-tool queries)...")

    prod_tfi_exact = 0
    prod_faiss_exact = 0
    prod_combined_top1 = 0
    prod_combined_top5 = 0
    prod_combined_top10 = 0
    prod_tfi_used = 0
    prod_tfi_family = 0

    for i, (query, expected) in enumerate(queries_prod):
        if i % 20 == 0:
            print(f"  [{i}/{len(queries_prod)}]...", end="\r")

        ent = detect_entity(query)
        meth = detect_method(query)
        qt = detect_qt(query, meth) if ent else QueryType.UNKNOWN
        tfi_tool = tfi.resolve(ent, qt, method=meth) if ent else None
        tfi_fam = tfi.get_family_tools(ent) if ent else {}

        faiss_results = await store.search(query, top_k=20)

        combined = []
        seen = set()
        if tfi_tool:
            combined.append(tfi_tool.lower())
            seen.add(tfi_tool.lower())
            adv_tfi_used += 1
        for variant, tid in tfi_fam.items():
            if tid.lower() not in seen:
                combined.append(tid.lower())
                seen.add(tid.lower())
        for r in faiss_results:
            if r.tool_id.lower() not in seen:
                combined.append(r.tool_id.lower())
                seen.add(r.tool_id.lower())

        exp_lower = expected.lower()

        if tfi_tool and tfi_tool.lower() == exp_lower:
            prod_tfi_exact += 1
        if tfi_tool:
            prod_tfi_used += 1
            eparts = expected.split("_")
            eent = eparts[1].lower() if len(eparts) >= 2 else ""
            rparts = tfi_tool.split("_")
            rent = rparts[1].lower() if len(rparts) >= 2 else ""
            if rent == eent:
                prod_tfi_family += 1
        if faiss_results and faiss_results[0].tool_id.lower() == exp_lower:
            prod_faiss_exact += 1
        if combined and combined[0] == exp_lower:
            prod_combined_top1 += 1
        if exp_lower in combined[:5]:
            prod_combined_top5 += 1
        if exp_lower in combined[:10]:
            prod_combined_top10 += 1

    m = len(queries_prod)
    print(f"\n{'='*76}")
    print(f"  PRODUCTION BENCHMARK (primary tools, {m} queries)")
    print(f"{'='*76}")
    print(f"  {'Metric':<30} {'TFI Only':<15} {'FAISS Only':<15} {'Combined':<15}")
    print(f"  {'-'*70}")
    print(f"  {'Top-1':<30} {prod_tfi_exact}/{m} ({100*prod_tfi_exact/m:.1f}%)   {prod_faiss_exact}/{m} ({100*prod_faiss_exact/m:.1f}%)   {prod_combined_top1}/{m} ({100*prod_combined_top1/m:.1f}%)")
    print(f"  {'Top-5':<30} {'N/A':<15} {'N/A':<15} {prod_combined_top5}/{m} ({100*prod_combined_top5/m:.1f}%)")
    print(f"  {'Top-10':<30} {'N/A':<15} {'N/A':<15} {prod_combined_top10}/{m} ({100*prod_combined_top10/m:.1f}%)")
    print(f"  {'TFI resolved':<30} {prod_tfi_used}/{m} ({100*prod_tfi_used/m:.1f}%)")
    print(f"  {'TFI right family':<30} {prod_tfi_family}/{m} ({100*prod_tfi_family/m:.1f}%)")

    # Final summary
    print(f"\n{'='*76}")
    print(f"  FINAL VERDICT")
    print(f"{'='*76}")
    print(f"  Combined system (TFI+FAISS) on adversarial:")
    print(f"    Top-1: {100*adv_combined_top1/n:.1f}%  Top-5: {100*adv_combined_top5/n:.1f}%  Top-10: {100*adv_combined_top10/n:.1f}%")
    print(f"  Combined system on production (primary tools):")
    print(f"    Top-1: {100*prod_combined_top1/m:.1f}%  Top-5: {100*prod_combined_top5/m:.1f}%  Top-10: {100*prod_combined_top10/m:.1f}%")
    print(f"  TFI saves Azure API calls on {100*prod_tfi_used/m:.0f}% of production queries")
    print(f"{'='*76}")


if __name__ == "__main__":
    asyncio.run(main())
