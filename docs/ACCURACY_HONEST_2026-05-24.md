# Pošten accuracy review routinga — 2026-05-24

**Pitanje (Filip):** je li routing industrijski-standardan ili "namješten" (hardkodiranje, doslovni primjeri) da izgleda da radi? Koliki je pošten accuracy na **teškim, mješovitim, NE-driver** upitima? Rade li flows + single-call i odlučuje li dobro kada koji?

**Opseg:** SAMO mjerenje + review. Nikakve promjene routing koda. Sve brojke su iz pravog Azure e2e bencha (`scripts/bench_router_e2e.py`) protiv Damirovog tool_data (950 tools).

---

## 0. Trust-but-verify: uhvaćen lažni cache (zašto su prve brojke bile smeće)
Prvi run je dao p@1 5.8% / 56.9% — **nevažeće**. Anchor cache `tests/benchmarks/router_anchor_cache.json` sadržavao je **fake 8-dim vektore** (MD5 hash iz unit-test harnessa), ne prave 1536-dim ada-002. Fingerprint se poklapao po TEKSTU anchora pa je bench tiho učitao smeće → L3 retrieval = šum → sve je kolabiralo na isti top-3. Rebuildano s pravim ada-002 (`dim=1536`, 11408 vektora) → brojke ispod su prave.
- ✅ Fake cache je **untracked** → NE ide u produkciju (čisti deploy rebuilda prave vektore).
- ⚠️ **Smell za kasnije:** produkcijski factory ([engine.py:1893](services/v2/engine.py#L1893)) cilja anchor cache **unutar `tests/benchmarks/`**. Treba ga premjestiti izvan `tests/` + buildati na deployu (ili shippati pravi prebuilt cache), inače stale/fake fajl na tom putu lomi L3.

---

## 1. Je li routing namješten? — NIJE (verificirano iz koda)
- **L3 (950-tool router)** — `llm_router.py` ima **0 keyword/literal forsiranja toola** (grep: samo empty-query guard). Čisto: catalog scope → **anchor cosine top-50** → **LLM tool-call (temp 0)** → hallucination-check (pick mora biti u top-50). To je **standardni RAG retrieval + LLM rerank.**
- **Benchmark** je **ručno pisan** (note-ovi "Chakavian dialect", "diminutive autica", "metaphor") — NIJE izveden iz anchora. Nema train/test leakage.
- **JEST keyword-asistirano (ali scoped, legitiman shortcut):** `driver_basics` (L2b) ima 15 regexa + 1-riječne `^tablica$`/`^marka$` → uvijek `get_MasterData`. I `_guess_flow_name` (12 substringa) bira flow. Oboje su brze rute za česte driver upite, NE generalni router.

---

## 2. Brojke (pravi ada-002 cache, real Azure)

| Eval set | n | p@1 | recall@3 | bilješka |
|---|---|---|---|---|
| **Stari benchmark** (driver-težak, česti tools, PUNI pipeline + L2b) | 58 | **70.7%** | 75.9% | driver 83% / manager 53% / admin 40%; GET 77%, **POST 0/5**; L2b servira 62% |
| **Hard set** (long-tail, NE-driver, čisti L3, method-ON) | 138 | **19.6%** | 52.9% | admin 26% / manager 19%; GET 32 / POST 32 / **PUT 0 / PATCH 0** / DELETE 35 |
| Hard set, bez internal toolova (pošteno) | 118 | **22.9%** | **61.9%** | 20 "internal" tools scoper ispravno izbacuje |
| Hard set, **najteže** (čisti L3, BEZ method buckets) | 138 | 18.1% | 47.1% | method bucket pomaže tek ~5pp |

**Hard set** = 138 anchor-NEOVISNih upita (generator vidi samo intent_summary, ne anchore), balansirano GET/POST/PUT/PATCH/DELETE (stari benchmark imao **0 mutacija**), 69 distinct long-tail toolova koje stari benchmark NE pokriva.

---

## 3. Pošten zaključak (nijansirano)
**Svakodnevni put radi dobro; široki rep ne.**
- **Česti tools + driver self-info: ~70–83%.** Ono što Damirovi vozači/manageri rade najviše (km, registracija, lista vozila, rezervacije, ljudi, troškovi) — solidno.
- **Long-tail (ostalih ~920 toolova): ~20% p@1, ~53–62% recall@3.** Tvrdnja "radi na svih 950" NIJE pošteno ispunjena za rep.

**Gdje točno puca (iz `exp_in_top50` dijagnostike):**
- **Retrieval je zapravo OK** s pravim embeddingsima — `exp_in_top50=YES` dominira u promašajima → pravi tool (ili njegova "obitelj") JEST u top-50/top-3. recall@3 ~53–62%.
- **Usko grlo = finalni LLM pick:** gpt-4o-mini **prečesto odbija** (`no_tool_call`) kad se natječe više skoro-identičnih sibling toolova, i **bira sibling/sinonim**:
  - `post_Cases` vs `post_AddCase` (sinonimi — promašaj je sporan),
  - `..._documents` vs `..._documents/{documentId}` vs `..._thumb` (razlikuju se za 1 path-segment),
  - duplikat `intent_summary` (FAZA 15: 64 toola dijele opis "Postavlja dokument kao zadani…") → doslovno nerazlučivo iz kratkog upita.
- **Mutacije (PUT/PATCH) 0%** — najviše pogođene gornjim (document/SetAsDefault siblings + obscure).

**Tj. broj 20% je STROG (exact single-ground-truth).** Stvarna korisnost je bliže recall@3 (~53–62%): bot pokazuje top-3 karte, user bira; a dosta "promašaja" su 1-suffix-off siblings ili sinonimi koje bi čovjek smatrao točnima.

---

## 4. Flows + single-call + odluka (review)
- **Single API call** (Model-A): doseže `executor.execute` end-to-end, formatira odgovor. Radi.
- **Flows** (booking/mileage/case): step→confirm→execute, svaki ima ASK_CONFIRM prije mutacije (ORCH-1 threadao tenant). Rade.
- **Odluka flow-vs-single:** keyword `_guess_flow_name` (rezerv/upis/kvar…). **Fragilno na parafrazu** — "trebam vozilo za petak" bez "rezerv" → ne uđe u flow, padne na Model-A (3-turn). Nije kvar (graceful), ali je keyword-ovisno.
- **L2b krade ne-driver upite:** "lista svih vozila"→get_MasterData, "Unesi 30500 km"→get_MasterData (umjesto post_AddMileage). **Caveat:** bench NE modelira produkcijski intent-gate (intent_type bi "lista vozila" klasificirao kao "other" → preskočio L2b → L3). Pa je steal u benchu **pesimističan** vs prod — ali realan rizik ako intent_type fula.

---

## 5. Caveati (pošteno)
1. Hard-set upiti su **LLM-generirani** (proxy, ne pravi WhatsApp logovi). Gpt-4o-mini generira I rutira — moguć blagi bias.
2. **Single ground-truth** po upitu → sinonim/sibling tools koji su legitiman odgovor broje se kao promašaj → p@1 je **donja granica**.
3. "internal" tools (20) scoper ispravno izbacuje → nepošteno ih brojati; ex-internal je 22.9%.
4. method-filter ON daje routeru točnu metodu "besplatno" (optimistično, pretpostavlja da je user pogodio akciju u Turn 1).

---

## 6. Poluge za SLJEDEĆI krug (NE ovaj — Filip odlučuje nakon broja)
Bez namještanja/keyword-patcheva. Po procijenjenom ROI-u:
1. **Smanjiti LLM decline** (`no_tool_call`) — prompt/threshold tuning da gpt-4o-mini commit-a kad je obitelj toolova u top-50. Najveća poluga (retrieval već nalazi tool).
2. **Razlučiti duplikat `intent_summary`** (FAZA 15, 64 toola) + document/SetAsDefault siblings — bez toga su doslovno nerazlučivi.
3. **Anchor cache izvan `tests/`** + build-on-deploy ili shippan pravi cache (vidi §0 smell).
4. (opc.) bolji ground-truth: dopustiti više točnih toolova po upitu (sinonimi) za pošteniji p@1.
5. Mutacije (PUT/PATCH) ciljano — najslabije.

---

## 7. Što je ostalo (telefon/auth, iz prošlog kruga)
- **Live re-probe NSN matchanja** (+385…/0… formati) — blokirano dok M1 token cooldown ne prođe (throttlao se od mojih probe-ova). Logika unit-verificirana (1615 testova zeleno).
- **403 na mutacije** — netestabilno bez pravog write-a; smoke-test korak (1 kontrolirani write + revert).

**Promjene koda ovaj krug:** SAMO mjerni alat — dodan `--no-l2b` flag u `bench_router_e2e.py` (measurement-only, ne dira routing) + generiran `tests/benchmarks/expanded_benchmark_v2.json`. Nijedan `services/` routing file nije mijenjan.
