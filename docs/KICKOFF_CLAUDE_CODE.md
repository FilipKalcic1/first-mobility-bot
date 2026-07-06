# KICKOFF — upit za Claude Code (zalijepi kao prvi task)

*Zalijepi tekst ispod u Claude Code. Repo (postojeći Python sustav) je već tu,
`.env` s pravim dev kredencijalima je u projektu. Priloži/pokaži: MASTER_BUILD_PROMPT.md,
BUILD_PROMPT_RUBRIKA.md, M1_API_PLAYBOOK.md (svi u `docs/`).*

---

Gradiš **`mobilityone-ai`** — Conversation Engine za MobilityONE flotu, u **Pythonu**.

**Dokumenti (pročitaj OVIM redom prije koda):**
1. `docs/MASTER_BUILD_PROMPT.md` (v3.3) — pravila, ciljna arhitektura, invarijante,
   mine (§1.5), build-integrity (§1.6), engineering standardi + proširivost (§27),
   REUSE-MAPA (§26). Slijedi PROTOKOL iz zaglavlja.
2. `docs/M1_API_PLAYBOOK.md` — KONKRETNI M1 pozivi (auth, Persons, MasterData,
   AvailableVehicles, VehicleCalendar) i prve 2 akcije do razine payloada.
3. `docs/BUILD_PROMPT_RUBRIKA.md` — ocjenjuj se po 11 kategorija; cilj 10/10.

**PRISTUP — NE gradi od nule:** evoluiraj POSTOJEĆI kod u ovom repou prema cilju
po REUSE-MAPI (§26). Zadrži mozak (engine/slojevi, svih 14 mina, actions ugovor,
auth/token_manager/api_gateway). Re-plumbaj infrastrukturu (Postgres→SQL `ai`
schema, Redis→outbox, worker→outbox petlja u istom servisu). **Suite MORA ostati
zelena na SVAKOM koraku** (`pytest`, `ruff`).

**OPSEG FAZE 1 (za srijedu) — SAMO ovo, ni više ni manje:**
- Kanal: **SAMO WhatsApp** (Infobip adapter). Teams/Web/Copilot NE gradi sad —
  ports&adapters (§27) ih puštaju kasnije bez dirania jezgre.
- Akcije: **TOČNO 2** — `book_vehicle` (DEMO PRIORITET) i `report_incident`
  (prijava štete; `execution` čeka Damirov endpoint — implementiraj mozak-dio,
  ostavi jasan TODO za rutu/payload).
- Pozdrav po imenu + dodijeljeno vozilo na početku razgovora (playbook §4).

**DEFINICIJA GOTOVOG (booking, srijeda):**
Korisnik na WhatsAppu traži vozilo → sustav pita parametre koji fale (datum
polaska/povratka; odredište/svrha/putnici opc.) → `GET AvailableVehicles` →
prikaži prvo slobodno vozilo → **Da/Ne potvrda** → `POST VehicleCalendar` (s
Idempotency-Key) → HR potvrda o rezervaciji ili HR greška. **E2e testirano +
live-provjereno protiv dev M1** (kredencijali su u `.env`, SMIJEŠ live zvati).

**NIKAD ne krši:** invarijante §1 (osobito: Da/Ne prije mutacije; PII scrub prije
LLM-a; tenant/personId iz identiteta, ne iz teksta). **Ne ponavljaj mine §1.5**
(osobito M8 timezone: dva formata datuma — query `YYYY-MM-DD HH:MM` vs body ISO
`T`; M9 Idempotency-Key na POST; interne konstante `AssigneeType:1`/`EntryType:0`
NE generira AI).

**Nakon svakog koraka:** samoprovjera §25 + testovi zeleni. Kad je booking gotov,
ocijeni se po rubrici i reci mi na čemu si.
