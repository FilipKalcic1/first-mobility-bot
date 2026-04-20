# Council Prompt: Optimalna arhitektura za MobilityOne tool resolution

Kopiraj CIJELI ovaj prompt u novi Claude razgovor.

---

Ti si arhitektonski council od 4 eksperta. Svaki ekspert ima drugačiji pristup problemu. Nakon što svaki iznese svoj prijedlog, debatirate i konvergirate na NAJBOLJE rješenje.

## TVOJ ZADATAK

Dizajniraj optimalnu arhitekturu za sustav koji prima poruku korisnika na hrvatskom jeziku (WhatsApp) i mora odabrati točan API alat od ~950 mogućih.

**SMIJEŠ potpuno odstupiti od trenutne arhitekture.** Nisi ograničen na postojeći pristup. Jedina ograničenja su:
- Azure OpenAI (embedding: ada-002 ili text-embedding-3-small/large ako je dostupno)
- GPT-4o-mini za LLM pozive (latency budget: <5s ukupno)
- Redis za cache
- ~950 API alata u fleet management domeni (vozila, troškovi, putovanja, oprema, zaposlenici...)
- Korisnici pišu kolokvijalni hrvatski na mobitelu

---

## TRENUTNI SUSTAV — KOMPLETNA SPECIFIKACIJA

### Arhitektura (6 koraka)

```
Korisnikov upit (hrvatski, WhatsApp)
    |
    v
1. ACTION INTENT DETECTION
   - ML klasifikator: CREATE/READ/UPDATE/DELETE/UNKNOWN
   - Confidence threshold: 0.95 za filtriranje FAISS rezultata
   - Ako confidence < 0.95: NE filtrira, LLM odlučuje
    |
    v
2. ENTITY DETECTION + TFI (Tool Family Index)
   - Substring matching: 75 entiteta, svaki s 3-15 stem-ova na hrvatskom
   - Stems sortirani po duljini (najdulji prvi) — "tipovi vozil" > "vozil"
   - Modifier entiteti (stats, metadata, settings) deprioritizirani
   - Possessive detection: "moj auto" → vehicles
   - Ako entity pronađen → TFI deterministički lookup:
     entity + queryType + HTTP method → točan tool_id
   - TFI scores: 0.88 (resolved tool), 0.75 (family varijante)
    |
    v
3. FAISS SEMANTIC SEARCH (uvijek se pokreće, čak i kad TFI uspije)
   - Model: Azure text-embedding-ada-002 (1536 dim)
   - Index: faiss.IndexFlatIP (cosine similarity)
   - Pool: min(500, top_k × 3) kandidata
   - Entity mismatch penalty: 1.0 (efektivno NEMA penalizacije)
   - Action filter: hard exclude (DELETE upit ne vraća GET alate)
   - HyDE fallback: ako top_score < 0.72, LLM generira hipotetski opis alata,
     concatenira s upitom, ponovo pretražuje
    |
    v
4. BOOST ENGINE (additivni model)
   - Entity match: +0.05
   - Family match: +0.06
   - Suffix intent (_groupby, _agg, _projectto): +0.05-0.06
   - Method mismatch: -0.05
   - BM25 hybrid: +0.07 × normalized_score
   - Possessive boosts: ±0.03-0.04
   - CAP: total boost ∈ [-0.08, +0.12]
    |
    v
5. MERGE + EXACT MATCH
   - TFI rezultati merge s FAISS+boost rezultatima (max score per tool)
   - Exact match index: 7587 example_queries_hr → tool_id, score=15.0
   - Sort po score, truncate na top 20
    |
    v
6. LLM RERANKER + LLM ROUTER
   - Reranker: top-20 kandidata → LLM reranks (gpt-4o-mini, temp=0)
   - Router: top-25 kandidata → LLM bira action + tool + params
   - Actions: SIMPLE_API, START_FLOW, CLARIFY, DIRECT_RESPONSE
   - Ako entity nejasan: pita korisnika "Na što se odnosi? (vozila, troškovi...)"
```

### Embedding tekst za svaki alat (kako se gradi)

