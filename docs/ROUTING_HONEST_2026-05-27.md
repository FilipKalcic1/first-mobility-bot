# Routing accuracy — prvi PRAVI e2e broj + fake-cache nalaz (2026-05-27)

Filip izabrao verificirati routing (najveći neprovjereni rizik). Mjereno `scripts/bench_router_e2e.py` (pravi router: scope→cosine→LLM-pick) na `expanded_benchmark_v2.json` (138 upita, balansirano GET/POST/PUT/PATCH/DELETE, admin/manager/internal-heavy).

## ⚠️ NALAZ #1: sve dosadašnje ovosesijske brojke bile su na LAŽNOM cacheu
`tests/benchmarks/router_anchor_cache.json` sadržavao je **8-dim NULE** umjesto pravih 1536-dim ada-002 vektora → cosine je vraćao **isti top-50 za svaki upit** (`top3` konstantan) → router nikad nije vidio prave kandidate. Prvi run je dao lažnih **0.7-4.3% p@1**. **I extraction-bench (41.7%) ranije ove sesije bio je na istom lažnom cacheu.** `_load_from_cache` ([anchor_index.py:177](../services/router/anchor_index.py#L177)) provjerava SAMO fingerprint (tool+phrase+deployment), **NE dimenziju** → učitao lažni blindno. → Maknuo lažni (`*.fake-dim8.bak`), rebuildao PRAVI (1536-dim, 950 tools).

## ✅ NALAZ #2: produkcija (Docker) NIJE slomljena ovim cacheom
Prod factory ([engine.py:1962](../services/v2/engine.py#L1962)) pokazuje cache na `tests/benchmarks/router_anchor_cache.json`, ALI: fake cache **nije u gitu** + **`.dockerignore` isključuje `tests/`** → ne ide u image → prod na prvom bootu **rebuilda PRAVI** cache (Azure embed ~1-2 min). Dakle lažni cache je kvario samo LOKALNA mjerenja.
**Dizajn-smell (popraviti):** (a) prod cache_path je unutar dockerignored `tests/` → cold-start rebuild SVAKI boot (~1-2 min, ~$0.01); (b) nema dim-provjere u `_load_from_cache` → bilo koji stale/fake fajl tiho lomi lokalne runove. Preporuka: premjesti cache izvan `tests/` (npr. `config/` ili data-dir) + dodaj dim-sanity check.

## PRAVI brojevi (real cache, 138 upita)
| Config | p@1 | recall@3 | scope_miss | route_miss |
|---|---|---|---|---|
| **A — prod-realistic** (L2b on, method-filter, persona=None) | **17.4%** (24/138) | **42.8%** (59) | 11.6% | 71% |
| **B — pure L3 stress** (no L2b, no method-filter) | **16.7%** (23/138) | **47.1%** (65) | 14.5% | 69% |

A≈B → broj je robustan (~17% p@1, ~45% recall@3).

**Po metodi (oba):** GET ~21-25% · POST ~29% · **PUT 0% · PATCH 0%** · DELETE ~35%.
**Po personi:** admin ~25% · manager ~14% · **internal 0%** · driver ~25% (samo 4 upita).

## Failure-modovi (iz route_miss uzoraka)
1. **PUT/PATCH = 0%** (56 od 138 upita) — partial-update toolovi (`patch_X_id`) imaju generičke `intent_summary` ("Djelomično ažurira stavku") → 271-duplikat problem. **Najveći fixabilni lever.**
2. **internal = 0%** (20 upita) — ciljaju internal-tagged toolove koje `drop_internal` makne → scope_miss. Nedrivabilni po dizajnu (ne bi trebali biti u user-facing evalu).
3. **L2b over-fire** — driver-basics shortcut hvata NE-driver upite ("koji su poolovi", "tipovi vozila") → `get_MasterData` (krivo). U prod je gated intent-klasifikatorom (bench ga ne modelira), ali matcher je pregladan.
4. **`no_tool_call` pervaziван** — gpt-4o-mini odbija pozvati tool i kad je expected u top-3 (`exp_in_top50=YES`). Routing nestabilnost.

## Iskreni caveati (ne preprodajem)
- **Set je SINTETIČKI (LLM-gen), NE pravi promet.** I admin/manager/internal-heavy (samo 4 driver upita) → ovo je TEŠKI long-tail, NE reprezentativan stvarnog prometa (koji dominiraju driver upiti: km/registracija/rezervacije — njih L2b + jednostavniji routing bolje hvataju). **Pravi prod broj za tipične upite je vjerojatno VIŠI.**
- **Neki labeli upitni** (npr. "dajte mi popis vozila" → exp=get_VehicleTypes; trebalo bi get_Vehicles) → stvarna točnost na dobro-labeliranim upitima je nešto viša.
- gpt-4o-mini nedeterministički → 1 run je indikacija.

## Odluka: curated subset (potvrđeno brojem)
recall@3 ~45% na teškom long-tailu + p@1 ~17% → **NE izlagati svih 950 free-text routingu za produkciju.** Suzi na **curated subset (driver-basics + česti manager/admin daily-use, ~30-50 toolova)** gdje osiguramo kvalitetu + mjerimo; širi kako se popravlja. To je preporuka iz `SYSTEM_GAPS` — sad s pravim brojem.

## Konkretni leveri (po vrijednosti)
1. **PUT/PATCH 0% → prepiši generičke intent_summary** za partial-update toolove (271-duplikat). Najveći dobitak.
2. **Tighten L2b gate** (ili se osloni na prod intent-klasifikator) — smanji over-fire na ne-driver.
3. **Premjesti anchor cache izvan `tests/` + dim-sanity check** (dizajn-smell gore).
4. **Curated subset** — pragmatičan prod odgovor sad.
5. Pravi broj tek iz **live telemetrije** (post-smoke) — sintetički set je proxy.

## Reprodukcija
```
python scripts/bench_router_e2e.py --benchmark-file tests/benchmarks/expanded_benchmark_v2.json            # A
python scripts/bench_router_e2e.py --benchmark-file tests/benchmarks/expanded_benchmark_v2.json --no-l2b --no-method-filter  # B
```
(Treba Azure. Real anchor cache se rebuilda ako fali — ~1-2 min prvi put.)

---

## ⭐ TOČAN route_miss breakdown + per-step (instrumentirano, re-run 2026-05-27)
Filip: "Napravi taj točan breakdown!" — bench je dotad printao samo `route_misses[:14]` (uzorak), pun popis se odbacivao. Dodan `Counter` nad PUNIM `route_misses` ([bench_router_e2e.py](../scripts/bench_router_e2e.py) summary blok, s `sum == route_miss` self-checkom) → TOČNI brojevi. Taksonomija (međusobno isključiva; `in_top50` provjeren PRVI jer je retrieval upstream uzrok):
- `retrieval_miss` — exp NIJE u top-50 (anchor cosine ga nikad nije izbacio).
- `no_tool_call` — exp JE u top-50, ali LLM nije pozvao nijedan tool.
- `wrong_pick` — exp JE u top-50, LLM izabrao DRUGI.
- `wrong_shortcut_L2b` — L2b driver-basics shortcut opalio na ne-MasterData upit.

### TOČNE brojke (real cache, 138 upita)
| Config | p@1 | recall@3 | scope_miss | route_miss | no_tool_call | wrong_pick | retrieval_miss | wrong_shortcut_L2b |
|---|---|---|---|---|---|---|---|---|
| **A** (L2b on, method-filter on) | 20 (14.5%) | 48 (34.8%) | 17 | **101** | **54** | 5 | 15 | 27 |
| **B** (no L2b, no method-filter) | 21 (15.2%) | 64 (46.4%) | 20 | **97** | **76** | 11 | 10 | 0 |

Self-check: A 54+5+15+27=101 ✓ · B 76+11+10+0=97 ✓ (poklapa route_miss).

**GLAVNI NALAZ: `no_tool_call` DOMINIRA.** Od L3-dosegnutih promašaja: A 74 (od toga 54 no_tool_call = **73%**), B 97 (76 = **78%**). LLM odbije pozvati IJEDAN tool iako je expected u top-50 koji vidi. `wrong_pick` sićušan (5/11), `retrieval_miss` umjeren (15/10) → **anchor NIJE glavno usko grlo** (surface-a expected u ~85-90%).

**Root cause `no_tool_call` (u kodu):** [llm_router.py:253](../services/router/llm_router.py#L253) `tool_choice="auto"` + system prompt **pravilo #4** ([:204-205](../services/router/llm_router.py#L204)) doslovno kaže *"Ako NIJEDAN tool ne pristaje, NE pozivaj nijedan tool"* → model eksplicitno POZVAN da odbije → `error="no_tool_call"` ([:295-299](../services/router/llm_router.py#L295)).

### Per-step breakdown (gdje pipeline puca)
| Step | Bench modelira? | Doprinos (A / B) | Bilješka |
|---|---|---|---|
| **L2a** intent klasifikator | ❌ NE (ide ravno na L2b/L3) | nemjereno | u prod bi mis-klasifikacija misroutala; bench preskače → caveat |
| **L2b** driver_basics shortcut | ✅ samo A | 27 / 0 krivo | SVIH 27 L2b fires krivo (eval ~nema get_MasterData). Matcher pregladan: "koji su poolovi", "dajte mi popis vozila", "tipovi vozila" → krivo `get_MasterData`. U prod gated L2a klasifikatorom (niže nego 27) |
| **action-picker** / method filter | ⚠️ pretpostavljen SAVRŠEN (bench uzme metodu expected toola) | 0 mjereno | realni user-pick akcije dodaje grešku koju bench NE hvata → caveat; B (filter off) ima čak VIŠI recall@3 (46% vs 35%) jer L2b ne krade |
| **scope** (CatalogScoper) | ✅ | 17 / 20 scope_miss | SVE internal-persona upiti na internal-tagged toolove koje `drop_internal` makne (internal persona = 0% p@1, 20 upita). Eval-set artefakt, NE pravi prod-fail |
| **anchor** cosine top-50 | ✅ | 15 / 10 retrieval_miss | surface-a expected u ~85-90% → NIJE glavno usko grlo |
| **LLM pick** (llm_router) | ✅ | **no_tool_call 54/76** + wrong_pick 5/11 | **#1 usko grlo** — `tool_choice="auto"` + pravilo #4 |

### Po metodi / personi (oba runa)
- **PUT 0/28 · PATCH 0/28 = 0%** u OBA runa (56 upita, 0 točnih). GET 21-29% · POST 25% · DELETE 19-31%.
- admin 19-23% · manager 12-14% · **internal 0%** (sve scope_miss) · driver 25% (samo 4 upita).
- **Veza PUT/PATCH ↔ no_tool_call (INFERENCIJA, ne egzaktno):** 56 PUT/PATCH = 0% najvjerojatnije pada uglavnom u `no_tool_call` — generički/duplikat intent_summary (271-problem) → LLM ne može pouzdano odabrati → odbije. Dakle dva levera su KOMPLEMENTARNA, ne konkurentska.

### Iskreni caveati
- **gpt-4o-mini run-to-run varijanca:** ovaj Run A = 14.5%/34.8%; prošli (gornja tablica gore u docu) = 17.4%/42.8%. ISTI cache, ISTI upiti — razlika je nedeterminizam LLM tool-calla (`no_tool_call` odluka varira). **Struktura (no_tool_call dominira) je robusna** preko obje runde + 14-uzorka; APSOLUTNE brojke tretiraj ±nekoliko.
- Set je SINTETIČKI, admin/manager/internal-heavy (4 driver upita) — NE reprezentativan stvarnog (driver-dominantnog) prometa. Pravi prod broj za tipične upite vjerojatno viši.
- L2a i action-picker NISU modelirani → njihov doprinos nemjeren.

### Lever (CLAUDE.md) — ZASEBAN sljedeći korak, NE ovaj krug (mjeri-prvo)
1. **Konkretno:** napadni `no_tool_call` prvo (najveći bucket, 54-76). Eksperiment: `tool_choice="auto"` → `"required"` ([llm_router.py:253](../services/router/llm_router.py#L253)) + omekšaj/makni pravilo #4. Jer je exp u top-50 za ~85-90% no_tool_call slučajeva, forsiranje pretvara većinu → točno ili u top-3 (recall@3 skoči). Komplementarno: prepiši generičke PUT/PATCH intent_summary.
2. **Slabost:** `required` miče "Ne razumijem" → istinski out-of-scope upit sad izabere KRIVI tool. Ublaženo top-3 pickerom + mutation-confirm; recall@3 (što cascade pokaže) je bolja mjera od p@1.
3. **Alternativa:** ostavi `auto`, samo prepiši intent_summary — pomaže `wrong_pick` (sićušan) + dio PUT/PATCH, ali NE rješava glavninu no_tool_calla.
4. **Trošak ako pogriješiš:** mjereno na 1 runu po configu (LLM varira) — prije commitanja `required` re-run 2-3× za stabilan pomak; inače optimiziraš na šum.

Bench summary sad printa: `route_miss breakdown: no_tool_call=X wrong_pick=Y retrieval_miss=Z wrong_shortcut_L2b=W (sum=... == route_miss=...)`.

---

## ✅ FIX: `tool_choice="required"` + LLM-pick vodi picker (2026-05-27)
Najveći lever iz breakdowna (`no_tool_call` = #1 bucket) napadnut. Tri promjene:
- [llm_router.py:253](../services/router/llm_router.py#L253): `tool_choice="auto"` → **`"required"`** (LLM mora odabrati iz top-50, ne smije odustati) + prompt **pravilo #4** omekšano ("UVIJEK odaberi najbliži tool, korisnik potvrđuje").
- [engine.py ACTION_GLOBAL](../services/v2/engine.py): **LLM-ov pick vodi 3-card picker (kartica #1)**, anchor popuni #2-3 (dedup). Prije: picker = anchor top-3, LLM pick služio SAMO za pre-fill parametara (skoro dekorativan). Sad LLM-ovo čitanje intent_summary stvarno dođe do korisnika.
- [bench_router_e2e.py](../scripts/bench_router_e2e.py): recall@3 mjeri novu kompoziciju kartica.

### Before → After (real cache, 138 upita, 4 runa)
| Config | p@1 prije | **p@1 poslije** | recall@3 prije | **recall@3 poslije** | no_tool_call prije→poslije |
|---|---|---|---|---|---|
| **A** (L2b on, method-filter on) | 14.5% | **37.0%** (A1=A2) | 34.8% | **47.1%** | 54 → **0** |
| **B** (no L2b, no method-filter) | 15.2% | **31.9–33.3%** (B1/B2) | 46.4% | **48.6–49.3%** | 76 → **0–1** |

**Po metodi (Run A poslije):** GET 28.6→35.7% · POST 25→46.4% · **PUT 0→14.3%** · **PATCH 0→39.3%** · DELETE 19.2→50.0%. **Po personi:** admin 19.4→50% · manager 13.5→34.6% · driver 25→50% · internal 0% (scope_miss, nepromijenjeno).

**Stabilnost:** `required` uklonio i run-to-run varijancu (A1≡A2; B1≈B2 ±1.4pp) — jer je nestabilni dio bila baš decline-odluka.

### Iskreni caveati (ne preprodajem)
- **`wrong_pick` PORASTAO** (A 5→39, B 11→64) — to je cijena forsiranja: gdje je LLM prije odustao, sad pogriješi pick. ALI: (a) ide u **top-3 picker** (korisnik bira, NE auto-execute), (b) net p@1 ~udvostručen jer su mnogi forsirani pickovi točni. Za cascade UX je strogo bolje (korisnik uvijek dobije 3 opcije umjesto "ne razumijem").
- **`retrieval_miss` NETAKNUT** (clean Run B: 10→10) — `tool_choice` ne dira anchor stage. Promjena NE popravlja retrieval; to ostaje zaseban lever. (Run A `retrieval_miss=0` je L2b-apsorpcija, ne pravi pomak.)
- Mjereno na **teškom sintetičkom long-tailu** (4 driver upita) → najgori slučaj; pravi driver-promet vjerojatno viši.
- **`wrong_shortcut_L2b` (27-32) sad najveći preostali bucket u Run A** → sljedeći lever (gate L2b / osloni se na L2a u prod).

### Sigurnost promjene
`route()` se zove SAMO iza action-pickera ([engine.py:982](../services/v2/engine.py#L982), `PENDING_STAGE_ACTION_GLOBAL`) — korisnik je već odabrao akciju, pa je "ništa ne pristaje" rijetko; rezultat ide u picker (ne auto-execute) + mutation-confirm ostaje. Unit testovi zeleno (101 picker/engine + 10 router; `test_llm_router` ažuriran na `required`).
