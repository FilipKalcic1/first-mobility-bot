"""
Benchmark: multilingual-MiniLM-L12-v2 FAISS index vs ada-002.
Builds a separate 384-dim FAISS index using sentence-transformers,
embeds all 290 benchmark queries locally, and measures Top-1/5/20.

No Azure API calls needed — all local.
"""
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

import numpy as np
import faiss


def build_embedding_text(tool_id: str, doc: dict) -> str:
    """Simplified embedding text: entity prefix + purpose + when_to_use + synonyms."""
    import re

    # Entity compound prefixes (same as faiss_vector_store.py)
    ENTITY_PREFIXES = {
        "companies": "Kompanije tvrtke firme - upravljanje kompanijama, tvrtkama, poduzećima.",
        "vehicles": "Vozila automobili auti - upravljanje vozilima, automobilima, flotom.",
        "vehicletypes": "Tipovi vozila kategorije vozila - vrste vozila u sustavu.",
        "vehiclecalendar": "Kalendar vozila raspored rezervacija - rezervacije i raspoloživost vozila.",
        "vehiclecontracts": "Ugovori vozila leasing najam - ugovori o korištenju vozila.",
        "vehicleassignments": "Dodjele vozila raspodjela - tko koristi koje vozilo.",
        "equipment": "Oprema inventar alati uređaji - upravljanje opremom, inventarom, alatima.",
        "equipmenttypes": "Tipovi opreme vrste opreme - kategorije opreme.",
        "equipmentcalendar": "Kalendar opreme raspored - rezervacije opreme.",
        "persons": "Osobe zaposlenici radnici - upravljanje zaposlenicima, radnicima.",
        "persontypes": "Tipovi osoba kategorije zaposlenika - vrste zaposlenika.",
        "personorgunits": "Organizacijske jedinice osoba odjeli - pripadnost zaposlenika odjelima.",
        "personperiodicactivities": "Periodične aktivnosti osoba - redovne aktivnosti zaposlenika.",
        "personactivitytypes": "Tipovi aktivnosti osoba - vrste aktivnosti zaposlenika.",
        "teams": "Timovi grupe ekipe - upravljanje timovima, radnim grupama.",
        "teammembers": "Članovi tima - tko je u kojem timu.",
        "cases": "Slučajevi predmeti štete kvarovi prijave - upravljanje slučajevima, štetama, prijavama.",
        "casetypes": "Tipovi slučajeva vrste prijava - kategorije slučajeva.",
        "expenses": "Troškovi izdaci računi rashodi - upravljanje troškovima, financijskim stavkama.",
        "expensetypes": "Tipovi troškova vrste troškova - kategorije troškova.",
        "expensegroups": "Grupe troškova skupine - grupiranje troškova.",
        "trips": "Putovanja putni nalozi - upravljanje putovanjima, službenim putovima.",
        "triptypes": "Tipovi putovanja - kategorije putovanja.",
        "partners": "Partneri dobavljači klijenti suradnici - upravljanje poslovnim partnerima.",
        "documents": "Dokumenti prilozi datoteke - upravljanje dokumentima.",
        "documenttypes": "Tipovi dokumenata - kategorije dokumenata.",
        "orgunits": "Organizacijske jedinice odjeli sektori - upravljanje odjelima.",
        "costcenters": "Troškovni centri mjesta troška - upravljanje troškovnim centrima.",
        "roles": "Uloge dozvole permisije - upravljanje ulogama, pravima pristupa.",
        "tenants": "Tenanti najmovi korisnici sustava - upravljanje tenantima.",
        "tenantpermissions": "Dozvole tenanta korisničke dozvole - prava tenanta.",
        "periodicactivities": "Periodične aktivnosti servisi - upravljanje redovnim poslovima.",
        "periodicactivitiesschedules": "Rasporedi periodičnih aktivnosti - termini servisa.",
        "periodicactivitytypes": "Tipovi periodičnih aktivnosti - kategorije servisa.",
        "schedulingmodels": "Modeli raspoređivanja - sheme za raspoređivanje.",
        "tags": "Oznake tagovi labele - upravljanje oznakama.",
        "pools": "Poolovi grupe resursa - upravljanje poolovima.",
        "metadata": "Metapodaci shema struktura polja - definicija polja sustava.",
        "lookup": "Šifrarnik referentni podaci katalog - šifrarnici, lookup tablice.",
        "dashboarditems": "Elementi nadzorne ploče dashboard - stavke kontrolne ploče.",
        "booking": "Rezervacija zauzimanje - rezervacije resursa.",
        "mileagereports": "Izvještaji o kilometraži - praćenje prijeđenih kilometara.",
        "vehicleshistoricalentries": "Povijesni zapisi vozila - povijest promjena vozila.",
        "vehiclesmonthlyexpenses": "Mjesečni troškovi vozila - pregled troškova po vozilima.",
        "vehicleboard": "Ploča vozila pregled voznog parka.",
        "mileage": "Kilometraža prijeđeni put.",
    }

    parts = []

    # Entity prefix
    name = re.sub(r'^(get|post|put|patch|delete)_', '', tool_id, flags=re.IGNORECASE)
    name_lower = name.lower()
    for entity_key in sorted(ENTITY_PREFIXES.keys(), key=len, reverse=True):
        if entity_key in name_lower:
            parts.append(ENTITY_PREFIXES[entity_key])
            break

    # Purpose
    purpose = doc.get("purpose", "")
    if purpose:
        parts.append(purpose)

    # When to use
    when_to_use = doc.get("when_to_use", [])
    if when_to_use:
        parts.append(" ".join(when_to_use))

    # Synonyms
    synonyms = doc.get("synonyms_hr", [])
    if synonyms:
        parts.append(" ".join(synonyms))

    return " ".join(parts)