```
[Entity prefix × 5 (npr. "vozilo automobil auto")]
+ [Purpose (hr)]
+ [when_to_use (hr)]
+ [HTTP method glagoli (hr): "obriši ukloni izbriši makni"]
+ [Synonyms_hr]
+ [Entity prefix ponovljen]
```

### Konfiguracija alata (tool_documentation.json, 950 alata)

Svaki alat ima:
```json
{
  "operation_id": "delete_Vehicles_DeleteByCriteria",
  "purpose": "Brisanje vozila prema kriterijima filtriranja",
  "when_to_use": ["Kada trebate obrisati više vozila odjednom"],
  "example_queries_hr": ["Izbriši vozila starija od 10 godina", ...],
  "synonyms_hr": ["vozilo", "auto", "automobil"],
  "parameter_origin_guide": {"filter": "USER: kriterij za brisanje"}
}
```

### Taxonomy struktura

- 950 alata = 21 operacija × ~45 entiteta
- Operacije: GET list, GET by ID, POST, PUT, PATCH, DELETE, DELETE by criteria, GroupBy, Agg, ProjectTo, multipatch, documents, metadata, thumb, Lookup, tree, SetAsDefault...
- Entiteti: Vehicles, Expenses, Trips, Cases, Equipment, Partners, Teams, Persons, OrgUnits, CostCenters, Roles, Tags, Pools, VehicleTypes, ExpenseTypes, TripTypes, CaseTypes, EquipmentTypes, VehicleCalendar, EquipmentCalendar, VehicleContracts, MileageReports, PeriodicActivities, SchedulingModels, DashboardItems...
- 80.4% alata ima identičnu parameter shemu unutar iste operacije

### Entity stems primjer

```json
{
  "vehicles": ["vozil", "auto", "automobil", "flot", "auti", "kola"],
  "expenses": ["trosk", "trosak", "troška", "trošak", "izdatak", "racun", "rashod"],
  "trips": ["putovanj", "trip", "voznj", "vožnj", "putni"],
  "cases": ["slucaj", "slučaj", "steta", "šteta", "kvar", "incident", "prijav"],
  "equipment": ["oprem", "uredaj", "uređaj", "inventar", "alat"]
}
```

### HTTP glagoli (hrvatski → method)

```json
{
  "obrisi": "delete", "izbrisi": "delete", "makni": "delete", "ukloni": "delete",
  "dodaj": "post", "kreiraj": "post", "napravi": "post", "unesi": "post",
  "azuriraj": "put", "izmijeni": "put", "promijeni": "put",
  "pokazi": "get", "prikazi": "get", "daj": "get", "dohvati": "get"
}
```

---

## BENCHMARK REZULTATI

### Produkcijski upiti (example_queries_hr, kako pravi korisnici pišu)

| Metrika | Full Pipeline (bez LLM reranker) |
|---|---|
| **Top-1** | **99.1%** (232/234) |
| **Top-5** | **100%** |
| **Top-25** | **100%** |

Glavni driver: exact match index (7587 entries) daje score 15.0.

### Adversarial upiti (176 upita BEZ entity ključnih riječi)

Primjeri adversarial upita:
- "Trebam se riješiti nekih stavki iz rezervacijskog plana" → delete_EquipmentCalendar_DeleteByCriteria
- "Možeš li mi pomoći da izbacim stavku koja mi ne treba?" → delete_Expenses_id
- "Želim izbrisati različite kategorije zadataka u sustavu" → delete_PersonActivityTypes

| Metrika | Full Pipeline | Raw FAISS only | gpt-4o (950 alata) |
|---|---|---|---|
| Top-1 | 7.4% | 11.4% | 14.2% |
| Top-5 | 24.4% | 39.2% | N/A |
| Top-10 | 35.2% | 51.7% | N/A |
| Top-25 | 55.1% | **72.7%** | N/A |

**Ključni nalaz:** Raw FAISS (bez TFI/boost) daje 72.7% Top-25, ali full pipeline daje samo 55.1%. TFI injektira krive family alate kad entity detection pogriješi (51% adversarial upita ima krivo detektirani entity).

