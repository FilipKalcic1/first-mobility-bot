# Plan do 10/10: Sigurna konvergencija na `/actions` arhitekturu

> **Status:** Glavni inženjerski plan refaktorizacije. Zamjenjuje Gemini "Aggressive
> Purge" plan — isto odredište (tanki bot nad ~30 akcija), ali put koji **ne ubija
> radni sustav**.
> **Sve tvrdnje provjerene protiv koda** (file:line), ne po mišljenju.
> **Datum:** 11.06.2026.

---

## 0. Iskrena ocjena (re-grade)

Gemini je dao **3/10 trenutnom sustavu** i **10/10 svom planu brisanja**. Oboje je
krivo. Evo poštene ocjene s tri odvojene stvari koje se ne smiju brkati:

| Što | Ocjena | Obrazloženje |
|---|---|---|
| **Trenutni sustav** | **7/10** | Radi, 1749 testova zeleno, ruff čist, deployable (k8s). Problemi su **popravljivi** (accuracy strop zbog 950-kataloga), ne fatalni. NIJE 3/10. |
| **Gemini plan (doslovno izvršen)** | **2/10** | Točno odredište, ali briše radni sustav prije zamjene → mrtav, nesiguran, pravno izložen bot. Auditor koji predlaže brisanje bez testova/rollbacka/sekvence sam piše nizak plan. |
| **Ciljna arhitektura** (nakon konvergencije) | **10/10** | Tanki bot nad ~30 `/actions`, svi safety slojevi netaknuti, prod-dokazan. Do toga se **stiže fazno**, ne big-bangom. |

**Ključna istina:** Gemini je pobrkao **ŠTO** (odredište — točno) s **KADA**
(sada — fatalno). Sve njegovo brisanje vrijedi tek **na kraju**, kad `/actions`
sloj živi. Raditi ga sada = mrtav bot u sredini.

---

## 1. Zašto Gemini "Aggressive Purge" ruši sustav (dokazani blast-radius)

Provjereno grep/read protiv koda:

| Gemini briše | Što se SLOMI | Dokaz (file:line) |
|---|---|---|
| `config/processed_tool_registry.json` | **Bot se ne diže** — registar je obavezan na bootu | `services/registry/__init__.py:96-101` → ako file fali, `return False` |
| `pending_mutation.py` | **Da/Ne confirm gate** (koji Gemini "zadržava" kao `mutation_gate.py`!) | `engine.py:2084/2164/2278` spremaju u `pending_mut_store`, `:326/2210` čitaju u `_continue_pending_mutation`. Bez store-a, gate je mrtav. **Samokontradikcija.** |
| `crisis_detector.py` | **Suicid → hotline redirect** (etička/pravna obveza) | `engine.py:427` (`crisis_detector.detect`), `:435` (`return crisis.response`) — u živom `_dispatch_message` |
| `special_intents.py` | **GDPR brisanje/izvoz** (zakonska obveza) + welcome/handover | `engine.py:495` + `_handle_special_side_effects` (gdpr_audit) |
| `pending_params.py` + `param_ui.py` | **Višeturni param collection** ("za koji datum?" → zapamti odgovor) | Geminijev VLASTITI opis (Sloj 2) kaže bot mora pitati; 1 stateless LLM poziv ne pamti kroz turnove |
| 11 modula iz `services/v2/` | **11 test fileova** iz 1749-zelene suite | Glob potvrdio: `test_crisis_detector.py` … `test_pending_clarify.py` (svih 11) |
| `k8s/` + "vrati ngrok" (korak 5) | **Regresija** s produkcijskog deploya na laptop+ngrok | `k8s/` = deploy path koji smo upravo izgradili |
| `config/tenants/` | Catalog scoping (per-tenant suženje) | `services/router/catalog_scoper.py` (gubi se suženje → veći pool → niža accuracy) |

### "Ruthless Pruning po jednoj live poruci" — metodološki katastrofa
Jedna poruka izvrši **~1 code-path (~5% koda)**. Brisati sve što "nije sudjelovalo"
= obrisati error-handling, outbound DLQ, circuit breaker, rate-limit, idempotency,
sve OSTALE namjere i kanale — dakle svu otpornost koju smo u auditu **upravo
popravili**. "Bez ijedne milisekunde idle" **nije** metrika kvalitete koda.

---

## 2. Što je Gemini TOČNO pogodio (zadržati kao ODREDIŠTE)

Da budemo pošteni — vizija je dobra, samo je krivo tempirana:

