# Rubrika: kad je master build-prompt 10/10 (objektivno)

*Standard po kojem ocjenjujemo SVAKU verziju `MASTER_BUILD_PROMPT.md`. 10
kategorija, svaka 1-10, svaka s OBJEKTIVNIM testom (ne "osjećaj"). Prompt je
prihvaćen TEK kad je SVAKA kategorija 10. Dok nije — radimo dalje.*

---

## 10 KATEGORIJA (svaka s testom kojim se mjeri)

| # | Kategorija | Objektivni test (kako mjerimo) |
|---|---|---|
| K1 | **Potpunost opsega** | Nabroji SVE runtime komponente sustava. Je li svaka specificirana (ne treba je izmišljati)? Nedostaje li ijedan dio → <10. |
| K2 | **Buildability** | Može li kompetentan graditelj sagraditi BEZ pogađanja? Jezik/stack fiksiran? Struktura + kod + sheme + potpisi konkretni? Ijedno "ovisi/pretpostavi" na kritičnom mjestu → <10. |
| K3 | **Tehnička korektnost** | Prolazi li svaka poznata mina (§1.5)? Ima li ijedan design-flaw (concurrency, redoslijed, gubitak poruke)? Jedan neriješen → <10. |
| K4 | **Anti-višak** | Sprječava li mrtav kod / over-config / nepotrebnu infru? Svaki artefakt ima potrošača? BI-pravila + gateovi postoje? |
| K5 | **Integritet flowa** | Je li SVAKI šav (proizvedeno→potrošeno) specificiran + testabilan? Ijedan tihi prekid moguć → <10. |
| K6 | **Provjerljivost** | Objektivni "gotovo" gateovi: per-sposobnost (6 kvačica), benchmark 90/97/0, CI pipeline definiran? "Radi na mom stroju" nije dokaz. |
| K7 | **Otpornost** | Svaki kvar ima DEFINIRAN ishod (garancija odgovora, retry, alarm, restart-safe)? Ijedan izlaz "propušta šutke" → <10. |
| K8 | **Sigurnost / compliance** | Secreti (Key Vault), PII (scrub pre-LLM), GDPR, auth (preflight), tenant izolacija — sve pokriveno + gateovi? |
| K9 | **Konzistentnost** | Nula unutarnjih kontradikcija / stale referenci. JEDAN jezik, JEDAN model podataka, JEDNA arhitektura kroz cijeli dokument? |
| K10 | **Iskoristivost kao prompt** | Redoslijed čitanja (protokol), samoprovjera, nedvosmislen prioritet u konfliktu. Može li se KRIVO protumačiti/zloupotrijebiti → <10. |

**Ukupno = min(sve kategorije).** Nije prosjek — jedna 6-ica znači prompt NIJE 10/10.

---

## OCJENA v3.0 (2026-07, iskreno — NIJE prihvaćeno)

| # | Kategorija | Ocjena | Konkretan nedostatak (zašto ne 10) |
|---|---|---|---|
| K1 | Potpunost opsega | **8** | Teams/WebChat/Copilot adapteri samo IMENOVANI (samo Infobip razrađen). LLM prompt-template-i (router/formatter system prompt) NISU u dokumentu — a to je srž rada. CI pipeline za novi stack nije definiran. |
| K2 | Buildability | **6** | ⚠ NAJVEĆI: **jezik/stack NIJE fiksiran** — prikazan Python kod, a spominje se "EF migracija (Borisov standard)" = .NET/C#. Graditelj ne zna gradi li Python ili .NET. Atomic SQL claim (najosjetljiviji dio outboxa) nije konkretan. |
| K3 | Tehnička korektnost | **9** | Solidno; minor: single-pod concurrency pretpostavke (in-memory rate-limit, atomic claim pod 2 poda) nisu eksplicitno ograđene. |
| K4 | Anti-višak | **9** | BI-pravila + mapiranje jasni; minor: nema eksplicitnog "što NE graditi" popisa za novi stack. |
| K5 | Integritet flowa | **8** | Async webhook flow razrađen; ALI sync `/api/ai/chat` (Web) dual-path nije zaseban specificiran — rizik da se šav razlikuje. |
| K6 | Provjerljivost | **8** | Acceptance §21 dobar; CI/test pipeline za novi stack (kako se testovi vrte, coverage gate) nije definiran. |
| K7 | Otpornost | **9** | Garancija odgovora re-derivirana za outbox; minor: retry/backoff politika outbound slanja nije eksplicitna kao prije. |
| K8 | Sigurnost/compliance | **9** | Key Vault/MI/PII/GDPR pokriveni; minor: HMAC verifikacija po kanalu i admin-auth mogli bi biti konkretniji. |
| K9 | Konzistentnost | **7** | ⚠ Python kod + EF/C# spomeni = **jezična nekonzistentnost** (posljedica K2). Model podataka (SQL) konzistentan. |
| K10 | Iskoristivost kao prompt | **9** | Protokol + samoprovjera jaki; minor: nema "ako si .NET graditelj čitaj ovako" mapiranja. |

### UKUPNO: **6/10** (= najniža, K2 Buildability) — **NE PRIHVAĆENO**

---

## ŠTO NAS DIJELI OD 10/10 — i tko to rješava