**gpt-4o s punom listom 950 alata postiže samo 14.2%** — dokazuje da je adversarial benchmark logički nerješiv bez entity vokabulara.

### Dual Index eksperiment (tool embeddings + example query embeddings)

| Index | Adversarial Top-25 | Production Top-1 |
|---|---|---|
| Tool-only (ada-002) | **72.7%** | 32.1% |
| Example-only (ada-002) | 22.7% | **98.7%** |
| Dual (weighted merge) | 22.7% | 99.1% |

Example query index je odličan za produkciju ali beskoristan za adversarial jer adversarial upiti ne liče na example queries.

### HyDE eksperiment

HyDE (Hypothetical Document Expansion) daje +1-4pp na nekim seedovima, zanemarivo.

### Eksperiment: Entity mismatch penalty + boost cap

Promjena entity penalty (1.0 → 0.85) i boost cap (0.12 → 0.20): BEZ EFEKTA na adversarial. Blago pogoršanje jer kad je entity detection kriv, penalty kažnjava ispravne alate.

---

## OGRANIČENJA

1. **Embedding model:** Azure ada-002 (1536 dim). Slabiji od text-embedding-3-large za hrvatski. 3-large nije deployan na Azure, OpenAI krediti potrošeni.
2. **Latency:** <5 sekundi end-to-end (LLM RTT dominira: 2-4s)
3. **Cost:** Embedding poziv per query (~$0.0001). LLM routing per query (~$0.001). HyDE per query (~$0.003 kad se triggera). LLM rerank per query (~$0.002).
4. **Redis:** Dostupan za cache (HyDE, kontekst, sesije)
5. **Korisnici:** Fleet manageri u Hrvatskoj, pišu na mobitelu, kratke poruke, često bez dijakritičkih znakova
6. **Nema pravih korisničkih logova** za analizu — benchmark je sintetički

---

## COUNCIL FORMAT

### Ekspert 1: "Retrieval Maximalist"
Fokus: Maksimizirati retrieval accuracy bez LLM-a. Pristup: bolji embeddings, bolji indeksi, bolja tekst reprezentacija. Smanjiti ovisnost o LLM-u.

### Ekspert 2: "LLM-First Pragmatist"  
Fokus: LLM je pametni, embeddings su glupi — iskoristi LLM maksimalno. Pristup: hijerarhijski routing, chain-of-thought, structured classification.

### Ekspert 3: "Taxonomy Refactor Advocate"
Fokus: 950 alata je previše za bilo koji pristup. Refaktoriraj na ~125 generičkih alata s entity parametrom. Pristup: smanjiti classification space.

### Ekspert 4: "Hybrid Pragmatist"
Fokus: Kombiniraj najbolje iz svakog pristupa. Prioritiziraj ROI — što daje najveći dobitak za najmanji trošak?

### Format rasprave

Za svakog eksperta:
1. **Prijedlog** (3-5 ključnih promjena)
2. **Očekivani accuracy** (s argumentacijom)
3. **Troškovi i rizici**
4. **Kritika ostalih pristupa**

Zatim:
5. **Debata** — svaki ekspert odgovara na kritike
6. **Konsenzus** — konačna preporuka s konkretnim implementacijskim planom
7. **Implementacijski plan** — točni fileovi, promjene, redoslijed

---

## CILJEVI

1. **Produkcija (upiti s entity ključnim riječima):** Zadržati ≥99% Top-1
2. **Edge cases (upiti bez entity ključnih riječi):** Podići Top-25 s 55% na ≥75%
3. **Cost:** Ne više od $0.01 per query prosječno
4. **Latency:** <5 sekundi end-to-end
5. **Održivost:** Rješenje koje se može održavati bez konstantnog tuninga

VAŽNO: Adversarial benchmark NIJE produkcija. Pravi korisnici gotovo uvijek koriste entity ključne riječi. Ali sustav mora imati graceful degradation za edge cases — bilo kroz bolje retrieval ili kroz pametno pitanje korisnika za pojašnjenje.

Počni s council debatom. Budi detaljan i konkretan — ne generičke preporuke nego točne promjene s očekivanim numeričkim utjecajem.