- ✅ End-state bot je **tanak** (~6-8 slojeva nad ~30 akcija).
- ✅ **Stage A (anchor retrieval) postaje nepotreban** — ALI tek kad katalog padne
  na ~30 (30 alata stane direktno u LLM; 950 fizički ne stane u kontekst).
- ✅ Neki intent slojevi se **kolabiraju u 1 strukturirani poziv** — tek kad je
  katalog ~30.
- ✅ **Viber adapteri** (skice ~točne, uz prave Infobip/HMAC detalje).
- ✅ Tenant/linguistic concerns **eventualno** sele na backend.

> Svaka od ovih je istina za **odredište**, ne za **danas**. Plan ispod ih sve
> ostvaruje — sigurnim redoslijedom.

---

## 3. Ispravljeni Manifest (3 liste umjesto "obriši sve")

### 3A. SIGURNO obrisati DANAS (stvarno mrtav kod — Faza 0)
Iz `docs/SUSTAV/15_LIVE_VS_DEAD.md` — realan kod koji **NIJE** u živom import-putu:
```
services/v2/confidence_gate.py     # + test
services/v2/active_learning.py     # + test + scripts/run_active_learning.py
services/v2/anchor_audit.py        # + test
```
Postupak: grep-potvrdi da nisu importani u `engine.py`/`worker.py`/`webhook_simple.py`/
`main.py` → obriši modul **i** njegov test → `pytest` zelen → commit. **Reverzibilno.**

### 3B. NIKAD ne brisati (non-negotiable — safety/legal/ethical/resilience)
```
crisis_detector.py        # suicid → Plavi telefon (etika/pravo)
pii_scrubber.py           # OIB/IBAN prije LLM-a (GDPR)
input_sanitizer.py        # prompt-injection obrana
special_intents.py        # GDPR brisanje/izvoz (zakon) + welcome/handover
mutation_gate.py + pending_mutation.py   # Da/Ne confirm gate (oboje, neodvojivo)
pending_params.py + param_ui.py          # višeturni param collection
identity.py               # tenant izolacija (sigurnost)
rate_limiter.py           # anti-spam/DoS
api_gateway resilience    # DLQ, circuit breaker, idempotency, SSRF guard
k8s/                      # produkcijski deploy (zamjena za ngrok)
```

### 3C. Obrisati TEK na kraju (Faza 4 — kad `/actions` pokriva sve high-frequency)
```
Stage A (anchor retrieval) u llm_router.py     # nepotreban tek s ~30 alata
SHA1 alias za >64-znak imena                    # nepotreban tek s kratkim imenima
950 objekata u tool_data.json → ~30 akcija      # tek kad bot ruta na /actions
intent_type/driver_basics/... kolaps            # tek kad je katalog ~30
processed_tool_registry.json                    # tek kad ga registry više ne čita
```

---

## 4. Ciljna struktura (10/10 kostur) — ispravljeni Gemini skeleton

Gemini skeleton je bio **preagresivan** (izbacio safety slojeve). Ispravak — što
**ostaje** u end-stateu (nakon Faze 4), s razlogom zašto svaki sloj preživljava:

```
mobilityone-whatsapp-bot/
├── config/
│   ├── tool_data.json               # ~30 akcija (NAKON Faze 4; danas 950)
│   └── entity_translations_hr.json
├── services/
│   ├── router/llm_router.py         # bez Stage A (NAKON Faze 4)
│   ├── formatter/llm_formatter.py
│   ├── v2/
│   │   ├── engine.py                # ~8 slojeva (ne 6 — safety ostaje)
│   │   ├── rate_limiter.py          ├── pii_scrubber.py
│   │   ├── input_sanitizer.py       ├── identity.py
│   │   ├── crisis_detector.py  ⟵ Gemini je htio obrisati (NE)
│   │   ├── special_intents.py  ⟵ GDPR (NE)
│   │   ├── mutation_gate.py + pending_mutation.py  ⟵ confirm gate (NE)
│   │   ├── pending_params.py + param_ui.py         ⟵ param collection (NE)
│   │   ├── executor.py              # async preko api_gateway (executor.py:90/187)
│   │   └── telemetry.py
│   ├── api_gateway.py  ├── token_manager.py  ├── admin_auth.py
├── k8s/                ⟵ NE brisati (produkcija)
├── webhook_simple.py   ├── worker.py
├── Dockerfile          └── docker-compose.yml
```

**Razlika od Geminija:** ~8 slojeva, ne 6. Razlika su 4 safety/legal sloja koja
Gemini briše a koja su zakonom/etikom/sigurnošću obavezna. To je razlika između
"izgleda tanko" i "tanko a sigurno".

---

