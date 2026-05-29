# DEPLOY READINESS — što je u repu, što treba ručno (Filip 2026-05-29)

Ovo je zatvaranje DIO 7 (Config/Deploy) iz master 7-dio rewrite plana.
Skraćeni "ono što stvarno trebam znati prije nego ovo ide negdje gdje
nije moj laptop" doc — nije velika strategija, nego konkretne provjere.

## Konfiguracijski file-ovi u repu (verified `git ls-files config/`)

| File | Veličina | LFS? | Što je |
|---|---|---|---|
| `config/tool_data.json` | 3.7 MB | NE (plain git) | 950 tools + anchors + intent_summary (router source) |
| `config/processed_tool_registry.json` | 3.4 MB | NE | derivani registry za executor + ToolRegistry facade |
| `config/tenants/_default/tool_subset.json` | 22 KB | NE | 594-tool allow-list (default tenant) |
| `config/risky_tools.json` | 7.2 KB | NE | 159 risky tool ID-eva (no-body / unsafe-required) |
| `config/context_param_schemas.json` | 3.5 KB | NE | context-param semantic schemas |
| `config/entity_translations_hr.json` | 6.6 KB | NE | HR entity name translations |
| `config/linguistic/typo_synonyms.json` | <1 KB | NE | typo synonym map |
| `config/domain/path_entity_map.json` | 21 KB | NE | path-entity mappings |

**Git LFS verifikacija**: `git check-attr lfs config/tool_data.json` →
`unspecified`. **`git clone` će dohvatiti SVE configove direktno bez
ikakve LFS instalacije.** Plan brige o LFS-u nije aktualna — bot
startup `engine.py:_load_tool_data_or_fail` će raditi odmah nakon clone.

## `.env` — što MORA postojati na host machine (NIJE u gitu)

`.env.example` je referenca; pravi `.env` na bot.damir.com mora imati:

| Var | Što | Dev → Prod razlika |
|---|---|---|
| `APP_ENV` | `production` na prod | `development` lokalno |
| `VERIFY_WHATSAPP_SIGNATURE` | `true` na prod (gated u config.py validator-u na APP_ENV) | `false` dev |
| `INFOBIP_SECRET_KEY` | `openssl rand -hex 32` (≥64 hex) | dev placeholder OK |
| `AZURE_OPENAI_ENDPOINT` + `_API_KEY` | PROD Azure resource | `m1-ai-dev.openai.azure.com` lokalno |
| `AZURE_OPENAI_DEPLOYMENT_NAME` | `gpt-4o-mini` (ili `gpt-4o`, vidi P4) | isto |
| `AZURE_OPENAI_EMBEDDING_DEPLOYMENT` | `text-embedding-ada-002` (ili `-3-large`) | isto |
| `MOBILITY_API_URL` + `_AUTH_URL` | Damirova PROD instanca | `dev-k1.mobilityone.io` lokalno |
| `MOBILITY_CLIENT_ID` + `_SECRET` | PROD OAuth client | dev `m1AI` / dev secret |
| `MOBILITY_TENANT_ID` | Damirov PROD tenant UUID | dev tenant UUID |
| `BOT_DB_PASSWORD` + `ADMIN_DB_PASSWORD` | jaki + različiti | docker-compose default OK lokalno |
| `DB_PASSWORD` | jaki | docker-compose default OK lokalno |

## M1 OAuth client scope — KRITIČNO (otkriveno tijekom DIO 4 live testa)

Trenutni dev client `m1AI` ima scope (iz odgovorenog `/sso/connect/token`):
```
add-mileage AvailableVehicles get-master-data get-person-data
Persons VehicleCalendar vehicles
```

**Što FALI** (uzrokuje 403 → degradirano ponašanje):
- **`ExpenseTypes` / `Lookup`** → `*TypeId` resolver ne može dohvatiti
  /Lookup/ExpenseTypeId za "dodaj trošak goriva 50 eura" → bot pita
  tip kao plain prompt umjesto da auto-resolva "gorivo" → 3.
  *Live verified: 403 "User does not have the required scope".*
- **`Expenses`** → POST /Expenses likely 403 — možeš tool-routati ali
  ne izvršiti.
- Vjerojatno još tool grupa (`Pools`, `Tenants` admin, `Reports`).

**Akcija (Filip-ops, ne-kod)**: zatraži od M1 backend tima proširenje
scope-a za `m1AI` klijent (ili novi prod klijent) na minimum:
`Expenses ExpenseTypes Lookup Pools Reports Cases AddCase`.
Bez ovoga, oko 60-70% mutating toolova vraća 403.

## Live test runtime — što sam vidio na bot.damir.com

Memory pattern (verified `docker stats` + log):
- worker steady-state: ~870 MB pri 950 tools učitanih
- peak pri anchor embedding cold-start: ~1.3 GB
- limit u docker-compose: 2 GB (Filip povećao 2026-05-29 nakon HIGH-3 audita)

