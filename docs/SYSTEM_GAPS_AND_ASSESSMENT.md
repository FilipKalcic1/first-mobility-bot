# SVE rupe u sustavu + procjena pristupa — 2026-05-24

**Pitanje (Filip):** "ako znaš sve rupe, napravi dokument sa apsolutno svima + procijeni je li naš pristup adekvatan i bi li tako trebao izgledati?"

**Što je ovo:** konsolidacija SVIH poznatih rupa iz cijele sesije + 3 honest audit doca ([ACCURACY_HONEST](ACCURACY_HONEST_2026-05-24.md), [EXTRACTION_HONEST](EXTRACTION_HONEST_2026-05-24.md), [PARAM_INPUT_AUDIT](PARAM_INPUT_AUDIT_2026-05-24.md)). Read-only — nije mijenjan kod. Cilj: jedan popis bez prikrivanja + iskrena procjena je li arhitektura adekvatna.

> **Napomena o brojkama bez body-scheme** (da nema privida nekonzistentnosti): tri broja mjere **različite rezove** istog problema:
> - **159** = `risky_tools.json` (8 hard-missing + 151 likely-missing) — operativni FAZA 8/9 broj.
> - **164** = toolovi čiji je body `array`/`primitive`/`none` (nije named-fillable) — PARAM_INPUT_AUDIT.
> - **196** = mutacije bez **ijednog** popunjivog named body parama (uklj. `patch_X_id`) — PARAM_INPUT_AUDIT.
> Preklapaju se, ali nisu isti skup. Citiram svaki s definicijom umjesto da ih spajam u jedan broj.

---

## ✅ RIJEŠENO (da popis bude pošten, ne samo doom)

- **Identitet:** telefon NSN-match (3 formata: `385…`/`+385…`/`0…`), `company_id`/`orgunit_id` capture iz `/Persons`, executor context-injection, completeness guard (`missing_required` refuse umjesto tiho-422). Live-verificirano da `/Persons` vraća CompanyId/OrgUnitId.
- **Param misclassification:** 55 krivo-tagiranih context params demotano na user_input (27 non-string + ~28 string value-polja; 14 ih bilo required → garantirano loš poziv) + parser guard (`_context_value_appropriate`) protiv buduće regresije.
- **Injectability:** 235 context params (vehicle 84 / person 42 / tenant 81 / company 18 / orgunit 10) — svi injectabilni iz identity. 0 bez ključa, 0 izvan identity-5.
- **Anti-fabrikacija:** `tool_schema_builder` emitira `required: []` LLM-u (required-enforcement ostaje preko registryja → param-ask) → LLM ne nagađa required (e06 `AssigneeType:1` nestao).
- **Ostalo:** bool "ne"→False; pagination se ne nudi; filter (registracija) + confirm; ranije EDGE/ORCH/EXE/GW fixevi (PII scrub, mutation-confirm, execution-lock, SSRF guard, hallucination-check).
- **1621 testova zeleno.**

---

## 🔴 OTVORENE RUPE (po kategoriji + severity)

### A. Operativno — bot NIKAD nije bio live (HIGH, temelj)
- Nijedna prava WhatsApp poruka nikad poslana end-to-end (smoke test nikad prošao).
- M1 OAuth token **trenutno zaključan** (`invalid_client`) + token-endpoint throttla na brze fetcheve (prod ublažen Redis cacheom, ali osjetljivo).
- Azure/Infobip prod creds + quota **neverificirano**.
- Live e2e za telefon/company_id fix **odgođen** (token zaključan).