## 5. Strangler Fig faze (pravi put — detaljno)

**Princip:** novo PORED starog (feature-flag); migriraj sposobnost-po-sposobnost;
staru putanju briši TEK kad je nova **dokazana u produkciji**. Ništa se ne briše
prije nego mu replacement živi + testiran.

**⚠️ GATE:** cijela Faza 1+ ovisi o **World A/B odluci (ponedjeljak — tko gradi
`/actions`)**. Bez nje krećeš samo Fazu 0.

### FAZA 0 — Sigurno čišćenje + Viber (SADA, ne ovisi o nikom)
- Obriši stvarno mrtav kod (lista 3A) — modul + test, suite zelena, commit.
- **Viber adapter** (channel-agnostični mozak ostaje netaknut):
  - `webhook_simple.py`: dodaj `"channel"` u `stream_data` (oba mjesta ~:514/:546);
    novi inbound parse za Viber payload (Infobip multichannel).
  - `worker.py`: granaj `_enqueue_outbound`/`_send_whatsapp` (~:1296) po `channel` tagu.
  - Pravi Infobip Viber API + HMAC verify (Geminijeve skice su približne, ne doslovne).
  - **Test:** novi `tests/` za channel-routing; suite zelena.
- **Exit kriterij:** suite zelena, Viber e2e radi, mrtav kod nestao. Bot i dalje 100% radi.

### FAZA 1 — Prvih ~5 `/actions` PORED postojećeg (po World A odluci)
- Damir izloži prvih ~5 akcija (`book_vehicle`, `add_mileage`, `report_incident`,
  `list_trips`, `vehicle_status`) — mapirane na tvojih ~20 high-frequency.
- U bot dodaj **"action mode" iza feature-flaga** (`V2_USE_ACTIONS=1`), PORED
  postojećeg 950-routera. Stari put ostaje default dok novi nije dokazan.
- **Repointaj svoja 3 flowa** (booking/mileage/case — već imaš orkestraciju u
  `flow_engine.py` kao gotov predložak) na `/actions/*` umjesto granularnih poziva.
- `executor.py` dobije granu: action → `POST /actions/{name}` (async, postojeći
  `api_gateway.call` + `TokenManager.get_token()` + Idempotency-Key + x-tenant).
- **Confirm gate OSTAJE** za write akcije (Da/Ne prije `/actions/report-incident`).
- **Mjeri:** benchmark (`bench_router_e2e.py`) na oba seeda — action-mode vs stari.
- **Exit kriterij:** 5 akcija na flagu, accuracy ≥ stari put, suite zelena.

### FAZA 2 — Migracija sposobnost-po-sposobnost
- Za svaku iduću sposobnost: dodaj `/actions` def u config, izmjeri, uključi iza flaga.
- Sibling kolizije (`delete_X` vs `delete_X_id`) **strukturno nestaju** kako
  efektivni katalog pada (LLM bira iz N akcija, ne 950).
- Svaka migracija: **suite zelena + benchmark zabilježen u CHANGELOG**. Regresija = revert te jedne akcije.

### FAZA 3 — Kirurško povlačenje stare putanje (PO sposobnosti)
- Kad je sposobnost **dokazana u PRODU** (pilot, telemetrija) → ukloni njenu staru
  granularnu putanju + pripadne testove. Brisanje je sad **sigurno jer replacement živi**.
- Ovo je jedino mjesto gdje se uopće briše live kod — i to po jednoj sposobnosti, s testovima.

### FAZA 4 — End-state tanjenje (TEK kad SVE high-frequency na `/actions`)
- Sada su Geminijeva brisanja **konačno sigurna**:
  - ukloni **Stage A** (anchor retrieval) iz `llm_router.py` — katalog je ~30, sve ide direktno LLM-u;
  - smanji `tool_data.json` 950 → ~30;
  - ukloni SHA1 alias; kolabiraj `intent_type`/`driver_basics`/`anchor_index` ako mjereno suvišni;
  - ukloni `processed_tool_registry.json` kad ga registry više ne čita.
- **Svako brisanje iza testa + benchmarka.** Ovo je KRAJ puta, ne početak.

### (Opcionalno) FAZA 5 — MCP server za M365 Copilot
- Omotaj `/actions` u MCP server → otključava Copilot kanal istim tool-layerom.

---

## 6. Ispravljeni tok podataka (sa safety gateovima koje Gemini izbacuje)

