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

## POVIJEST OCJENA
| Verzija | Datum | Ukupno | Blokator |
|---|---|---|---|
| v3.0 | 2026-07 | 6/10 | K2 stack neodređen |
