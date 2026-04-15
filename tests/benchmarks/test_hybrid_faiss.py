"""
Benchmark: Hybrid FAISS — ada-002 + multilingual-MiniLM merged results.
If the two models find different correct tools, the union yields higher accuracy.
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

import numpy as np
import faiss


def build_ml_text(tool_id: str, doc: dict) -> str:
    """Build text for multilingual model: entity + purpose + when_to_use + synonyms."""
    import re
    ENTITY_PREFIXES = {
        "companies": "Kompanije tvrtke firme",
        "vehicles": "Vozila automobili auti",
        "vehicletypes": "Tipovi vozila",
        "vehiclecalendar": "Kalendar rezervacija vozila",
        "vehiclecontracts": "Ugovori najam vozila",
        "vehicleassignments": "Dodjele vozila",
        "equipment": "Oprema inventar alati uređaji",
        "equipmenttypes": "Tipovi opreme",
        "equipmentcalendar": "Kalendar opreme",
        "persons": "Osobe zaposlenici radnici",
        "persontypes": "Tipovi osoba",
        "personorgunits": "Organizacijske jedinice osoba",
        "personperiodicactivities": "Aktivnosti osoba",
        "personactivitytypes": "Tipovi aktivnosti osoba",
        "teams": "Timovi grupe ekipe",
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
        "tenants": "Tenanti sustav",
        "tenantpermissions": "Dozvole tenanta",
        "periodicactivities": "Periodične aktivnosti servisi",
        "periodicactivitiesschedules": "Rasporedi aktivnosti",
        "periodicactivitytypes": "Tipovi periodičnih aktivnosti",
        "schedulingmodels": "Modeli raspoređivanja",
        "tags": "Oznake tagovi",
        "pools": "Poolovi resursi",
        "metadata": "Metapodaci shema struktura",
        "lookup": "Šifrarnik referentni podaci",
        "dashboarditems": "Nadzorna ploča dashboard",
        "booking": "Rezervacije",
        "mileagereports": "Kilometraža izvještaji",
        "vehicleshistoricalentries": "Povijest vozila",
        "vehiclesmonthlyexpenses": "Mjesečni troškovi vozila",
        "vehicleboard": "Ploča vozila",
        "mileage": "Kilometraža",
    }
    parts = []
    name = re.sub(r'^(get|post|put|patch|delete)_', '', tool_id, flags=re.IGNORECASE)
    name_lower = name.lower()
    for ek in sorted(ENTITY_PREFIXES.keys(), key=len, reverse=True):
        if ek in name_lower:
            parts.append(ENTITY_PREFIXES[ek])
            break
    purpose = doc.get("purpose", "")
    if purpose: parts.append(purpose)
    wtu = doc.get("when_to_use", [])
    if wtu: parts.append(" ".join(wtu))
    synonyms = doc.get("synonyms_hr", [])
    if synonyms: parts.append(" ".join(synonyms))
    return " ".join(parts)


async def main():
    para_path = Path(__file__).parent / "tool_recognition_paraphrases.json"
    with open(para_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    doc_path = project_root / "config" / "tool_documentation.json"
    with open(doc_path, "r", encoding="utf-8") as f:
        tool_docs = json.load(f)

    # 1. Ada-002 FAISS (existing V6.0)
    print("Initializing ada-002 FAISS V6.0...", flush=True)
    from services.faiss_vector_store import FAISSVectorStore
    ada_store = FAISSVectorStore()
    await ada_store.initialize(tool_documentation=tool_docs)
    ada_tool_ids = ada_store._tool_ids
    print(f"  {ada_store._index.ntotal} tools", flush=True)

    # 2. Multilingual FAISS
    print("Initializing multilingual FAISS...", flush=True)
    from sentence_transformers import SentenceTransformer
    ml_model = SentenceTransformer('paraphrase-multilingual-MiniLM-L12-v2')

    ml_tool_ids = sorted(tool_docs.keys())
    ml_texts = [build_ml_text(tid, tool_docs[tid]) for tid in ml_tool_ids]
    ml_embeddings = ml_model.encode(ml_texts, batch_size=64, show_progress_bar=False)
    ml_embeddings_np = np.array(ml_embeddings, dtype=np.float32)
    faiss.normalize_L2(ml_embeddings_np)
    ml_index = faiss.IndexFlatIP(ml_embeddings_np.shape[1])
    ml_index.add(ml_embeddings_np)
    print(f"  {ml_index.ntotal} tools", flush=True)

    # Benchmark
    top1_ada = top5_ada = top20_ada = 0
    top1_ml = top5_ml = top20_ml = 0
    top1_hybrid = top5_hybrid = top20_hybrid = 0
    total = 0

    for tool_entry in data["tools"]:
        expected = tool_entry["tool_id"]
        for paraphrase in tool_entry["paraphrases"]:
            total += 1

            # Ada-002 search
            ada_results = await ada_store.search(paraphrase, top_k=20)
            ada_ids = [r.tool_id for r in ada_results]

            # Multilingual search
            q_emb = ml_model.encode([paraphrase])
            q_np = np.array(q_emb, dtype=np.float32)
            faiss.normalize_L2(q_np)
            distances, indices = ml_index.search(q_np, 20)
            ml_ids = [ml_tool_ids[idx] for idx in indices[0] if idx >= 0]

            # Hybrid: interleave (ada first, then ml unique)
            hybrid_ids = list(ada_ids)  # Start with ada
            for mid in ml_ids:
                if mid not in hybrid_ids:
                    hybrid_ids.append(mid)

            # Score
            if expected in ada_ids[:1]: top1_ada += 1
            if expected in ada_ids[:5]: top5_ada += 1
            if expected in ada_ids[:20]: top20_ada += 1

            if expected in ml_ids[:1]: top1_ml += 1
            if expected in ml_ids[:5]: top5_ml += 1
            if expected in ml_ids[:20]: top20_ml += 1

            if expected in hybrid_ids[:5]: top5_hybrid += 1
            if expected in hybrid_ids[:20]: top20_hybrid += 1
            if expected in hybrid_ids[:1]: top1_hybrid += 1

            if total % 50 == 0:
                print(
                    f"  [{total}/290] Ada5={top5_ada} ML5={top5_ml} Hybrid5={top5_hybrid}",
                    flush=True,
                )

    n = total
    print(f"\n{'='*70}")
    print(f"HYBRID FAISS BENCHMARK")
    print(f"{'='*70}")
    print(f"{'Method':<25s}  {'Top-1':>8s}  {'Top-5':>8s}  {'Top-20':>8s}")
    print(f"{'-'*55}")
    print(f"{'Ada-002 (V6.0)':<25s}  {top1_ada:3d}/{n} {top1_ada/n:.1%}  {top5_ada:3d}/{n} {top5_ada/n:.1%}  {top20_ada:3d}/{n} {top20_ada/n:.1%}")
    print(f"{'Multilingual MiniLM':<25s}  {top1_ml:3d}/{n} {top1_ml/n:.1%}  {top5_ml:3d}/{n} {top5_ml/n:.1%}  {top20_ml:3d}/{n} {top20_ml/n:.1%}")
    print(f"{'Hybrid (Ada + ML)':<25s}  {top1_hybrid:3d}/{n} {top1_hybrid/n:.1%}  {top5_hybrid:3d}/{n} {top5_hybrid/n:.1%}  {top20_hybrid:3d}/{n} {top20_hybrid/n:.1%}")

    # Overlap analysis
    ada_set = set()
    ml_set = set()
    both_set = set()
    ada_only = set()
    ml_only = set()
    idx = 0
    for tool_entry in data["tools"]:
        expected = tool_entry["tool_id"]
        for _ in tool_entry["paraphrases"]:
            # Count queries where correct in top-5
            in_ada = (idx < len(ada_ids)) and True  # approximate
            idx += 1

    print(f"\n  Ada-002 unique Top-5 hits: how many does ML NOT get?")
    # Recount properly
    ada_top5_set = set()
    ml_top5_set = set()
    q_idx = 0
    for tool_entry in data["tools"]:
        expected = tool_entry["tool_id"]
        for paraphrase in tool_entry["paraphrases"]:
            ada_results2 = await ada_store.search(paraphrase, top_k=5)
            ada_ids2 = [r.tool_id for r in ada_results2]

            q_emb2 = ml_model.encode([paraphrase])
            q_np2 = np.array(q_emb2, dtype=np.float32)
            faiss.normalize_L2(q_np2)
            d2, i2 = ml_index.search(q_np2, 5)
            ml_ids2 = [ml_tool_ids[idx2] for idx2 in i2[0] if idx2 >= 0]

            if expected in ada_ids2: ada_top5_set.add(q_idx)
            if expected in ml_ids2: ml_top5_set.add(q_idx)
            q_idx += 1

    only_ada = ada_top5_set - ml_top5_set
    only_ml = ml_top5_set - ada_top5_set
    both = ada_top5_set & ml_top5_set
    union = ada_top5_set | ml_top5_set

    print(f"  Ada-only in Top-5: {len(only_ada)}")
    print(f"  ML-only in Top-5:  {len(only_ml)}")
    print(f"  Both in Top-5:     {len(both)}")
    print(f"  UNION Top-5:       {len(union)}/{n} = {len(union)/n:.1%}")


if __name__ == "__main__":
    asyncio.run(main())
