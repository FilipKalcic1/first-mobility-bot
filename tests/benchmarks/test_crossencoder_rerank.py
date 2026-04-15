"""
Benchmark: Cross-encoder reranking of FAISS top-100.

1. FAISS (ada-002) returns top-100 candidates (current pipeline)
2. Cross-encoder scores each (query, tool_text) pair with bidirectional attention
3. Rerank by cross-encoder score → Top-5/Top-20

Cross-encoders are ~10x more accurate than bi-encoders for retrieval.
The model can learn that "predmet" in query relates to "slučaj" in tool text.
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


def build_tool_display_text(tool_id: str, doc: dict) -> str:
    """Build short text for cross-encoder scoring (entity + purpose + when_to_use)."""
    import re
    ENTITY_LABELS = {
        "vehicles": "Vozila automobili",
        "vehicletypes": "Tipovi vozila",
        "vehiclecalendar": "Kalendar vozila",
        "vehiclecontracts": "Ugovori vozila",
        "vehicleassignments": "Dodjele vozila",
        "equipment": "Oprema inventar alati",
        "equipmenttypes": "Tipovi opreme",
        "equipmentcalendar": "Kalendar opreme",
        "persons": "Osobe zaposlenici",
        "persontypes": "Tipovi osoba",
        "personorgunits": "Org. jedinice osoba",
        "personperiodicactivities": "Periodične aktivnosti osoba",
        "personactivitytypes": "Tipovi aktivnosti osoba",
        "teams": "Timovi grupe",
        "teammembers": "Članovi tima",
        "cases": "Slučajevi predmeti štete kvarovi",
        "casetypes": "Tipovi slučajeva",
        "expenses": "Troškovi izdaci računi",
        "expensetypes": "Tipovi troškova",
        "expensegroups": "Grupe troškova",
        "trips": "Putovanja putni nalozi",
        "triptypes": "Tipovi putovanja",
        "partners": "Partneri dobavljači klijenti",
        "documents": "Dokumenti prilozi",
        "documenttypes": "Tipovi dokumenata",
        "orgunits": "Organizacijske jedinice odjeli",
        "costcenters": "Troškovni centri",
        "roles": "Uloge dozvole",
        "tenants": "Tenanti korisnici sustava",
        "tenantpermissions": "Dozvole tenanta",
        "periodicactivities": "Periodične aktivnosti servisi",
        "periodicactivitiesschedules": "Rasporedi periodičnih aktivnosti",
        "periodicactivitytypes": "Tipovi periodičnih aktivnosti",
        "schedulingmodels": "Modeli raspoređivanja",
        "tags": "Oznake tagovi",
        "pools": "Poolovi grupe resursa",
        "metadata": "Metapodaci shema struktura",
        "lookup": "Šifrarnik referentni podaci",
        "dashboarditems": "Elementi nadzorne ploče",
        "booking": "Rezervacije",
        "mileagereports": "Izvještaji o kilometraži",
        "vehicleshistoricalentries": "Povijesni zapisi vozila",
        "vehiclesmonthlyexpenses": "Mjesečni troškovi vozila",
        "vehicleboard": "Ploča vozila",
        "mileage": "Kilometraža",
    }

    # Extract entity
    name = re.sub(r'^(get|post|put|patch|delete)_', '', tool_id, flags=re.IGNORECASE)
    name_lower = name.lower()
    entity_label = ""
    for ek in sorted(ENTITY_LABELS.keys(), key=len, reverse=True):
        if ek in name_lower:
            entity_label = ENTITY_LABELS[ek]
            break

    parts = []
    if entity_label:
        parts.append(f"[{entity_label}]")

    purpose = doc.get("purpose", "")
    if purpose:
        parts.append(purpose)

    when_to_use = doc.get("when_to_use", [])
    if when_to_use:
        parts.append(" ".join(when_to_use[:2]))  # First 2 only for brevity

    return " ".join(parts)


async def main():
    para_path = Path(__file__).parent / "tool_recognition_paraphrases.json"
    with open(para_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    doc_path = project_root / "config" / "tool_documentation.json"
    with open(doc_path, "r", encoding="utf-8") as f:
        tool_docs = json.load(f)

    # Pre-compute tool display texts
    tool_texts = {}
    for tool_id, doc in tool_docs.items():
        tool_texts[tool_id] = build_tool_display_text(tool_id, doc)

    # Initialize FAISS store for base retrieval
    print("Initializing FAISS (ada-002)...", flush=True)
    from services.faiss_vector_store import FAISSVectorStore
    store = FAISSVectorStore()
    await store.initialize(tool_documentation=tool_docs)
    print(f"  {store._index.ntotal} tools indexed", flush=True)

    # Load cross-encoder
    print("Loading cross-encoder (mmarco-mMiniLMv2-L12-H384-v1)...", flush=True)
    t0 = time.time()
    from sentence_transformers import CrossEncoder
    cross_encoder = CrossEncoder('cross-encoder/mmarco-mMiniLMv2-L12-H384-v1')
    print(f"  Loaded in {time.time() - t0:.1f}s", flush=True)

    # Run benchmark
    top1 = top5 = top20 = 0
    top1_faiss = top5_faiss = top20_faiss = 0
    total = 0
    misses = []

    for tool_entry in data["tools"]:
        expected_tool = tool_entry["tool_id"]

        for paraphrase in tool_entry["paraphrases"]:
            total += 1

            # Step 1: FAISS retrieval (top-100)
            faiss_results = await store.search(paraphrase, top_k=100)
            faiss_ids = [r.tool_id for r in faiss_results]

            # Track raw FAISS accuracy
            if expected_tool in faiss_ids[:1]:
                top1_faiss += 1
            if expected_tool in faiss_ids[:5]:
                top5_faiss += 1
            if expected_tool in faiss_ids[:20]:
                top20_faiss += 1

            # Step 2: Cross-encoder reranking
            # Build (query, tool_text) pairs
            pairs = [(paraphrase, tool_texts.get(tid, "")) for tid in faiss_ids]
            scores = cross_encoder.predict(pairs)

            # Rerank by cross-encoder score
            reranked = sorted(zip(faiss_ids, scores), key=lambda x: x[1], reverse=True)
            reranked_ids = [r[0] for r in reranked]

            # Measure accuracy
            if expected_tool in reranked_ids[:1]:
                top1 += 1
            if expected_tool in reranked_ids[:5]:
                top5 += 1
            if expected_tool in reranked_ids[:20]:
                top20 += 1
            else:
                rank = reranked_ids.index(expected_tool) + 1 if expected_tool in reranked_ids else 999
                misses.append({
                    "expected": expected_tool,
                    "query": paraphrase[:80],
                    "rerank_rank": rank,
                    "top3": reranked_ids[:3],
                })

            if total % 10 == 0:
                print(
                    f"  [{total}/290] FAISS Top-5={top5_faiss}/{total} "
                    f"Reranked Top-5={top5}/{total}",
                    flush=True,
                )

    n = total
    print(f"\n{'='*70}")
    print(f"CROSS-ENCODER RERANKING BENCHMARK")
    print(f"{'='*70}")
    print(f"Total queries: {n}")
    print(f"\n  RAW FAISS (ada-002, top-100 pool):")
    print(f"    Top-1:  {top1_faiss}/{n} = {top1_faiss/n:.1%}")
    print(f"    Top-5:  {top5_faiss}/{n} = {top5_faiss/n:.1%}")
    print(f"    Top-20: {top20_faiss}/{n} = {top20_faiss/n:.1%}")
    print(f"\n  CROSS-ENCODER RERANKED (from FAISS top-100):")
    print(f"    Top-1:  {top1}/{n} = {top1/n:.1%}")
    print(f"    Top-5:  {top5}/{n} = {top5/n:.1%}")
    print(f"    Top-20: {top20}/{n} = {top20/n:.1%}")
    print(f"\n  IMPROVEMENT:")
    print(f"    Top-1:  {top1 - top1_faiss:+d} ({(top1 - top1_faiss)/n:+.1%})")
    print(f"    Top-5:  {top5 - top5_faiss:+d} ({(top5 - top5_faiss)/n:+.1%})")
    print(f"    Top-20: {top20 - top20_faiss:+d} ({(top20 - top20_faiss)/n:+.1%})")

    print(f"\nMisses: {len(misses)}")

    # Save
    out_path = Path(__file__).parent / "crossencoder_results.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "total": n,
            "faiss": {"top1": top1_faiss, "top5": top5_faiss, "top20": top20_faiss},
            "reranked": {"top1": top1, "top5": top5, "top20": top20},
            "misses": misses,
        }, f, indent=2, ensure_ascii=False)
    print(f"Saved to {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