def main():
    para_path = Path(__file__).parent / "tool_recognition_paraphrases.json"
    with open(para_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    doc_path = project_root / "config" / "tool_documentation.json"
    with open(doc_path, "r", encoding="utf-8") as f:
        tool_docs = json.load(f)

    print("Loading multilingual-MiniLM-L12-v2...", flush=True)
    t0 = time.time()
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer('paraphrase-multilingual-MiniLM-L12-v2')
    print(f"  Loaded in {time.time() - t0:.1f}s", flush=True)

    # Build tool texts and embeddings
    print("Embedding 950 tools...", flush=True)
    t0 = time.time()
    tool_ids = sorted(tool_docs.keys())
    tool_texts = [build_embedding_text(tid, tool_docs[tid]) for tid in tool_ids]
    tool_embeddings = model.encode(tool_texts, show_progress_bar=True, batch_size=64)
    print(f"  {len(tool_ids)} tools embedded in {time.time() - t0:.1f}s", flush=True)
    print(f"  Embedding dim: {tool_embeddings.shape[1]}", flush=True)

    # Build FAISS index
    dim = tool_embeddings.shape[1]
    embeddings_np = np.array(tool_embeddings, dtype=np.float32)
    faiss.normalize_L2(embeddings_np)
    index = faiss.IndexFlatIP(dim)
    index.add(embeddings_np)
    print(f"  FAISS index built: {index.ntotal} vectors", flush=True)

    # Embed all benchmark queries
    print("Embedding 290 queries...", flush=True)
    all_queries = []
    query_to_expected = []
    for tool_entry in data["tools"]:
        expected_tool = tool_entry["tool_id"]
        for paraphrase in tool_entry["paraphrases"]:
            all_queries.append(paraphrase)
            query_to_expected.append(expected_tool)

    t0 = time.time()
    query_embeddings = model.encode(all_queries, show_progress_bar=True, batch_size=64)
    print(f"  {len(all_queries)} queries embedded in {time.time() - t0:.1f}s", flush=True)

    # Search
    print("Searching...", flush=True)
    query_np = np.array(query_embeddings, dtype=np.float32)
    faiss.normalize_L2(query_np)
    distances, indices = index.search(query_np, 50)

    # Compute metrics
    top1 = top5 = top20 = 0
    total = len(all_queries)
    misses = []

    for i in range(total):
        expected = query_to_expected[i]
        result_ids = [tool_ids[idx] for idx in indices[i] if idx >= 0]

        if expected in result_ids[:1]:
            top1 += 1
        if expected in result_ids[:5]:
            top5 += 1
        if expected in result_ids[:20]:
            top20 += 1
        else:
            # Find rank
            rank = result_ids.index(expected) + 1 if expected in result_ids else 999
            misses.append({
                "expected": expected,
                "query": all_queries[i][:80],
                "rank": rank,
                "top3": result_ids[:3],
            })

    print(f"\n{'='*70}")
    print(f"MULTILINGUAL FAISS BENCHMARK (paraphrase-multilingual-MiniLM-L12-v2)")
    print(f"{'='*70}")
    print(f"Total queries: {total}")
    print(f"Top-1:  {top1}/{total} = {top1/total:.1%}")
    print(f"Top-5:  {top5}/{total} = {top5/total:.1%}")
    print(f"Top-20: {top20}/{total} = {top20/total:.1%}")
    print(f"Misses: {len(misses)}")
    print(f"\nFor comparison (ada-002 via UnifiedSearch):")
    print(f"  Top-5: 91/290 = 31.4%")
    print(f"  Top-20: 147/290 = 50.7%")

    # Show some misses
    print(f"\nSAMPLE MISSES (first 15):")
    for m in misses[:15]:
        print(f"  exp={m['expected'][:30]:30s} rank={m['rank']:3d} top3={[t[:25] for t in m['top3']]}")


if __name__ == "__main__":
    main()