```
[ KORISNIK ] (WhatsApp / Viber)
   │
[ webhook_simple.py ] → HMAC verify → +channel tag → XADD u Redis stream
   │
[ worker.py → V2Engine ]
   ├─ L-1 rate_limiter · L0.5 pii_scrubber · L0.6 input_sanitizer   ⟵ NE briši
   ├─ L0 identity (telefon→person/tenant)                            ⟵ NE briši
   ├─ L0.7 crisis_detector (suicid→hotline)                          ⟵ NE briši (Gemini je htio)
   ├─ PENDING continuations (params/mutation/clarify)                ⟵ NE briši (Gemini je htio)
   ├─ special_intents (GDPR/welcome/handover)                        ⟵ NE briši (Gemini je htio)
   ├─ L3 router → bira 1 od ~30 akcija (NAKON Faze 4; danas 1 od 950)
   └─ L6 mutation_gate → Da/Ne PRIJE write akcije                    ⟵ NE briši
   │
[ executor.py ] → async POST /actions/{name} (api_gateway: token + x-tenant + Idempotency-Key + SSRF)
   │
[ Business API /actions/* ]  ⟵ MobilityOne (Damir) — orkestrira granularne pozive
   │
[ llm_formatter.py ] → suhi JSON → hrvatski
   │
[ worker.py outbound ] → po channel tagu → WhatsApp/Viber
```
Razlika od Geminijevog dijagrama: **3 safety sloja (crisis, pending, special) +
confirm gate ostaju**. To nisu "suvišni međuslojevi" — to su pravne/etičke/UX obveze.

---

## 7. Ispravljena validacija (NE "prune po 1 poruci")

Gemini: "pošalji 1 poruku, obriši sve što nije sudjelovalo." → katastrofa (§1).
Pravi pristup:

1. **Coverage-driven dead-code detekcija:** `pytest --cov` + import-graf (kao
   `15_LIVE_VS_DEAD.md`) identificira stvarno mrtav kod — preko cijele suite, ne 1 poruke.
2. **Benchmark na oba seeda** (`tool_recognition_paraphrases.json` + `.seed1337`):
   svaka promjena routinga mora proći oba; regresija = revert.
3. **Puna suite zelena** (1749+) na svakom koraku — ne destruktivno.
4. **Stepenasti prod rollout:** feature-flag → pilot s 3-5 vozača → telemetrija →
   tek onda povlačenje stare putanje. (Ostaješ na **k8s**, NE ngrok.)
5. **Live test** zadržavamo — ali kao **dodatak** benchmarku, ne zamjenu, i preko
   k8s/ingress (ngrok je samo dev-fallback).

---

## 8. Prava definicija 10/10 (mjerljivo, ne "bez idle ms")

Sustav je 10/10 kad **istovremeno**:
- [ ] Odredišna arhitektura postignuta (bot ruta na ~30 `/actions`, ne 950).
- [ ] **Svi safety/legal slojevi netaknuti** (crisis, GDPR, PII, confirm gate).
- [ ] Benchmark ≥ **prihvatni kriterij 90/97/0** (`DAMIR_ACCURACY_UGOVOR`), 2 tjedna na živom prometu.
- [ ] Puna test suite zelena + ruff čist.
- [ ] **Ništa obrisano bez dokazanog replacementa**; svaka faza ima rollback.
- [ ] Multichannel (WA+Viber) radi; deploy na k8s (ne ngrok).

"Bez ijedne suvišne linije" se postiže coverage-alatima preko cijele suite — ne
ručnim brisanjem po jednom live pozivu.

---

## 9. Gate i sljedeći korak

**Sve poslije Faze 0 ovisi o ponedjeljku** (World A/B — tko gradi `/actions`):
- **World A** (Damir gradi): plan iznad as-is; ti repointaš bota.
- **World B** (ti gradiš adapter): isti faze, ali Faza 1 uključuje i tvoj BFF sloj
  koji sintetizira `/actions` nad granularnim API-jima (poopćenje `flow_engine.py`
  s 3 na ~30) — više posla na tebi, neovisno o Damiru.

**Odmah izvedivo bez ikoga (Faza 0):** mrtav kod (3A) + Viber adapter. To kreni
kad god — ne čeka sastanak, ne ruši ništa.

---

### Dodatak — kako provjeriti svaku tvrdnju iz §1
```bash
grep -rn "/actions/" services/                                   # prazno → /actions ne postoji
sed -n '96,101p' services/registry/__init__.py                   # registar obavezan na bootu
grep -n "crisis_detector.detect\|pending_mut_store.save" services/v2/engine.py
ls tests/v2/test_{crisis_detector,pending_mutation,special_intents}.py   # testovi postoje
cat docs/SUSTAV/15_LIVE_VS_DEAD.md                               # stvarno mrtav kod (3A)
```