### B. Routing — najveći FUNKCIONALNI jaz (HIGH)
- **Long-tail (~920 ne-čestih toolova): p@1 ~20%, recall@3 ~53–62%.** Česti/driver tools: ~70–83% (driver 83% / manager 53% / admin 40%).
- **Router NESTABILAN run-to-run** (12/12 → 5/12 na istim jasnim by-id upitima; gpt-4o-mini `no_tool_call` odbija i jasne delete/izmijeni upite). **#1 blocker za točan tool-pick.** A/B dokazao da NIJE od `required:[]` promjene — to je gpt-4o-mini decline-variance na ~99-toolnom bucketu.
- **Usko grlo = finalni LLM pick, NE retrieval** — `exp_in_top50=YES` dominira u promašajima (pravi tool JEST u top-50). LLM bira sibling/sinonim ili odbija.
- **Sibling/duplikat ambiguity:** 64 toola dijele `intent_summary` (npr. "Postavlja dokument kao zadani…") → doslovno nerazlučivi iz kratkog upita; document/`{id}`/thumb razlikuju se za 1 path-segment.
- **Mutacije (PUT/PATCH) ~0%** u hard-setu — najpogođenije gornjim.
- **Flow-vs-single odluka** je keyword-based (`_guess_flow_name`: rezerv/upis/kvar…) → fragilna na parafrazu ("trebam vozilo za petak" bez "rezerv" → ne uđe u flow; graceful pad na Model-A, ali keyword-ovisno).
- **L2b driver shortcut** zna ukrasti ne-driver upite ("lista vozila"→get_MasterData). Gated intent_type-om u prod, ali rizik ako intent_type fula.

### C. Param-input strukturne granice (MED — auditirano, NIJE popravljivo ekstrakcijom)
- **37 `*_multipatch`** (required `array` body) — primaju array patch-operacija; user ne tipka JSON-array u WhatsApp.
- **196 mutacija bez popunjivog named body** (uglavnom `patch_X_id` partial-update + nekoliko `Sync*`/`Metadata_Order`/`ResendInvitation`) → bot ne može sklopiti body generički.
- Toolovi s required poljem bez enuma/hinta (npr. `AssigneeType`) → pitamo, ali user ne zna → ne završi.

### D. Backend DATA / metadata strop (HIGH — uzrok ~pola problema, NIJE naš kod)
- **0 enuma** na svih 3938 params → LLM nikad ograničen na dozvoljene vrijednosti. Provjereno na 3 živa Swaggera (1239 ops): **M1 Swagger NEMA enume** → backfill iz Swaggera nemoguć.
- **164 toola bez upotrebljive body-scheme** (array/primitive/none) — bot ne zna koja polja postoje.
- **64 toola duplikat `intent_summary`** → router ih ne razlikuje.
- Samo **26% required params ima `description`** → LLM bez hinta.

### E. Execution / API contract (MED)
- **403 na mutacije netestiran** (OAuth client write-scope nepoznat). Bot nikad nije napravio pravu mutaciju.
- Reaktivni 4xx→HR (`ApiErrorTranslator`) **pokriva, ne sprječava** lošu vrijednost.

### F. Identity — data caveat (MED)
- M1 sprema Phone u 3 nekonzistentna formata; **većina demo-osoba ima null Phone** → neprepoznati (data-entry gap, ne kod).
- `TenantRoles` prazan → role-based persona mrtva (launch koristi `persona=None`).