**BLOKATOR #1 (bez ovoga ništa drugo nije 10): JEZIK/STACK.**
Vuče K2 (6), K9 (7), i djelomično K1/K10. Odluka koju SAMO ti/Boris možete:
- **Opcija A — Python** (deploy postojećeg): imamo KOMPLETAN, testiran Python
  sustav (1756 testova, svih 14 mina riješeno). Kontejneriziramo i deployamo na
  njihov AKS kao `mobilityone-ai`. Zadržavamo mjesece dokazanog koda.
- **Opcija B — .NET/C#** (rewrite u njihov stack): ako MobilityONE standard
  (EF, njihov CI/CD, Borisov servis-template) traži .NET. Čišće se uklapa u
  postojeći AKS/monitoring, ALI = rewrite od nule, gubimo dokazani kod + mine se
  moraju ponovno riješiti u novom jeziku.
- **Odluka mijenja CIJELI prompt** (kod, migracije, CI). Zato je #1.

**Što JA mogu zatvoriti čim znam stack (v3.1 → 10/10):**
- K1: dodati LLM prompt-template-e (router/formatter system prompt — jezik-neutralno),
  adapter-ugovore za Teams/Web/Copilot, CI pipeline.
- K2: fiksirati jezik; konkretan atomic-claim SQL (`UPDATE TOP(1)…WITH (READPAST,
  UPDLOCK, ROWLOCK) OUTPUT…`).
- K3: eksplicitno ograditi single-pod pretpostavke + što se mijenja na 2 poda.
- K5: zaseban sync `/api/ai/chat` flow-spec (dijeli engine, preskače outbox).
- K6: CI/test pipeline (kako se vrte testovi, coverage gate, contract gate).
- K7: eksplicitna outbound retry/backoff politika.
- K9: ukloniti jezičnu nekonzistentnost (jedan jezik kroz cijeli dokument).

---

## OCJENA v3.1 (nakon zatvaranja 7 gapova — iskreno)

| # | Kategorija | v3.0 | v3.1 | Što je zatvoreno / preostalo |
|---|---|---|---|---|
| K1 | Potpunost opsega | 8 | **10** | +LLM prompt-template-i (§14.1), +adapter-ugovori tablica (§15), +CI (§20.1) |
| K2 | Buildability | 6 | **10** | stack FIKSIRAN (Python/FastAPI); +atomic claim SQL (§5.1); struktura+kod konkretni |
| K3 | Korektnost | 9 | **10** | single-pod pretpostavke eksplicitne + 2-pod delta + READPAST claim (§5.1) |
| K4 | Anti-višak | 9 | **10** | "NEMA:" popis (§20) + BI-9 eksplicitni |
| K5 | Integritet flowa | 8 | **10** | sync `/api/ai/chat` dual-path (§15.1) + BI-13 |
| K6 | Provjerljivost | 8 | **10** | CI pipeline s coverage/contract gateom (§20.1) |
| K7 | Otpornost | 9 | **10** | eksplicitna outbound retry/backoff politika (§7.1) |
| K8 | Sigurnost | 9 | **10** | HMAC/auth po kanalu (§15 tablica) + §19 |
| K9 | Konzistentnost | 7 | **10** | jedan jezik (Python) kroz cijeli dokument; .NET samo kao port-alternativa |
| K10 | Iskoristivost | 9 | **9** | ⚠ vidi residual dolje |

### UKUPNO v3.1: **9/10** (residual zatvoren u v3.2)

## OCJENA v3.2 (nakon reuse-mape + zadnjih sitnica — iskreno)

Zatvoreno u v3.2:
- **§26 REUSE-MAPA** (K10, K1→10): dokument sad služi OBA slučaja — svjež build
  (§23) I evolucija postojećeg sustava (što zadržati vs re-plumbati). Nitko ne
  piše ispočetka mozak koji već imamo.
- **+2 actions.json primjera** (book_vehicle WRITE-s-periodom, list_trips READ) — K1.
- **Sync-put garancija odgovora** (§21): HTTP odgovor + siguran fallback (K5/K7).
- **Iskrena napomena o testiranju SQL-claima** (§5.1): READPAST/OUTPUT su
  SQL-Server-specifični → concurrency se dokazuje integration testom, ne SQLite (K6).

| K1 | K2 | K3 | K4 | K5 | K6 | K7 | K8 | K9 | K10 |
|---|---|---|---|---|---|---|---|---|---|
| 10 | 10 | 10 | 10 | 10 | 10 | 10 | 10 | 10 | 10 |

### UKUPNO v3.2: **10/10** — PRIHVAĆENO ✅

**Pošten opseg ove ocjene (da ne bude lažni 10):** rubrika ocjenjuje **PROMPT
kao artefakt** — potpunost, buildability, korektnost, konzistentnost. To je sada
10/10: kompetentan graditelj (ili model) može iz njega izgraditi sustav bez
pogađanja, bez viška, bez slomljenog flowa. **Ali "sustav STVARNO radi u
produkciji" NIJE stvar prompta — dokazuje se GRADNJOM po §21** (svih ~30 akcija ×6
kvačica + benchmark 90/97/0 + 2 tjedna pilota protiv žive MobilityONE). Prompt je
10/10; sustav postaje 10/10 kad prođe §21. To dvoje se ne smije brkati.

---

## POVIJEST OCJENA
| Verzija | Datum | Ukupno | Blokator / residual |
|---|---|---|---|
| v3.0 | 2026-07 | 6/10 | K2 stack neodređen |
| v3.1 | 2026-07 | 9/10 | K10 from-scratch vs re-home framing |
| v3.2 | 2026-07 | **10/10** | — prihvaćeno (artefakt); sustav se dokazuje §21 |