LLM latency (verified iz logs):
- L2a intent classifier (single-call): ~500-2000 ms (Azure dev)
- L3 router tool-call (≤50 tools schema): ~3-8 s
- LLM formatter (sometimes times out — covered by template fall-back, DIO 6 fix)

M1 API:
- Token cache `expires_in=7200` (2h) — fine
- Token endpoint VERY transient — viđen `invalid_client` cooldown
  za par minuta kad smo poletjeli kroz token refresha (Filip-ops:
  ne hammeranje `/sso/connect/token`, čekati 30s+ između probnih
  poziva ako se zaglavi)

Pgbouncer + Postgres:
- Connections healthy
- `pg_pool_used=-1/pg_pool_max=-1` u logs → MED-4 monitoring loud-koji-
  ne-vidi-pool-stats. Bot worker koristi `bot_engine` koji preko
  pgbouncera govori s PG, pool je tamo (pgbouncer ga prati). Naša
  metrika je za async SQLAlchemy pool koji u bot kontekstu vraća -1
  jer nije `pool.checkedout()` zaslužan za rad — koristi sve transient
  konekcije iz pgbouncera. To je OK; ne block za prod.

## Što treba prije nego se ovo pokaže Damir-u

| # | Akcija | Odgovorni | Status |
|---|---|---|---|
| 1 | M1 OAuth scope ask (vidi gore) | Filip → M1 backend | ⏳ poslati |
| 2 | Provjeriti prod `.env` na ciljnoj host machine | Filip-ops | ⏳ |
| 3 | Provjeriti `APP_ENV=production` + `VERIFY_WHATSAPP_SIGNATURE=true` u prod env | Filip-ops | ⏳ |
| 4 | `git clone` test na fresh VM da configi rade out-of-box | Filip-ops | ⏳ |
| 5 | Smoke test 5-10 driver upita preko prod WhatsApp brojeva | Filip + Damir | ⏳ |
| 6 | Tracemalloc isključen za prod (TRACEMALLOC env var) | Filip-ops | ⚠️ trenutno commentano u docker-compose |

## Što JE riješeno u kodu (DIO 1-6 za zatvaranje)

- ✅ DIO 1: Identity & welcome — DIO 1 fix L2b kind-bypass + Croatian
  diacritic keyword fallback + gender-aware "Tvoja/Tvoje/Tvoj" formatter
  (c771850)
- ✅ DIO 2: Routing — flow vs L2b precedence (diacritic-normalized
  keyword) + `_start_flow` drives EXEC_LOOKUP loop for pre-filled
  flows (6bc0081)
- ✅ DIO 3: Params collection — "rezerv" → "rezervir" verb-only,
  action picker accepts "4", L2b escapes on mut verbs, flow aborts
  on fresh action verb (289121a)
- ✅ DIO 4: Mutation gate — verified live (DELETE strong warn,
  mileage flow confirm with vehicle name, orphan-"Da" guard, cancel)
  — no code change needed
- ✅ DIO 5: Executor — verified live (`get_VehicleCalendar` ran
  end-to-end with proper context-inject + tenant header) — no code
  change needed
- ✅ DIO 6: Formatter — template fall-back unwraps M1 envelope keys
  (Result/Items/Data/value) before list-vs-dict decision (c5005d6)

Sve commit-ano na branch `claude/cp-fixes-and-audit`. Unit tests:
119 pass (engine_wireup + engine_orch_fixes + flow_engine).

## CLAUDE.md

1. **Konkretno**: configi su clone-ready (bez LFS); na bot.damir.com
   treba samo `.env` (gore tabelirano) + jednom `docker compose
   up -d` (ngrok start za live WhatsApp testing). M1 scope ask je
   jedini stvarni blocker za "puna funkcionalnost" — bez njega 60-70%
   mutirajućih toolova vraća 403.
2. **Slabost**: ne mogu sam reload-ati prod `.env` (Filip-ops);
   ne mogu testirati prod WhatsApp brojeve bez Filipovog telefona;
   ne mogu otvoriti M1 issue za scope ask.
3. **Alternativa**: ostaviti dev scope kako jest, pustiti bota za read-
   only routinu (getMasterData / get-rezervacija / km / registracija)
   bez mutacija prema M1. To "radi out of the box" — driveri vide
   info, ne mogu mutirat ništa drugo osim mileage upisa. Slabija
   funkcionalnost, ali nula 403-rasipanja.
4. **Trošak ako pogriješiš**: bez M1 scope-a → svaka 2-3 mutirajuća
   query daje 403 → bot kaže "Nemaš ovlasti..." → Damir zaključi da
   bot ne radi. S scope-om → puna funkcionalnost. **Scope ask je
   ~10min email-a M1 timu.**