### G. Infra / ops (MED/LOW, deferred)
- **GW-A2:** token plaintext u Redis → treba AUTH+TLS u prod.
- **Anchor cache path je UNUTAR `tests/`** ([engine.py:1893](services/v2/engine.py#L1893)) — stale/fake fajl tamo lomi L3 (već uhvaćen fake dim-8 cache koji je dao lažnih 5.8%). Treba ga premjestiti izvan `tests/` + build-on-deploy.
- **EDGE-4:** DLQ write-only (nema consumera). **DATA-2:** nema Alembic migracija. (+ LOW: EDGE-7/8, EXE-2/3, GW-A1/3/5.)

### H. Mjerenje (honest caveat)
- Benchmarki su **sintetički / LLM-generirani** (proxy, ne pravi WhatsApp logovi); gpt-4o-mini i generira i rutira → moguć blagi bias.
- **Single ground-truth** po upitu → sinonim/sibling koji su legitiman odgovor broje se kao promašaj → p@1 je **donja granica** (stvarna korisnost bliže recall@3).
- Ekstrakcija mjerena samo na **čistoj površini** (id/broj/registracija); datumi/enumi/multi-filter nemjereni.

---

## 🧭 PROCJENA: je li pristup adekvatan + bi li tako trebao izgledati?

**Arhitektura/mehanika — DA, adekvatna i industry-standard.** Data-driven registry → RAG retrieval (anchor cosine top-50) → LLM tool-call → hallucination-check → confirm-before-mutate → generička param-rezolucija (extract/ask/inject) → PII/guards. Kod je čvrst (1621 test), verificirano **nije namješten** (0 keyword-forsiranja toola u L3). **Pipeline TREBA tako izgledati.**

**ALI ambicija "jedan bot pouzdano vozi SVIH 950 tools iz free-text WhatsAppa" — NE s trenutnim podacima/routingom.** Iskreno:
- Routing 1-od-~99 u bucketu je nepouzdan (~20% long-tail) + nestabilan (decline-variance).
- ~Pola tool-baze strukturno/data nije chat-drivabilno (164 no-body, 37 multipatch, 196 no-fill, 0 enuma).
- Nikakva "pamet koda" to ne prelazi — strop je **backend data + retrieval/LLM-pick pouzdanost**.

### Preporuka (CLAUDE.md format)

1. **Konkretno:** zadrži arhitekturu; **SUZI produkcijski scope na curated subset (30–50 daily-use tools)** preko postojećeg `tool_subset.json` mehanizma (već postoji!) — driver basics, rezervacije, km, ključne manager liste — gdje OSIGURAMO metadata + mjerimo accuracy. Širi kako backend obogaćuje Swagger. Paralelno napadni router-stabilnost (decline na jasnim upitima — najveća poluga jer retrieval VEĆ nalazi tool).

2. **Glavna slabost preporuke:** "950 tools" zvuči bolje za prezentaciju; sužavanje priznaje da ne pokrivamo sve odmah + curated subset traži ručni izbor i održavanje (novi daily-use tool treba dodati ručno dok ga telemetrija ne otkrije).

3. **Alternativa (drugi prioritet):** ostani na 950, prihvati ~20% long-tail + 3-turn cascade + reaktivni error-translate kao "degraded ali safe"; uloži u backend Swagger obogaćivanje (enumi/body/opisi) da digneš strop. Sporije, ovisi o Damirovom timu, ali ne traži scoping odluku.

4. **Trošak ako pogriješiš:** guraš 950 bez sužavanja → korisnici udaraju u ~20% long-tail (krivi tool / nepotpun poziv) → "ne valja AI", gubitak povjerenja (teško vratiti). Presuziš → neki legitiman upit padne na "ne mogu" — ali to je **SIGURNO** (refuse, ne smeće) i fixabilno širenjem subseta.

**Bottom line:** pristup je dobro **DIZAJNiran**, ali trenutno preširoko **SKALIRAN** za kvalitetu podataka koju imamo. Adekvatno za produkciju = isti pipeline + uži, metadata-čist scope + mjerenje + postupno širenje. To **NIJE rewrite** — to je scoping odluka (config) + nastavak već započetih fixeva (router-stabilnost, metadata-dedup).

---

## Najveće poluge za sljedeći krug (po ROI-u, Filip odlučuje)
1. **Smanjiti LLM decline** (`no_tool_call`) — prompt/threshold tuning da gpt-4o-mini commit-a kad je obitelj toolova u top-50. Najveća poluga (retrieval već radi).
2. **Razlučiti 64 duplikat `intent_summary`** + document/SetAsDefault siblings.
3. **Curated `tool_subset.json`** (30–50 daily-use) + metadata-čišćenje samo tog seta.
4. **Anchor cache izvan `tests/`** + build-on-deploy.
5. **Backend ask:** M1 doda enume/opise/body-scheme u Swagger — jedini pravi long-tail accuracy lever.

**Promjene koda ovaj krug:** NEMA — ovo je read-only konsolidacija. Nije commitano.
