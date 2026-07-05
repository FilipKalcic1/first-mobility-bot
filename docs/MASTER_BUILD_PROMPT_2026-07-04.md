# MASTER BUILD PROMPT — MobilityOne Fleet AI Bot

> **Uputa graditelju (AI ili programeru):** Ti si senior inženjer. Iz OVOG JEDNOG
> dokumenta izgradi kompletan, produkcijski sustav opisan ispod. Dokument je
> samostojeći: sadrži misiju, invarijante, arhitekturu, strukturu repoa, sve
> ugovore (config/state/API), flowove, kod-primjere, testni i deploy plan.
> Redoslijed gradnje je u §23. Definicija "gotovo" je u §21. Ako nešto nije
> ovdje specificirano, primijeni konzervativni default i ZAPIŠI odluku.
>
> **Verzija:** 2026-07-04 **v1.1** (revizija po pressure-testu: ciljno stanje
> normativno — §0.5; skela karantenizirana — §9.2/§22.5; potpunost per-akcija —
> §21.2) · Konsolidacija 5 dokumenata (TEHNIČKA SPECIFIKACIJA,
> PRESSURE POINTS, PLAN KONVERGENCIJA, USPOREDBA ARHITEKTURA, M1 ADDENDUM) +
> **stvarno stanje koda** (suite: 1754 passed / 0 failed, coverage 87%).
> Gdje se spec i implementacija razlikuju, OVAJ dokument nosi istinu.

---

## §0 MISIJA

Korisnik (vozač službene flote) šalje poruku prirodnim jezikom na **WhatsApp ili
Viber** (Infobip broj): *"želim vidjeti moja posljednja putovanja"*, *"pukla mi
je guma na ZG-1234-AB"*, *"rezerviraj auto sutra 9-15"*. Sustav mora:

1. **prepoznati namjeru** (1 od kataloga akcija/alata),
2. **pozvati TOČNO pravi API** MobilityOne backenda (param punjenje, filtriranje,
   šifrarnici, ekstrakcija iz responsa — sve točno),
3. **odgovoriti na hrvatskom**, sažeto i utemeljeno SAMO na podacima iz API-ja.

**Podjela odgovornosti (pravilo koje sve oblikuje):**
```
BOT          = jezik + sigurnost + JEDNA čista akcija po turnu
BUSINESS API = poslovna pravila + orkestracija granularnih poziva   (cilj, /actions)
DOMAIN API   = podaci (≈950 granularnih CRUD ruta, M1 Swagger)
```
**Normativna hijerarhija ovog dokumenta:** CILJNI sustav je §0.5 — bot zove
ISKLJUČIVO `/actions` (~30 akcija). Model A kaskada nad 950 ruta je PRIJELAZNA
SKELA (§9.2) koja postoji samo dok `/actions` API ne postoji; ima egzaktnu
delete listu (§22.5) i datum smrti (Faza 4). Migracija je fazna (Strangler Fig,
§23.2) — **ništa se ne briše prije dokazane zamjene, ali NIŠTA prijelazno se ne
smije zamijeniti za cilj.**

---

## §0.5 CILJNO STANJE — NORMATIVNO (ovo je sustav koji gradimo)

**Bot zove ISKLJUČIVO `POST/GET /actions/*` (~30 akcija). Ništa od prijelazne
skele (§22.5) ne postoji u ovom stanju.**

### Ciljno stablo (end-state; usporedi sa §3 koji je današnja stvarnost)
```
mobilityone-whatsapp-bot/                (CILJNO — višak iz §22.5 NE postoji)
├── config/
│   ├── actions.json                     # ~30 akcija — JEDINI katalog (§11)
│   └── entity_translations_hr.json
├── services/
│   ├── api_gateway.py · token_manager.py · auth_preflight.py
│   ├── whatsapp_service.py · viber_service.py      # kanali (hook seam §15.2)
│   ├── tenant_resolver.py · tenant_config.py       # §16
│   ├── queue_service.py · errors.py · retry_utils.py · tracing.py · admin_auth.py
│   ├── router/llm_router.py             # SAMO: 30 shema → 1 LLM tool-call
│   │                                    #   (BEZ Stage A / anchora / scopinga)
│   ├── formatter/llm_formatter.py
│   ├── mcp/server.py                    # Copilot kanal — omata ISTE akcije
│   └── v2/
│       ├── engine.py                    # orkestrator (~8 slojeva)
│       ├── rate_limiter · pii_scrubber · input_sanitizer · output_sanitizer
│       ├── identity · crisis_detector · special_intents · gdpr_audit
│       ├── negation_handler · multi_intent_detector · meta_intents
│       ├── action_registry · action_validator       # §11 (jedini "katalog" kod)
│       ├── clarify_ui · pending_clarify · mutation_gate · pending_mutation
│       ├── param_ui · pending_params · optional_extractor · param_labeler
│       ├── type_resolver · api_error_translator · executor
│       ├── conversation_history · telemetry · latency_ux · azure_rate_guard
│       └── knowledge/                   # F2 RAG (rag_retriever · doc_store)
├── webhook_simple.py · worker.py · main.py · config.py · database.py · alembic/
├── k8s/ · Dockerfile · docker-compose.yml
└── tests/                               # po modulu + e2e razgovori + contract
                                         #   fixturei + benchmark NAD AKCIJAMA
```

### Ciljni flow (end-state)
```
poruka (WA / Viber / Copilot / Web)
→ [chat kanali] safety krevet (rate→PII→inject) → identity(→tenant strict)
→ pending nastavci (params/confirm/clarify)
→ LLM: 30 akcija DIREKTNO u tool-call (nema retrievala — sve stane u prompt)
     → { action | clarify | answer }
→ action_validator (anti-halucinacija) → coercion → fali required? pitaj
→ codebook? semantika ide backendu (on mapira po tenantu)
→ inject person/tenant → [write] param echo + "Potvrđuješ? (Da/Ne)"
→ executor → POST /actions/<name> {čisti poslovni payload}
→ BUSINESS API orkestrira (pravila + granularni pozivi + šifrarnici)
→ čisti JSON → formatter → hrvatski → outbound po kanalu
[Copilot put: mcp/server.py izlaže ISTE akcije; Copilot je mozak — §15.3]
```

**Definicija "stigli smo":** ovo stablo + ovaj flow + §21.2 potpunost po akciji
+ §22 registar bez 🔴. Sve što je u §3 a nije ovdje = višak s planom smrti (§22.5).

---

## §1 NEPOVREDIVI INVARIJANTI (krše li se — build je neispravan)

| # | Invarijanta | Mehanizam |
|---|---|---|
| 1 | **Nijedna mutacija bez eksplicitne potvrde korisnika** (Da/Ne) — LLM *bira* akciju, nikad ne *izvršava* write sam | mutation_gate + pending_mutation (echo parametara prije slanja) |
| 2 | **PII se scrubba PRIJE ijednog LLM poziva** (OIB/IBAN/telefon → [REDACTED]) i prije logova | pii_scrubber; PIIScrubFilter na logging |
| 3 | **Tenant strict-binding**: identitet iz telefona → person_id + TenantId iz M1; bez TenantId NEMA nijednog API poziva; tenant NIKAD iz env-defaulta ni iz teksta korisnika | identity.resolve; x-tenant header |
| 4 | **Krizni signal (suicid) → hotline poruka** (Plavi telefon 116 123), terminal, prije routinga | crisis_detector |
| 5 | **GDPR intenti** (brisanje/izvoz) → audit zapis + definiran postupak | special_intents + gdpr_audit |
| 6 | **Korisnik UVIJEK dobije odgovor ili poruka završi u DLQ s alarmom** — nikad tiha smrt (dokaz: §21.3, 20 enumeriranih izlaza) | worker fallback trojka + DLQ ×2 |
| 7 | **Idempotencija ×3 razine**: webhook dedup (60s) → msg_lock (300s) → outbound `sent:{key}` (600s) + `Idempotency-Key` header prema API-ju | vidi §5 |
| 8 | **Redoslijed poruka istog korisnika** se čuva (per-sender lock; worker ×1 ili Redis lock pri ×N) | worker |
| 9 | **Prompt-injection obrana na ulazu** (input_sanitizer) i na izlazu iz API podataka (output_sanitizer) | postoji |
| 10 | **Fail-closed rubovi**: HMAC bez tajne/potpisa → 401; auth strict gate traži POZITIVNU verifikaciju (§13) | webhook, auth_preflight |
| 11 | **AI ne vidi interne parametre** (person_id, tenant_id = inject; CaseType kodovi = backend/resolver) — AI puni SAMO što korisnik izgovori | actions.json granica §11 |
| 12 | **Svaki novi izlazni put iz koda = novi redak u §21.3 + test** | CI pravilo |

---

## §2 ARHITEKTURA — jedna slika (stvarno stanje + ciljni sloj)

```
════════════════════════ VANJSKI SVIJET ═══════════════════════════════════
  [WhatsApp user]   [Viber user]           [M365 Copilot]      [Web user]
        │                │                       │                 │
  ┌─────▼────────────────▼─────┐                 │                 │
  │        INFOBIP CLOUD        │                │                 │
  │  WA kanal (ŽIV)             │                │                 │
  │  Viber kanal (KOD GOTOV;    │                │                 │
  │   ⚠ čeka sender approval)   │                │                 │
  └─────────────┬───────────────┘                │                 │
                │ HTTPS webhook                  │ MCP (F-M365)    │ /chat (F-Web)
════════════════▼══════ NAŠ CLUSTER (AKS / VM) ══▼═════════════════▼════════
  namespace: mobility-bot                 ┌──────────────┐
  ┌──────────────────────────┐            │ mcp/server.py │ (F-M365: omata
  │ INGRESS nginx + TLS cert │            │ ⚠ Entra auth  │  ISTE akcije)
  └──────────┬───────────────┘            └──────┬────────┘
  ┌──────────▼────────────────────────────┐      │
  │ bot-api ×2 (HPA 2-4, maxUnavail=0)    │      │
  │  webhook_simple.py:                   │      │
  │  /webhook/whatsapp · /webhook/viber   │      │
  │  HMAC → dedup → +channel tag → XADD   │      │
  └──────────┬────────────────────────────┘      │
  ┌──────────▼───────────────┐                   │
  │ REDIS (AOF, noeviction)  │ stream+queue+state│
  └──────────┬───────────────┘                   │
  ┌──────────▼───────────────────────────────────▼───────────────────────┐
  │ bot-worker ×1 (V2Engine)                                             │
  │  [boot: auth_preflight — scope introspection + route probe, §13]     │
  │  safety(rate/PII/inject) → identity → pending nastavci → LLM decision│
  │  → validacija → params(ask/coerce/codebook) → Da/Ne → executor       │
  │  → formatter(HR) → outbound queue (channel tag) → WA/Viber send      │
  └───────┬──────────────────────────────┬───────────────────────────────┘
  ┌───────▼───────┐              ┌───────▼──────────┐
  │ POSTGRES      │              │ AZURE OPENAI     │
  │ user_mappings │              │ gpt-4o-mini      │
  │ tenant_settings(F1)│         │ decision+format  │
  └───────────────┘              └──────────────────┘
                │  Bearer + x-tenant + Idempotency-Key
════════════════▼══════════ M1 CLOUD (MobilityOne) ═════════════════════════
  ┌────────────────────┐  ┌──────────────────────────┐  ┌────────────────┐
  │ IdentityServer     │  │ BUSINESS API /actions/*  │  │ DOMAIN API     │
  │ OAuth client_creds │  │ (GRADI SE — World A/B    │─▶│ 950 granularnih│
  │ (scope → §13)      │  │  gate; BUSINESS_API_URL) │  │ CRUD ruta (ŽIV)│
  └────────────────────┘  └──────────────────────────┘  └────────────────┘
```

Mini master flow:
```
poruka → HMAC → dedup → stream(+channel) → worker → safety → identity(→tenant)
→ [pending nastavak?] → LLM bira akciju → validacija → [fali param? pitaj]
→ [šifrarnik? riješi] → [write? "Potvrđuješ? Da/Ne"] → API poziv → JSON
→ hrvatski → outbound po channel tagu → korisnik      [svaki izlaz = poruka]
```

---

## §3 STRUKTURA REPOA (stvarno stanje; F-oznake = buduće faze)

```
mobilityone-whatsapp-bot/
├── config/
│   ├── tool_data.json                # DANAS: ~950 alata (Swagger-derived registar)
│   ├── processed_tool_registry.json  # boot-obavezan registar (registry ga učitava)
│   ├── actions.json                  # F1: ~30 akcija (§11) — PORED tool_data, iza flaga
│   ├── entity_translations_hr.json   # HR nazivi entiteta
│   └── tenants/                      # ⚠ SKELA: dev-seed za catalog_scoper (950-svijet);
│                                     #   NIJE tenant-identitet! umire u Fazi 4 (§16.0, §22.5)
├── services/
│   ├── api_gateway.py                # HTTP + OAuth + x-tenant + Idempotency-Key + SSRF
│   │                                 #   + circuit breaker + list-serializacija (§12)
│   ├── token_manager.py              # OAuth client_credentials + token cache
│   ├── auth_preflight.py             # PP1: scope introspection + route probe (§13)
│   ├── whatsapp_service.py           # Infobip WA send + retry/backoff; HOOKOVI za kanale:
│   │                                 #   ENDPOINT_PATH · SPAN_NAME · _default_sender()
│   │                                 #   · _payload_recipient() · build_payload()
│   ├── viber_service.py              # ViberService(WhatsAppService) — SAMO overridea
│   │                                 #   hookove (§15.2); retry/error logika naslijeđena
│   ├── tenant_resolver.py            # phone→tenant (Postgres + Redis cache, bypass_cache)
│   ├── queue_service.py              # XREADGROUP wrapper (STREAM_INBOUND)
│   ├── errors.py                     # ErrorCode str-enum + HTTP_STATUS_TO_ERROR_CODE
│   ├── retry_utils.py · tracing.py · rag_scheduler.py · admin_auth.py
│   ├── router/
│   │   ├── llm_router.py             # Model A kaskada (§9): anchor retrieval + LLM tool-call
│   │   └── tool_schema_builder.py    # tool schema za LLM (suppressa filter/useandfor)
│   ├── formatter/llm_formatter.py    # JSON → hrvatski (§14): grounding, truncation, prune
│   └── v2/
│       ├── engine.py                 # ORKESTRATOR — jedini smije spajati slojeve (§8)
│       ├── rate_limiter.py · pii_scrubber.py · input_sanitizer.py · output_sanitizer.py
│       ├── identity.py               # phone → person_id/tenant_id/vlastito vozilo
│       ├── crisis_detector.py · special_intents.py · gdpr_audit.py    # NIKAD ne brisati
│       ├── intent_type.py · driver_basics.py · negation_handler.py
│       ├── multi_intent_detector.py · meta_intents.py
│       ├── flow_engine.py            # multi-step orkestracije (3 flowa; predložak za World B)
│       ├── clarify_ui.py · pending_clarify.py          # top-3 izbor 1/2/3
│       ├── mutation_gate.py · pending_mutation.py      # Da/Ne gate (NEODVOJIVI par)
│       ├── param_ui.py · pending_params.py             # HR pitanja + multi-turn params
│       ├── optional_extractor.py · param_labeler.py · type_resolver.py
│       ├── api_error_translator.py   # 4xx → HR objašnjenje (cache 1h)
│       ├── executor.py               # API izvršenje (async, preko gatewaya; budžet 15s)
│       ├── conversation_history.py   # zadnjih 5 turnova, PII-scrubbed, TTL 30min
│       ├── telemetry.py · latency_ux.py · atomic_io.py · cache_invalidation.py
│       ├── azure_rate_guard.py
│       ├── action_registry.py        # F1 (§11) — učita+validira actions.json
│       ├── action_validator.py       # F1 (§11) — anti-halucinacija prije executora
│       └── knowledge/                # F2 (RAG): rag_retriever.py · doc_store.py
├── services/mcp/server.py            # F-M365: MCP server omata iste akcije (§15.3)
├── services/tenant_config.py         # F1: tenant_settings store (§16)
├── webhook_simple.py                 # FastAPI: /webhook/{whatsapp,viber}, admin endpointi
├── worker.py                         # consumer loop + outbound pump + health (§7)
├── main.py                           # FastAPI app (mount webhook routera; F-Web: /chat)
├── config.py                         # pydantic Settings (env tablica §4)
├── database.py · alembic/            # Postgres (user_mappings; F1: 004_tenant_settings)
├── k8s/                              # 11 manifesta + README runbook (§20)
├── Dockerfile · docker-compose.yml
├── scripts/                          # verify_production_readiness.py · probe_* · sync_tools
│                                     # build_golden_set.py · regenerate_anchors …
└── tests/                            # 1754 passed / 9 skipped; ≥85% coverage gate
    ├── test_webhook*.py · test_worker_*.py · test_whatsapp_service.py
    ├── test_viber_service.py · test_auth_preflight.py
    ├── tests/contract/ (harness §17.3 + fixtures/)
    ├── tests/v2/ (po modulu + test_architecture.py = ENFORCED manifest modula!)
    └── tests/benchmarks/ (dual-seed golden set; 90/97/0 protokol)
```

> `tests/v2/test_architecture.py` drži **LEAF_MODULES manifest** — dodavanje/
> brisanje modula u `services/v2/` MORA ažurirati manifest u istom commitu.

---

## §4 KONFIGURACIJA — kompletna env tablica

| Var | Obavezno | Značenje |
|---|---|---|
| `DATABASE_URL` | ✅ | Postgres (asyncpg) — pydantic-required; = bot user u dual modelu |
| `BOT_DATABASE_URL` / `ADMIN_DATABASE_URL` | opc. | **dual-user DB security model**: bot = ograničeni user, admin = puni pristup; oba fallback na DATABASE_URL |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` / `DB_POOL_RECYCLE` | default 5/10/3600 | SQLAlchemy pool (health prati iskorištenost) |
| `REDIS_URL` | ✅ | Redis (stream+queue+state) |
| `REDIS_MAX_CONNECTIONS` | default 100 | Redis connection pool cap |
| `MOBILITY_API_URL` | ✅ | M1 Domain API host |
| `MOBILITY_AUTH_URL` | ✅ | IdentityServer token endpoint |
| `MOBILITY_CLIENT_ID` / `MOBILITY_CLIENT_SECRET` | ✅ | OAuth client_credentials |
| `MOBILITY_TENANT_ID` | ✅ | dev/default tenant (probe; NIKAD za user pozive — §1.3) |
| `AZURE_OPENAI_ENDPOINT` / `AZURE_OPENAI_API_KEY` | ✅ | LLM (gpt-4o-mini deployment, PIN verziju) |
| `AZURE_OPENAI_API_VERSION` / `AZURE_OPENAI_DEPLOYMENT_NAME` | default u kodu | API verzija + ime TVOG chat deploymenta |
| `AZURE_OPENAI_EMBEDDING_DEPLOYMENT` | ⚠ skela/F2 | embeddingi: danas ih troši SAMO 950-skela (anchor/registry); u cilju tek F2 RAG |
| `GDPR_HASH_SALT` | ✅ prod | konzistentna PII pseudonimizacija kroz restarte; generiraš JEDNOM (`openssl rand -hex 32`) i čuvaš ZAUVIJEK — **rotacija mijenja sve hash-eve, NE rotira se rutinski** |
| `AZURE_OPENAI_API_VERSION` / `AZURE_OPENAI_DEPLOYMENT_NAME` / `AZURE_OPENAI_EMBEDDING_DEPLOYMENT` | default 2024-08-01-preview / gpt-4o-mini / text-embedding-ada-002 | API verzija + deployment imena (chat + embeddings) |
| `LLM_INPUT_PRICE_PER_1K` / `LLM_OUTPUT_PRICE_PER_1K` / `DAILY_COST_BUDGET_USD` | default | cost tracking + dnevni budžet alarm |
| `DRIFT_BASELINE_DAYS` / `DRIFT_ANALYSIS_HOURS` / `DRIFT_MIN_SAMPLES` | default 7/6/50 | model-drift detekcija nad telemetrijom |
| `GDPR_HASH_SALT` | ✅ prod | konzistentna PII pseudonimizacija kroz restarte — TAJNA; ⚠ rotacija mijenja SVE hash-eve pa se NE rotira rutinski (čuvati kao trajnu tajnu) |
| `SENTRY_DSN` | opc. | error tracking |
| `APP_ENV` | ✅ | `production` aktivira validatore (npr. zabrana isključenog HMAC-a) |
| `INFOBIP_BASE_URL` / `INFOBIP_API_KEY` | WA/Viber | Infobip account (`App {key}` auth) |
| `INFOBIP_SENDER_NUMBER` | WA | WhatsApp sender broj |
| `INFOBIP_SECRET_KEY` | WA/Viber | HMAC-SHA256 tajna za webhook (X-Hub-Signature-256) |
| `VIBER_SENDER` | Viber | registrirano IME Viber sendera; **neset = Viber kanal off** (poruke → DLQ `VIBER_NOT_CONFIGURED`) |
| `VERIFY_WHATSAPP_SIGNATURE` | default true | HMAC verifikacija (prod je ne smije isključiti) |
| `AUTH_PREFLIGHT` (`=0` off) / `AUTH_PREFLIGHT_STRICT` (`=1` fail-closed) | default log-only | §13 |
| `MOBILITY_REQUIRED_SCOPES` | opc. | space/comma lista scopeova koje token MORA nositi |
| `BUSINESS_API_URL` | F1 | host `/actions/*`; **default = MOBILITY_API_URL** (config, ne pretpostavka) |
| `V2_USE_ACTIONS` | F1, default 0 | action-mode PORED starog routera (kill-switch = 0) |
| `V2_TELEMETRY` / `V2_TELEMETRY_BACKEND` | default on | stdout+redis sinkovi |
| `CONTRACT_BASE_URL` / `CONTRACT_BEARER_TOKEN` / `CONTRACT_ALLOW_MUTATIONS` | CI/dev | live contract testovi (§17.3); POST fixturei traže eksplicitni `ALLOW_MUTATIONS=1` |
| `ANCHOR_CACHE_PATH` | k8s | PVC lokacija anchor cachea |
| `WORKER_HEARTBEAT_FILE` | k8s | liveness datoteka (worker nema HTTP port) |
| `ADMIN_TOKEN_1..N` + `ADMIN_TOKEN_N_USER` | admin | admin endpointi (gdpr-process, cache-invalidate, tenants) — više imenovanih tokena (audit zna TKO) |
| `ADMIN_ALLOWED_IPS` / `ADMIN_RATE_LIMIT_PER_MINUTE` | admin | IP allowlist (CIDR podržan) + rate limit admin API-ja |
| `OTEL_ENABLED` | default false | tracing no-op dok se ne uključi |

**Uklonjeno 2026-07-04 (mrtve — čitao ih samo config, bez živog potrošača;
test `test_dead_config_vars_stay_removed` čuva da se ne vrate):**
`LLM_INPUT/OUTPUT_PRICE_PER_1K` + `DAILY_COST_BUDGET_USD` (cost tracking —
admin_api obrisan) · `DRIFT_*` (drift detekcija nikad spojena) · `SENTRY_DSN`
(nikad `sentry_sdk.init()`; vrati se TEK s inicijalizacijom) ·
`WHATSAPP_VERIFY_TOKEN` (GET verifikacija je bezuvjetni "ok").

### 4.1 Klasifikacija — što je STVARNO potrebno (ne gomilamo)

```
JEZGRA (bez ovoga se ne diže, 9):   DATABASE_URL · REDIS_URL · MOBILITY_API_URL
  · MOBILITY_AUTH_URL · MOBILITY_CLIENT_ID · MOBILITY_CLIENT_SECRET
  · MOBILITY_TENANT_ID · AZURE_OPENAI_ENDPOINT · AZURE_OPENAI_API_KEY (+APP_ENV)
KANALI (čim kanal radi, 5):         INFOBIP_BASE_URL · INFOBIP_API_KEY
  · INFOBIP_SENDER_NUMBER · INFOBIP_SECRET_KEY · VIBER_SENDER
PROD SIGURNOST (3+):                GDPR_HASH_SALT · ADMIN_TOKEN_1..N(+_USER)
  · ADMIN_ALLOWED_IPS  (VERIFY_WHATSAPP_SIGNATURE ostaje true)
DEFAULTI — NE DIRAJ bez mjerenja:   DB_POOL_* · REDIS_MAX_CONNECTIONS
  · AZURE_OPENAI_API_VERSION/DEPLOYMENT_NAME · V2_TELEMETRY* · AUTH_PREFLIGHT*
  · ADMIN_RATE_LIMIT_PER_MINUTE · OTEL_ENABLED
F1/BUDUĆE (tek uz /actions):        BUSINESS_API_URL · V2_USE_ACTIONS
  · MOBILITY_REQUIRED_SCOPES · CONTRACT_* (samo CI/dev)
K8S OPS (2):                        WORKER_HEARTBEAT_FILE · ANCHOR_CACHE_PATH(⚠ skela)
OPCIONALNO (dual-user DB, prod):    BOT_/ADMIN_DATABASE_URL
SKELA-VEZANO (umire s Fazom 4):     AZURE_OPENAI_EMBEDDING_DEPLOYMENT (do F2 RAG)
  · ANCHOR_CACHE_PATH
```

### 4.2 Gdje i kako NABAVITI svaku bitnu vrijednost

| Vrijednost | Gdje se nabavlja | Kako |
|---|---|---|
| `DATABASE_URL` (+BOT_/ADMIN_) | naš Postgres | sami definiramo: kreiraj DB + user(e); format `postgresql+asyncpg://user:pass@host:5432/db` |
| `REDIS_URL` | naš Redis | sami (k8s/compose servis); format `redis://host:6379/0` |
| `MOBILITY_API_URL` / `MOBILITY_AUTH_URL` | **M1 (Damir)** | host dev/prod instance; auth = isti host + `/sso/connect/token` |
| `MOBILITY_CLIENT_ID` / `_SECRET` | **M1 (Damir)** | service account (client_credentials) koji IZDAJE njihova strana |
| `MOBILITY_TENANT_ID` | **M1 (Damir)** | UUID dev tenanta — SAMO za probe/smoke (runtime tenant dolazi iz /Persons!) |
| `AZURE_OPENAI_ENDPOINT` / `_API_KEY` | Azure Portal | Azure OpenAI resource → Keys & Endpoint (firma već ima resource) |
| `AZURE_OPENAI_DEPLOYMENT_NAME` | Azure OpenAI Studio | ime deploymenta koji SI kreirao (gpt-4o-mini); PIN verziju modela |
| `INFOBIP_BASE_URL` / `_API_KEY` | Infobip portal | portal.infobip.com → API keys; base = tvoja subdomena `xxxxx.api.infobip.com` |
| `INFOBIP_SENDER_NUMBER` | Infobip | WhatsApp broj koji ti je Infobip dodijelio/registrirao |
| `INFOBIP_SECRET_KEY` | **TI ga izmisliš** | `openssl rand -hex 32` → ISTI string upišeš u Infobip webhook config I u env (shared secret za HMAC) |
| `VIBER_SENDER` | Infobip (ops!) | ime sendera NAKON što Infobip odobri Viber registraciju (dani-tjedni — pokreni odmah) |
| `GDPR_HASH_SALT` | **TI ga generiraš** | `openssl rand -hex 32` — JEDNOM, čuvaš zauvijek (rotacija = gubitak povezivosti hash-eva) |
| `ADMIN_TOKEN_N` | **TI ga generiraš** | `openssl rand -hex 32` po OSOBI (token = identitet u auditu) |
| `BUSINESS_API_URL` + scope imena | **M1 (Damir)** — sastanak | pitanja B1/B2 u PITANJA_ZA_SEFA docu |
| TLS cert za webhook | cert-manager (k8s) | automatski Let's Encrypt kroz ingress — ništa ručno |

---

## §5 PODACI I STANJE

### 5.1 Redis — svi ključevi (vlasnik, TTL, svrha)

| Ključ | Vlasnik | TTL | Svrha |
|---|---|---|---|
| `wh_dedup:{message_id}` | webhook | 60s | Infobip retry dedup (SET NX) |
| `whatsapp_stream_inbound` | webhook→worker | maxlen 100k | ulazni red SVIH kanala (entry nosi `channel`) |
| `msg_lock:{sender}:{msg_id}` | worker | 300s | idempotencija obrade |
| `v2:rl:m\|h:{phone}` | engine | 60s/3600s | rate limit bucketi |
| `v2:identity:{phone}` | engine | 30s | identitet cache |
| `tenant_phone:{e164}` | tenant_resolver | 300s | phone→tenant cache (GDPR path koristi `bypass_cache=True`!) |
| `v2_pending_params:{phone}` | engine | 300s | multi-turn prikupljanje parametara |
| `v2:pending_mut:{phone}` | engine | 300s | čekanje Da/Ne |
| `v2:pending_mut_exec:{phone}` | engine | 30s | anti-replay pri "Da" (dupli tap / Infobip retry) |
| `v2_pending_clarify:{phone}` | engine | 300s | izbor 1/2/3 + "nije točno" reoffer |
| `v2_conv_history:{phone}` | engine | 30min | zadnjih 5 turnova (PII-scrubbed) |
| `session:{sender}:tenant_id` | worker | 3600s | tenant za api_gateway precedencu |
| `mobility:access_token` | gateway | ~expiry | OAuth token cache |
| `api_err_translate:{hash}` | translator | 3600s | 4xx→HR prijevod cache |
| `whatsapp_outbound` / `_processing` / `_delayed` (zset) | worker | — | izlazni red (entry: `{to,text,channel,idempotency_key[,attempt,scheduled_at]}`) |
| `sent:{idempotency_key}` | worker | 600s | outbound dedup nakon crasha |
| `dlq:inbound` / `dlq:outbound` / `dlq:webhook` | worker/webhook | 7d expire | mrtve poruke + alarm u health logu |
| `routing:accuracy_log[:{tenant}]` | telemetry | LTRIM 0..999, 30d | 1 event po routing odluci |
| `gdpr:requests:{tenant}` / `handover:requests:{tenant}` | gdpr_audit | 90d/30d | audit tragovi |
| `tenant_cfg:{tenant_id}` | F1 tenant_config | 300s | cache bot-side postavki (DB istina) |

### 5.2 Postgres — STVARNA slika (audit 2026-07-04)

**Namjerni dizajn: baza je MALA.** Sve runtime stanje (pending, history,
dedup, queue) živi u Redisu s TTL-ovima; M1 drži poslovne podatke; Postgres
drži samo TRAJNA mapiranja i postavke — KB/MB reda veličine.

```
DANAS (alembic 001-003):
  user_mappings           ✅ ŽIVA — jedina koju bot koristi (tenant_resolver)
     id UUID PK · phone_number UNIQUE · api_identity · display_name
     · tenant_id · is_active · created/updated_at · (+consent polja iz 002)
  conversations           ⚠ MRTVA — nitko ne piše (povijest je u Redisu,
  messages                ⚠ MRTVA    v2_conv_history 30min TTL!)
  tool_executions         ⚠ MRTVA — nitko ne piše (telemetrija je u Redisu)
     → V1 ostavština; kandidat za DROP migraciju u Fazi 4 (uz eksplicitnu
       potvrdu — možda ih Damir želi za buduće trajno arhiviranje razgovora)
  consent polja (002)     ⚠ nekorištena u services/ — isti kandidat

F1:  tenant_settings  (bot-side overlay; JSONB settings + actions_enabled)
F2:  tenant_documents (RAG upload: id, tenant_id, filename, content, uploaded_at)

USERI (dual-user least-privilege, opcionalno ali preporučeno za prod):
  bot_user    → SELECT/INSERT/UPDATE na user_mappings (+tenant_settings u F1)
  admin_user  → puni pristup + DDL (alembic migracije, admin API)
```

**Skaliranje baze — odluka: NE trebaju replike.** Podaci su sitni, promet ide
na Redis/M1; bot radi ~1 DB lookup po NOVOM korisniku (poslije toga cache).
Produkcija = managed Postgres (npr. Azure Database for PostgreSQL, najmanji
tier) s **point-in-time restore backupom** — to je jedini DB-ops zahtjev.
Connection pooling VEĆ postoji (SQLAlchemy pool 5+10 overflow; health prati
iskorištenost; prag za PgBouncer: tek ako pool utilizacija trajno >80%).

---

## §6 ULAZNI RUB — webhook (`webhook_simple.py`)

**Rute:** `POST /webhook/whatsapp` (default channel `whatsapp`) i
`POST /webhook/viber` (default `viber`) — **isti handler** (`_webhook_entry` →
`_process_webhook(default_channel)`), Infobip subscription po kanalu cilja svoju rutu.

Koraci (svaki fail-path definiran):
1. **Per-IP rate limit** (200/60s) → 429.
2. **HMAC verify** raw bodyja: header `X-Hub-Signature-256`, tajna
   `INFOBIP_SECRET_KEY`, sha256 hexdigest, `compare_digest`, opcionalni
   `sha256=` prefiks; **fail-closed** (nema tajne/potpisa → 401). Gate:
   `VERIFY_WHATSAPP_SIGNATURE` (prod ga ne smije isključiti).
3. **JSON parse** — nevaljan → 200 `{"error":"invalid_json"}` (Infobipu se ne
   vraća 5xx zbog parse-a).
4. `results[]` iteracija; ne-dict item → skip.
5. **Sender multi-fallback**: `from` → `sender` → `phoneNumber` → `phone` →
   `contact.phone`; bez sendera (delivery report) → skip.
6. **Kanal**: `integrationType` mapa `{"WHATSAPP":"whatsapp","VIBER":"viber"}`,
   nepoznato/odsutno → default rute. **Payload pobjeđuje rutu.**
7. **Edge tenant resolve** `resolve_tenant_for_phone(sender)` — iznimka NIKAD ne
   dropa poruku (tretira se kao unknown → `needs_onboarding=1`, worker onboarda).
8. **Tekst parse** `extract_text_and_type(result)` — tolerira `message` dict /
   `content` dict / `content` listu / top-level `text`/`body`; ne-TEXT →
   `text="[NON_TEXT:{TYPE}]"` + `original_type` (worker vraća "samo tekst" poruku).
9. **Dedup** (samo text put): `SET wh_dedup:{message_id} NX EX 60`; Redis pad →
   fall-through (msg_lock je backstop).
10. **XADD** `whatsapp_stream_inbound` — polja:
    `{sender, text, message_id, request_id, tenant_id, needs_onboarding, channel
    [, original_type]}`; retry ×3 backoff, reset klijenta pod lockom; totalni
    fail → `dlq:webhook` (lpush; file fallback); **Infobipu uvijek 200** osim
    401/429/503-drain.
11. **Graceful drain**: `APP_STOPPING` → 503 (Infobip će retryati poslije restarta).

Admin endpointi (token auth): `GET /admin/gdpr-requests`,
`POST /admin/gdpr-process` (dry-run pa stvarno; **bypass_cache=True** na resolve
da stale cache nikad ne autorizira brisanje), `POST /admin/cache-invalidate`,
`GET /whatsapp/routing-log?tenant=`; F1: `POST/PATCH/GET /admin/tenants`.

---

## §7 WORKER (`worker.py`)

### 7.1 Inbound petlja
- `XREADGROUP` grupa `workers`, consumer ime `worker_{ts}_{pid}_{rand:04d}`
  (jedinstvenost dokazana testom); `count=MAX_CONCURRENT`, `block=STREAM_BLOCK_MS`.
- Po poruci: unpack `{sender,text,message_id,request_id,tenant_id,channel}` —
  **`channel = data.get("channel","whatsapp")`** (backward-compat default);
  `session:{sender}:tenant_id` write; `msg_lock` SET NX 300s (dup → ACK+skip);
  non-text → HR refusal kroz **isti channel** (enqueue fail → release lock, NE
  ackaj); rate-limit cooldown poruka; per-sender asyncio lock (cap 10k, LRU evict).
- **ACK protokol:** `ack_ok` TEK nakon uspješnog outbound enqueua (ili DLQ
  zapisa) → `XACK`+`XDEL`; enqueue fail → poruka OSTAJE pending (restart reclaim).
- `engine.process_message` budžet **90s** → timeout HR poruka; iznimka →
  `dlq:inbound` + HR fallback; prazan odgovor → HR fallback. (= trojka §21.3 #13-15.)

### 7.2 Outbound pump
```
BLMOVE whatsapp_outbound → whatsapp_outbound_processing (crash safety)
→ idempotency: GET sent:{key} → skip ako poslano
→ recipient validacija (prazan to → DLQ MISSING_RECIPIENT)
→ _send_whatsapp(to, text, attempt, idempotency_key, channel)   ← dispatch:
     channel=="viber" → lazy ViberService (samo uz VIBER_SENDER; inače DLQ
                        VIBER_NOT_CONFIGURED — glasno, bez crasha)
     inače            → WhatsAppService
→ uspjeh: SETEX sent:{key} 600 + LREM processing
→ iznimka u petlji: DLQ (s channel poljem) + LREM + petlja ŽIVI dalje
Startup: _requeue_abandoned_outbound (processing → outbound, LMOVE petlja)
Delayed promoter: Lua atomic zrangebyscore+rpush+zrem svakih 5s
```

### 7.3 Klasifikacija grešaka slanja (⚠ POVIJESNI BUG — ne ponoviti!)
Servisi vraćaju **`ErrorCode` str-enum VRIJEDNOSTI** (`"VALIDATION_PHONE_INVALID"`,
`"GATEWAY_RATE_LIMITED"`), NE kratke literale. Klasifikacija:
```python
MAX_SEND_ATTEMPTS = 3
_PERMANENT_SEND_ERRORS = frozenset({
    # legacy literali (stari DLQ zapisi/replay) +
    "INVALID_PHONE","INVALID_RECIPIENT","INVALID_NUMBER","AUTH","UNAUTHORIZED","FORBIDDEN","BLOCKED",
    # STVARNE vrijednosti koje servisi emitiraju:
    ErrorCode.PHONE_INVALID.value, ErrorCode.PARAMETER_MISSING.value,
    ErrorCode.BAD_REQUEST.value, ErrorCode.UNAUTHORIZED.value, ErrorCode.FORBIDDEN.value,
    ErrorCode.NOT_FOUND.value, ErrorCode.METHOD_NOT_ALLOWED.value, ErrorCode.VALIDATION_ERROR.value,
})
# permanent → ODMAH DLQ · transient → delayed retry (5·2^n; cap→DLQ)
# rate limit: error_code in ("RATE_LIMIT", ErrorCode.RATE_LIMITED.value) → delay = retry_after or 30
# RETRY_EXHAUSTED ostaje TRANSIJENTAN (vanjski delayed-retry je namjeran)
```

### 7.4 Split pragovi po kanalu
`_CHANNEL_SPLIT_LIMITS = {"whatsapp": 4000, "viber": 960}` (margina ispod tvrdih
limita 4096/1000). Bez viber praga poruke 1000-4000 znakova bile bi TIHO odrezane
truncationom servisa. Chunk idempotency key: `{to}:{md5(chunk)[:12]}:{ts_ns}:{idx}`,
svi chunkovi u JEDAN rpush.

### 7.5 Health/heartbeat
Periodički log: processed/failed/duplicates, `wa_sent/wa_retries` +
`viber_sent/viber_retries` (get_stats per servis), memory, DLQ dubine (alarm na
rast), pg pool, Lua SHA re-load; `WORKER_HEARTBEAT_FILE` touch (k8s exec
livenessProbe — worker nema HTTP port).

---

## §8 ENGINE — master tok (30 koraka; svaki korak = modul + state + error putanja)

```
━━ ULAZ ━━  1. POST /webhook/{whatsapp|viber} → HMAC (fail→401)
            2. dedup SET wh_dedup NX EX 60      3. XADD (+channel; retry×3→DLQ; 200)
━━ WORKER ━ 4. XREADGROUP    5. msg_lock NX 300s    6. per-sender lock
            7. engine.process_message — budžet 90s (timeout→HR poruka+ACK)
━━ SAFETY ━ 8. rate_limiter → cooldown    9. pii_scrubber (PRIJE LLM-a!)
           10. input_sanitizer → blok    11. identity.resolve (cache 30s;
               nepoznat broj → enrollment poruka, terminal)
           12. crisis_detector → hotline (terminal)
           13. special_intents → GDPR (audit) / welcome / handover (terminal)
━━ STANJE ━ 14. pending_params?  poruka = odgovor na pitanje → nastavi tamo
           15. pending_mutation? poruka = Da/Ne → §8a  16. pending_clarify? izbor 1/2/3
━━ ODLUKA ━ 17. router (§9): → {kind: action|clarify|answer, action?, params?, text?}
               Azure retry ×3 backoff; totalni fail → siguran HR fallback
           18. answer→27 · clarify→pošalji pitanje (kraj) · action→↓
━━ PARAMS ━ 19. validacija (akcija postoji? polja u shemi? tipovi?) fail→clarify
           20. coercion: HR datumi/brojevi → ISO/int (Europe/Zagreb)
           21. fali required → pending_params.save + HR pitanje (kraj; sljedeći
               turn ulazi na 14 i NASTAVLJA)
           22. codebook param → backend mapira (opcija C default) ili
               type_resolver fallback (dohvat šifrarnika → match; dvosmisleno→clarify)
           23. inject person_id/tenant_id iz identiteta (AI ih NIKAD ne generira)
━━ WRITE ━ 24. mutation → echo parametara ("Provjeri prije slanja: …") +
               "Potvrđuješ? (Da/Ne)" → pending_mut 300s (kraj turna)
               §8a "Da": exec lock 30s + stale-confirm guard (>90s re-ask;
               >30s re-validate identity) → 25 · "Ne": clear+"U redu" ·
               treće: "Nisam siguran…" (pending ostaje) · brojka 2/3→cancel,
               1→eksplicitni Da re-prompt
━━ EXEC ━━ 25. executor → api_gateway: OAuth token + x-tenant + Idempotency-Key
               + SSRF guard → poziv (budžet 15s, circuit breaker po servisu)
           26. 2xx→dalje · 4xx→api_error_translator→HR objašnjenje ·
               5xx/timeout→generička HR + pending OSTAJE (može opet "Da")
━━ IZLAZ ━ 27. llm_formatter → HR (§14)   28. conv_history.append (5, 30min)
           29. enqueue_outbound {to,text,channel,idempotency_key}
           30. outbound pump (§7.2) → send po kanalu → korisnik
```

---

## §9 ROUTING

### 9.1 CILJNI routing (NORMATIVAN): ~30 akcija direktno u LLM
```
korisnikov tekst + history[-3:] + identity sažetak
→ gpt-4o-mini tool-call nad SVIH ~30 shema iz actions.json (stanu u prompt —
  NEMA retrievala, NEMA action-pickera, NEMA scopinga)
→ { kind: "action" | "clarify" | "answer", action?, params?, text? }
→ nesigurno → TOP-3 clarify kartice (1/2/3; "nije točno" → reoffer)   [ostaje!]
→ dalje ista petlja: validator → params → confirm → execute
```
`action_registry.openai_tools()` pretvara §11 sheme u OpenAI tools;
`action_validator` = anti-halucinacija (nepoznata akcija/polje/tip → clarify,
NIKAD slijepo dalje). Točnost nosi KVALITETA OPISA u actions.json
(description + use_when + examples — §11), ne retrieval mašinerija.
Uvođenje: iza `V2_USE_ACTIONS=1` dok benchmark ≥ skela na oba seeda, zatim
postaje jedini put (§23.2 Faza 3/4).

### 9.2 ⚠ PRIJELAZNA SKELA (VIŠAK — umire u Fazi 4): Model A kaskada nad ~950
**Postoji iz JEDNOG razloga: `/actions` API još ne postoji (gate §24#1), a bot
mora raditi danas.** Ne brisati prije dokazane zamjene (mrtav bot = Gemini
lekcija), ali NE ULAGATI u nju ništa novo osim bug-fixeva.
```
korisnikov tekst
→ ACTION PICKER: POGLEDATI / UNIJETI / IZMIJENITI / IZBRISATI (+global intents)
→ scoped L3: anchor retrieval (top-50 kandidata za taj action-tip)
   → gpt-4o-mini tool-call (tool_choice="required") nad top-50 shema
→ nesigurno → TOP-3 clarify (isti mehanizam kao 9.1)
→ dalje ISTA petlja: params → confirm → execute
```
Detalji skele: `tool_schema_builder` SUPPRESSA `filter`/`useandfor`; anchor
cache na `ANCHOR_CACHE_PATH`; registar se generira iz M1 Swaggera
(`scripts/sync_tools.py` → `tool_data.json` 3.8MB + `processed_tool_registry.json`
3.4MB; boot-obavezan u skeli). **Kriterij smrti:** sposobnost dokazana na
/actions putu u produkciji → njen skela-put se briše (po sposobnosti, Faza 3);
kad SVE high-frequency prijeđe → cijela kaskada + registar van (Faza 4, §22.5).

---

## §10 PARAM LIFECYCLE — od rečenice do API polja (8 stanica)

```
"rezerviraj auto sutra od 9 do 15, hitno je"
① LLM EKSTRAKCIJA (opisi+examples u shemi su KRITIČNI za točnost)
② VALIDACIJA (akcija/polja/tipovi — izmišljeno → clarify)
③ COERCION "sutra 9h"→2026-07-05T09:00:00 · "12,5"→12.5 · "da"→true (Europe/Zagreb)
④ MISSING-REQUIRED LOOP: HR pitanje → odgovor SE PAMTI (Redis 300s) → sljedeće
   polje; "odustani"→abort; nova tema→clear+svježe routanje
⑤ CODEBOOK RESOLVE (§16.3: backend default; type_resolver fallback)
⑥ IDENTITY INJECT (person_id/tenant_id — AI ih NE puni)
⑦ PARAM ECHO u confirm poruci — korisnik VIDI točno što se šalje PRIJE slanja
⑧ SLANJE + BACKEND VALIDACIJA (strukturiran 400 → HR prijevod)
```
Station-by-station: svaka stanica ima definiran ishod (clarify / re-ask s
primjerom / pick-lista / "ne mogu dohvatiti profil" / HR objašnjenje) — **nijedna
ne propušta šutke**, i ništa se ne piše bez ⑦.

---

## §11 ACTIONS.JSON — glavni ugovor (F1; jedino što AI "vidi")

### 11.1 Schema
```jsonc
{
  "name": "snake_case ≤64",
  "ai": {
    "description": "kada koristiti",
    "use_when": ["primjeri fraza"],
    "parameters": {                          // SAMO što korisnik izgovori!
      "<param>": { "type": "string|integer|number|boolean|date|datetime",
                   "format": "date|date-time|''",
                   "description": "poslovni opis (OBAVEZNO)",
                   "examples": ["…"], "required": true,
                   "codebook": "CaseType|null" }   // šifrarnik marker
    }
  },
  "execution": { "method": "POST|GET", "action": "/actions/<name>" },
  "policy":    { "mutation": true, "inject": ["person_id","tenant_id"] }
}
```

### 11.2 Puna primjera (predložak za svih ~30)
```json
{ "name": "report_incident",
  "ai": { "description": "Prijava kvara, štete, nezgode ili tehničkog problema na vozilu.",
    "use_when": ["pukla mi je guma","auto ne pali","imao sam nezgodu","prijavljujem kvar"],
    "parameters": {
      "registration_plate": { "type":"string","required":true,
        "description":"Registracijska oznaka vozila na kojem je problem.",
        "examples":["ZG-1234-AB","ZG1234AB","moja rega"] },
      "description": { "type":"string","required":true,
        "description":"Opis kvara/štete korisnikovim riječima.",
        "examples":["pukla guma prednja lijeva","ogrebotina na vratima"] },
      "incident_type": { "type":"string","required":false,"codebook":"CaseType",
        "description":"Vrsta problema; ako korisnik ne kaže jasno, backend ostavlja default.",
        "examples":["kvar","šteta","nezgoda"] } } },
  "execution": { "method":"POST","action":"/actions/report-incident" },
  "policy": { "mutation":true,"inject":["person_id","tenant_id"] } }
```
```json
{ "name": "book_vehicle",
  "ai": { "description": "Rezervacija službenog vozila za određeni period.",
    "use_when": ["rezerviraj auto","trebam vozilo sutra","bookiraj kombi za petak"],
    "parameters": {
      "date_from": { "type":"datetime","format":"date-time","required":true,
        "description":"Početak rezervacije.","examples":["2026-07-05T09:00:00","sutra 9h"] },
      "date_to":   { "type":"datetime","format":"date-time","required":true,
        "description":"Kraj rezervacije.","examples":["2026-07-05T15:00:00","do 15h"] },
      "vehicle_hint": { "type":"string","required":false,
        "description":"Ako korisnik traži konkretno vozilo. Prazno = backend nudi slobodno.",
        "examples":["kombi","ZG-1234-AB"] } } },
  "execution": { "method":"POST","action":"/actions/book-vehicle" },
  "policy": { "mutation":true,"inject":["person_id","tenant_id"] } }
```

### 11.3 Tri vrste parametara (test za razvrstavanje)
| Pitanje | Da → |
|---|---|
| Korisnik ovo izgovori? | `ai.parameters` |
| Dolazi iz "tko je korisnik"? | `policy.inject` |
| Interni kod / default / computed? | backend (bot ga NE spominje) |

### 11.4 Operativna semantika
Boot fail-fast (malformiran file → RuntimeError, pod se ne digne) · reload bez
deploya (`/admin/cache-invalidate`) · verzioniranje ADITIVNO (novi param uvijek
`required:false`; breaking = novo ime `_v2` — k8s rollout drži oba poda živa) ·
per-action enable flag (akcija bez prošlog contract testa = OFF) ·
`V2_USE_ACTIONS=0` je kill-switch.

---

## §12 EXECUTOR + API GATEWAY (postojeći, reuse za sve)

- **Async uvijek** (`api_gateway.call`) — nikad sync `requests` (blokira event-loop).
- Headeri: `Authorization: Bearer` (TokenManager cache; 401→auto refresh),
  `x-tenant: {tenant_id}`, `Idempotency-Key` (UUID stabilan kroz retry) na SVAKU
  mutaciju.
- **SSRF guard** (dozvoljena samo M1/Business baza), **circuit breaker** po
  servisu, executor budžet **15s**.
- **List serializacija query paramova** (živo naučeno): `Filter` vrijednosti se
  joinaju s `" and "`, ostale liste zarezom; lowercase-safe `=`.
- F1 grana: `execution.action` staza je puna (`/actions/…`), `service=""`,
  `base_url_override=settings.BUSINESS_API_URL` (default MOBILITY_API_URL).
- 4xx → `api_error_translator` (strukturiran `{error_code,field?,message}` →
  deterministički HR prijevod; cache 1h) · 5xx/timeout → generička HR poruka,
  pending confirm OSTAJE.

---

## §13 AUTH PREFLIGHT (`services/auth_preflight.py`) — garancija PP1

Missing OAuth grant mora biti **činjenica na startu**, ne produkcijski 403
(živo dokazan failure mode 2026-05-30: "403 scope — bot nema ovlasti").

- **Sloj 1 — introspection:** dekodiraj VLASTITI JWT payload (bez signature
  verify), `granted_scopes` (string i lista oblik; `scp` fallback; ne-dict
  payload → `{}`), diff protiv `MOBILITY_REQUIRED_SCOPES` (unset → skip diffa).
- **Sloj 2 — route probe:** benign GET po ruti (`/Persons?Rows=1`; F1:
  `probe_routes_from_actions` — GET akcije, entryji bez patha se PRESKAČU) →
  `{ruta: status}`. 401/403 = auth problem; transport/-1/5xx NIJE.
- **`PreflightReport.verified`** = token dohvaćen BEZ greške I (ako je probea
  bilo) bar jedan PRAVI HTTP odgovor (≥200; 403 JE dokaz — problema). Mrtvi
  kredencijali tipično daju status 0/-1, ne 401!
- Gate semantika: default **log-only** (`NEVERIFICIRAN` WARN);
  `AUTH_PREFLIGHT_STRICT=1` → RuntimeError ako `not ok OR not verified` (ili
  preflight sam pukne) — **fail-closed**. Hook: worker startup + readiness
  skripta (uz live kredencijale readiness traži i `verified`).

---

## §14 FORMATTER (`llm_formatter.py`) — JSON → hrvatski

Pravila: **grounding** (samo podaci iz JSON-a, ništa se ne izmišlja) · izlazni
PII scrub · liste: truncation s `ukupno_stavki`/`prikazano_prvih` ("imaš 27,
prikazujem 10") · envelope-aware prune (M1 envelope ključevi: `Data/data/Result/
Results/Items/items/value` — dok M1 ne potvrdi ugovor, §18 #1) · odgovor ciljano
<500 tokena · datumi u HR formatu.

---

## §15 KANALI

### 15.1 Načelo
Mozak (engine) je **channel-agnostičan**. Kanal je tag na rubu: webhook ga upiše
u stream entry, outbound entry ga nosi, pump po njemu bira servis. Novi kanal =
inbound parse + outbound send, Nula izmjena engine-a.

### 15.2 WhatsApp + Viber (IMPLEMENTIRANO — subclass seam)
`WhatsAppService` drži SVU retry/backoff/error logiku (Infobip-platformska) i
4 hooka. WA slanje: `POST https://{INFOBIP_BASE_URL}/whatsapp/1/message/text`,
payload `{"from": broj, "to": broj, "content": {"text": …}}`, headeri
`Authorization: App {INFOBIP_API_KEY}` + JSON. `ViberService(WhatsAppService)`
overridea SAMO:
```python
MAX_MESSAGE_LENGTH = 1000            # WA: 4096
ENDPOINT_PATH = "/viber/2/messages"  # WA: /whatsapp/1/message/text
SPAN_NAME = "viber_service.send"
_default_sender() → settings.VIBER_SENDER      # registrirano IME, ne broj!
build_payload() → {"messages":[{"sender":IME,"destinations":[{"to":msisdn}],
                                "content":{"text":…,"type":"TEXT"}}]}
_payload_recipient() → payload["messages"][0]["destinations"][0]["to"]
```
**⚠ MINA (naučeno, ne ponoviti):** success-log koji čita `payload["to"]` na
Viber obliku baca KeyError NAKON isporuke → guta ga retry petlja → **duplicirani
sendovi**. Zato je recipient čitanje HOOK. Uspjeh se određuje SAMO HTTP statusom
(200/201); messageId ekstrakcija već pokriva `messages[]` response oblik; auth
identičan (`App {key}`).
**⚠ OPS PREREQUISITE:** Viber business sender mora biti registriran/odobren kod
Infobipa (dani-tjedni!) — kod je spreman, kanal se pali configom
(`VIBER_SENDER` + subscription → `/webhook/viber`). Inbound payload oblik
potvrditi u sandboxu (parser je tolerantan; nepoznato se GLASNO loga).

### 15.3 M365 Copilot preko MCP (F-M365 — gradi se čim /actions postoji)
```
Teams user → COPILOT JE MOZAK (razumije/ekstrahira/bira tool) → MCP protokol
→ naš services/mcp/server.py:
   • tools = ISTE akcije iz actions.json (jedan izvor istine)
   • identitet: user email → GET /Persons?Filter=Email(=)… → person/tenant
     (isti strict-binding; Email polje verificirano u output_keys)
   • write akcije: MCP annotation "requiresConfirmation" → potvrdu renderira
     COPILOT UI (naš Da/Ne gate je za chat kanale)
   • ⚠ auth SAMOG servera: Entra ID validacija — otvorena odluka (§22 #8)
→ isti executor → /actions/* → rezultat → Copilot formatira
[V2Engine je ZAOBIĐEN — mi dajemo alate, Copilot je mozak]
```

### 15.4 Web (F-Web)
Sync `/chat` fasada u main.py koja interno zove ISTI engine (bez dupliranja) —
messaging kanali ostaju async (webhook→stream→worker; Infobip očekuje brz 200).

### 15.5 Usklađenost sa šefovim overview dokumentom (Fleet AI Copilot v2.1)
Njegovih 7 komponenti → naša implementacija — dizajn JE njegov dizajn, razrađen:

| Šefova komponenta | Naše | Napomena |
|---|---|---|
| 1 Channels (WA/Viber/Web/M365) | webhook rute + whatsapp/viber_service; Web=15.4; M365=15.3 | WA živ; Viber kod gotov |
| 2 AI Backend (thin) | V2Engine | postaje thin migracijom orkestracije u /actions |
| 3 OpenAI (decision+conversation) | llm_router + llm_formatter | + Da/Ne gate (on ga u QB mailu sam traži — human-in-the-loop) |
| 4 Tool Config (ai/execution) | `config/actions.json` (§11) | identična struktura kao njegov book_vehicle primjer |
| 5 MCP (execution+auth) | danas executor+gateway+token_manager (funkc. ekvivalent); cilj + `mcp/server.py` | OBAVEZAN — M365 kanal je u njegovom docu |
| 6 Business API /actions | backendova strana | ugovor §17.1; World A/B §24#1 |
| 7 Domain/Granular API | 950 M1 ruta | u cilju ih bot NE zove direktno |

**3 NAMJERNE devijacije (obrazložene, ne slučajne):**
1. **Async ingress za messaging** umjesto sync `/chat` — Infobip očekuje brz
   200, obrada traje sekunde, retry semantika; sync `/chat` postoji kao fasada
   za Web kanal (15.4), isti engine.
2. **Da/Ne mutation gate između decision i execution** — "OpenAI odlučuje"
   znači *bira akciju*, ne *izvršava bez potvrde* (i njegov QB zahtjev).
3. **Safety slojevi ostaju u botu** (crisis/PII/injection/GDPR) — to je
   pravna/etička obveza kanala prema korisniku, ne "business logika" koja
   seli u /actions.
Njegov MCP input `{tool, input, user:{email,phone,token}}` ≡ naš contract:
`tool`→ime akcije, `input`→poslovni body, `user`→identity inject (kod nas
razriješen PRIJE poziva; per-user token nije primjenjiv na WA kanalu).

---

## §16 TENANTI

### 16.0 Kako tenant logika radi END-TO-END (jedan pogled)
```
 poruka s broja +385 99 …
   │
   ├─ 1. tenant_resolver / identity.resolve(phone)
   │      Postgres user_mappings + Redis cache (tenant_phone: 300s /
   │      v2:identity: 30s); miss → GET /Persons?Filter=Phone(=)…
   │      (NSN contains-fallback za 3 živa formata broja + post-verifikacija)
   │
   ├─ 2. response NOSI TenantId  ← M1 JE IZVOR ISTINE, bez preseta
   │      STRICT BINDING: nema TenantId → korisnik ODBIJEN (enrollment
   │      poruka); tenant NIKAD iz env-defaulta ni iz teksta poruke
   │
   ├─ 3. x-tenant: {TenantId} na SVAKOM API pozivu → izolacija podataka
   │
   ├─ 4. bot-side postavke: tenant_settings red u DB (lazy-kreiran s
   │      defaultima na PRVI susret; cache tenant_cfg: 300s) —
   │      jezik, bot_status, actions_enabled per akcija
   │
   └─ 5. per-tenant ŠIFRARNICI (CaseType…): backend mapira semantiku (§16.3)

 NOVI TENANT U M1 → mi ne radimo NIŠTA (korak 1-4 se dogodi sam na prvu poruku).
```
**Presuda za `config/tenants/` folder (da bude kristalno):** on NIJE dio tenant
logike gore. Sadrži samo `_default/tool_subset.json` = dev-seed za
`catalog_scoper` koji u 950-SKELI sužava katalog alata per tenant. Umire sa
skelom (Faza 4, §22.5) — per-tenant uključivanje akcija u cilju je
`tenant_settings.actions_enabled` u bazi, mijenja se admin pozivom bez deploya.

### 16.1 Izvor istine = M1 backend (bot NE kreira i NE presetira tenante)
```
MEHANIZAM 1 — LAZY (ŽIV, dokazan): prva poruka → identity.resolve(phone)
  → GET /Persons → response nosi TenantId (strict binding; bez njega odbij)
  → tenant "stigne sa svakim korisnikom", NULA pripreme
MEHANIZAM 2 — BULK SYNC (opcionalan boost): GET /Tenants → upsert svih
  (⚠ ovisi o scope za bulk listing — NIJE uvjet, lazy radi bez toga)
```
Novi tenant u M1 → **mi ne radimo ništa**: prva poruka vozača lazy-kreira
`tenant_settings` red s defaultima. Zero deploy, zero commit.

### 16.2 Bot-side postavke (F1): Postgres + Redis cache + Admin API
`tenant_settings` (§5.2) — JSONB u bazi je ISPRAVAN format (problem je bio
*file u repou* = commit+deploy za izmjenu, ne JSON sam). Read-through cache
`tenant_cfg:{id}` TTL 300s, invalidate na admin izmjenu (preslika dokazanog
`tenant_resolver` patterna). `config/tenants/` folder = samo dev-seed.
GDPR offboarding tenanta: purge settings+dokumenti+embeddings+cache (dry-run pa
stvarno, kao gdpr-process).

### 16.3 Šifrarnici (CaseType i sl. — per-tenant kodovi!)
```
Tenant A: 1=Kvar 2=Šteta 3=Nezgoda · Tenant B: 1=Nezgoda 2=Kvar 4=Vandalizam
OPCIJA C (default): AI šalje SEMANTIKU ("kvar") → Business API mapira po tenantu
OPCIJA B (fallback/World B): type_resolver: GET /…Types → [(id,naziv)] →
  match(user_text) → id; dvosmisleno → clarify pick-lista
OPCIJA A (statični enum): NIKAD — puca na per-tenant kodovima
```

---

## §17 BUSINESS API — vanjski ugovori

### 17.1 Ugovor po akciji (checklist za backend tim PRIJE gradnje)
| # | Zahtjev | Zašto |
|---|---|---|
| 1 | ruta + input DTO (čista poslovna polja) | granica AI↔backend |
| 2 | strukturiran error `{error_code, field?, message}` za SVE 4xx | deterministički HR prijevod |
| 3 | backend mapira šifrarnike (opcija C) | per-tenant kodovi |
| 4 | **honoriranje `Idempotency-Key`** (dedup ≥10 min) | timeout+retry = dupla rezervacija bez toga |
| 5 | READ liste vraćaju `{items, total}` + max page size | "imaš 27, prikazujem 10" |
| 6 | objavljeni rate-limiti (429 + `Retry-After`) | backoff kalibriran naslijepo |
| 7 | **pisana potvrda OAuth scope granta za SVE /actions/* rute** PRIJE deploya | ŽIVO dokazan failure (403 scope) |
| 8 | timezone semantika naive ISO datetimea (preporuka: Europe/Zagreb ili offset format) | rezervacije pomaknute 1-2h = tiha korupcija |

Primjer backend implementacije (skica handlera): resolve plate→VehicleId →
codebook map → orkestracija (create incident + block calendar) → čisti sažetak
`{status, incident_id, vehicle_status}`.

### 17.2 M1 addendum (današnji granularni API — 8 otvorenih stavki)
envelope ugovor (gdje su redovi + total) · paginacija (max Rows? stabilnost
offseta? default sort) · **Idempotency-Key honoriranje** · **timezone naive
datetimea** · strukturiran 4xx shape · rate limiti · kanonski format telefona u
/Persons (živo izmjereno 3 formata → NSN contains-fallback + post-verifikacija) ·
golden sample responses (top ~20 endpointa). Prioritet: 3+4 (tihe korupcije).

### 17.3 Contract testovi (harness POSTOJI — `tests/contract/`)
- **Fixture po akciji**: `{action, method, path, request, response, errors[]}`;
  placeholderi `<any>/<any-string>/<any-int>/<any-number>/<any-bool>` (bool NIJE
  any-int); superset matcher (backend smije vraćati više).
- **OFFLINE (uvijek u CI)**: struktura, placeholder sintaksa, `request_override`
  ⊆ request, merged request ⊆ shema akcije (kad actions.json postoji).
- **LIVE (gated)**: `CONTRACT_BASE_URL`+`CONTRACT_BEARER_TOKEN`; **POST fixturei
  SKIPaju bez `CONTRACT_ALLOW_MUTATIONS=1`** (guard protiv pisanja u okolinu s
  pravim podacima); error slučajevi se šalju živo (override → očekuj 4xx + match
  expect); bearer token se drži izvan test-frame localsa (pytest -l ne smije
  procuriti token u CI log).
- **PRAVILO:** akcija ide ON u produkciji TEK kad prođe offline + live contract
  + smoke. Nijedna "na ruke". Fixturei su ujedno provider-contract za backend CI.

---

## §18 SCENARIJI (sequence sažeci — S1-S10)

```
S1 READ: "pokaži moja zadnja putovanja" → list_trips → GET → {trips,total:27}
   → "Zadnjih 10 od ukupno 27…"                    (1 LLM decision + 1 format)
S2 WRITE+Da/Ne: report_incident → echo+confirm → "Da" → exec lock → POST →
   INC-5521 → clear pending → invalidate identity → HR potvrda
   "Ne"→"U redu, odustajem." · treće→"Nisam siguran…" (pending ostaje)
S3 MISSING PARAM: book_vehicle bez date_to → "Do kada trebaš vozilo?" →
   "do 15" → parse → nastavi na confirm ("odustani"→abort; nova tema→clear)
S4 ŠIFRARNIK CLARIFY: match=None → "Kakav problem? Kvar, Šteta ili Nezgoda?"
S5 NEPOZNAT BROJ: enrollment poruka, terminal — NIJEDAN API poziv bez tenanta
S6 SAFETY: rate-limit cooldown · injection blok · crisis hotline · GDPR audit
S7 BACKEND GREŠKA: 409 VEHICLE_UNAVAILABLE → HR "Nema slobodnih vozila u tom
   periodu…" · 5xx → circuit breaker + pending OSTAJE
S8 RAG (F2): kind=answer → retriever top-3 chunka tenant dokumenta →
   "odgovori SAMO iz priloženog, citiraj izvor" → bez ijednog /actions poziva
S9 VIBER: isti koraci 4-28, samo channel tag na rubovima (E2E test: isti
   razgovor s oba taga)
S10 COPILOT: §15.3 (Copilot mozak, mi alati)
```

---

## §19 SIGURNOST / GDPR checklista

HMAC fail-closed · PII scrub pre-LLM + PIIScrubFilter na logovima (maskirani
telefoni) · input/output sanitizer · SSRF guard · tenant strict-binding ·
NetworkPolicy zero-trust + non-root container · admin token auth · GDPR:
consent, erasure endpoint (dry-run pa stvarno, bypass_cache resolve), audit
liste, tenant offboarding purge · secreti u k8s Secretu (rotacija = update +
rolling restart; **rotirati sve iz ngrok ere**) · LLM verzija PINirana
(model bump = benchmark gate na oba seeda).

---

## §20 DEPLOY (k8s/ — 11 manifesta + README runbook)

| Komponenta | Konfiguracija | Zašto |
|---|---|---|
| bot-api ×2 | HPA 2-4, RollingUpdate maxUnavailable=0, PDB, preStop sleep | webhook NIKAD ne pada tijekom deploya (Infobip ne čeka) |
| bot-worker ×1 | Recreate | per-sender in-process redoslijed; ×N recept: per-sender lock → Redis (README) |
| Redis | AOF everysec + **noeviction**, 384MB | queue je load-bearing — eviction = izgubljene poruke |
| Postgres | in-cluster ili managed | user_mappings + tenant_settings |
| Ingress | nginx + cert-manager TLS | javni HTTPS webhook (zamjena za ngrok) |
| migrate Job | alembic upgrade head | prije rollouta |
| NetworkPolicy | default-deny | zero-trust |
| worker liveness | exec provjera starosti WORKER_HEARTBEAT_FILE | nema HTTP porta |
| anchor cache | PVC na ANCHOR_CACHE_PATH | topli start routera |

Runbook: `kubectl apply -k k8s/` → migrate wait → `curl /webhook/whatsapp`→"ok"
→ Infobip webhook na `https://<host>/webhook/whatsapp`; **Viber subscription →
`/webhook/viber` + VIBER_SENDER u secretima**. Rollout/rollback: `set image` /
`rollout undo`; worker rollout = do ~60s buffera u streamu, bez gubitka.
Pilot smije na 1 VM + docker-compose (~30-60€; Infobip retry ublažava restart
gap); produkcija AKS (~150-250€) — ista slika, prelazak je runbook, ne prepis.

### 20.1 SCALING ODLUKE — što DA, što NE (i zašto; ne gomilamo tehnike)

| Tehnika | Odluka | Obrazloženje |
|---|---|---|
| **Load balancer** | ✅ IMAMO | k8s ingress-nginx pred api ×2 — webhook HA |
| **Horizontalno API** | ✅ IMAMO | stateless api, HPA 2-4, maxUnavailable=0 |
| **Queue-based load leveling** | ✅ SRŽ DIZAJNA | Redis stream apsorbira burst; latencija raste, gubitka nema |
| **Caching** | ✅ SVUGDJE | Redis: identitet 30s · tenant 300s · OAuth token · 4xx prijevodi 1h · tenant_cfg 300s — svaki s TTL-om i invalidacijom |
| **Connection pooling** | ✅ IMAMO ×3 | SQLAlchemy pool (5+10) · httpx AsyncClient reuse (TLS handshake 1×) · Redis pool (100) |
| **Rate limiting** | ✅ IMAMO ×3 | per-IP na webhoooku (200/60s) · per-phone u engineu (m/h bucketi) · admin API (30/min) |
| **Circuit breaker** | ✅ IMAMO | po M1 servisu; njihov outage ne ruši bota |
| **Horizontalno WORKER** | ⏸ NE SADA (×1 by design) | per-sender redoslijed poruka; consumer grupa VEĆ podržava ×N — recept (per-sender lock → Redis) primijeniti na pragu: sustained >75-150 msg/min |
| **DB replike** | ❌ NE | podaci KB/MB reda, ~1 lookup po novom korisniku (dalje cache); replike = trošak bez koristi. Backup=PITR, ne replika |
| **Redis HA/cluster** | ⏸ NE ZA PILOT | AOF everysec + noeviction + brzi k8s restart; prag: >500 aktivnih korisnika → managed Redis |
| **PgBouncer** | ⏸ NE | SQLAlchemy pool dovoljan; prag: pool utilizacija trajno >80% |
| **Sharding** | ❌ NE | apsurd za ovaj volumen — YAGNI |
| **CDN/edge** | ❌ NE | nema statičkog sadržaja |
| **LLM skaliranje** | kvota, ne infra | Azure TPM/RPM kvota se diže zahtjevom; azure_rate_guard + retry već štite |

**Bottleneck istina:** usko grlo NIJE naša infra nego LLM latencija (0.5-2s/
poziv) i M1 API — zato je dizajn async s redom u sredini, a ne sync lanac.

### 20.2 Infra skica s ×N oznakama (buduće stanje = današnje + pragovi)

```
                    Infobip / M365 / Web
                          │ HTTPS
              ┌───────────▼────────────┐
              │ INGRESS-NGINX (LB, TLS) │            ← load balancer, cert-manager
              └───────────┬────────────┘
        ┌─────────────────┼─────────────────┐
   ┌────▼────┐       ┌────▼────┐            │
   │ api pod │  ×2-4 │ api pod │  (HPA)     │       ← stateless, rate-limit/IP
   └────┬────┘       └────┬────┘            │
        └────────┬────────┘                 │
           ┌─────▼──────┐                   │
           │   REDIS ×1 │ AOF+noeviction    │       ← queue+cache+state (SVE TTL)
           │            │ [prag >500 korisn.│
           └─────┬──────┘  → managed/HA]    │
           ┌─────▼───────────────┐   ┌──────▼──────┐
           │ worker ×1 (Recreate)│   │ mcp/server  │ (F-M365)
           │ [prag >150 msg/min  │   └──────┬──────┘
           │  → ×N + Redis lock] │          │
           └──┬────────┬─────────┘          │
      ┌───────▼──┐  ┌──▼───────────┐        │
      │ POSTGRES │  │ AZURE OPENAI │        │
      │ ×1 (PITR │  │ (kvota+PIN)  │        │
      │  backup) │  └──────────────┘        │
      └──────────┘                          │
                 ┌──────────────────────────▼───┐
                 │ M1 CLOUD: IdentityServer +    │
                 │ /actions (F1) + Domain API    │
                 └───────────────────────────────┘
  Svaka kutija: što je ×N (api), što je ×1 s definiranim PRAGOM za rast
  (worker/Redis), što je vanjska ovisnost (LLM kvota, M1 SLA).
```

---

## §21 TESTOVI + DEFINICIJA GOTOVOG

### 21.1 Trenutno stanje suite (referentne brojke)
**1754 passed / 9 skipped (by design: live contract + actions.json cross-check)
/ 0 failed · coverage 87% (CI gate ≥85% na services+config) · ruff čist.**
E2E: 4 puna razgovora kroz produkcijski factory s pravim registrom (read,
write+confirm, decline, reoffer) s **exact-call asertacijama**
(method/service/path/body/tenant/idempotency) + Viber e2e pipeline.

### 21.2 Acceptance = POTPUNOST PO AKCIJI, ne samo agregat

**Akcija POSTOJI tek kad ima SVIH 6 (nijedna "na ruke"):**
```
 1. actions.json entry (two-level opisi: tehnički type/format + poslovni
    description + use_when + examples)
 2. offline contract fixture (request/response/errors s placeholderima)
 3. LIVE contract PASS protiv dev Business API-ja (uklj. error slučajeve)
 4. smoke na dev M1 (stvarni poziv, stvaran odgovor)
 5. e2e razgovorni test (poruka→…→HR odgovor, exact-call asertacije)
 6. uključena u benchmark golden set
```
**Sustav je GOTOV kad:** SVIH ~30 akcija prošlo svih 6 gore + agregat
**90/97/0** (top-1 / top-3 / halucinirane) na dual-seed golden setu + 2 tjedna
zelenog pilota + showstopper registar bez 🔴 (svi 🟡 s potvrđenim odgovorom) +
`verify_production_readiness.py` PASS uz live kredencijale (auth preflight
`ok AND verified`). **Capability tablica ~30 akcija sa statusom 6 kvačica se
vodi u repou** (počinje s 5 iz Faze 1: book_vehicle, add_mileage,
report_incident, list_trips, vehicle_status — fixturei za 3 već postoje).

### 21.3 Garancija "uvijek odgovor" — enumeracija SVIH izlaza (čuvati tablicu!)
20 putanja, svaka s testom: HMAC fail(401) · dup webhook · Redis pun(503→retry)
· rate-limit · injection · nepoznat broj · crisis · GDPR · clarify/param/confirm
pitanja · akcija OK · 4xx HR · 5xx+pending · engine None/timeout/iznimka
(worker trojka) · outbound transient retry ×3 · permanent/iscrpljen → DLQ+alarm
· crash-nakon-slanja bez duplikata (sent:) · pod restart (stream čuva) ·
iznimka u pump petlji (DLQ + petlja živi). **Svaki novi izlaz = novi redak+test.**
Honest bound: finalna DOSTAVA je na WhatsApp/Viber platformi.

---

## §22 SHOWSTOPPER REGISTAR (živa tablica — nijedan rizik nepraćen)

| # | Stavka | Simptom | Mitigacija | Status |
|---|---|---|---|---|
| 1 | World A/B — tko gradi /actions | nema se što zvati | odluka na sastanku; World B stopgap = poopćeni flow_engine | 🔴 GATE za F1 |
| 2 | OAuth scope za /actions/* | SVAKI poziv 403 (živo dokazano) | auth_preflight (ok AND verified) + ugovor §17.1#7 | 🟡 mitigacija ŽIVA |
| 3 | Business API hosting | 404/DNS | `BUSINESS_API_URL` env | 🟡 config spreman |
| 4 | Viber sender registracija | kanal mrtav ma kakav kod | **kod GOTOV**; registraciju pokrenuti ODMAH (dani-tjedni lead) | 🟡 čeka ops |
| 5 | Timezone /actions datetimea | rezervacije ±1-2h | ugovor §17.1#8 | 🟡 |
| 6 | Bulk GET /Tenants scope | bulk sync ne radi | lazy put NE ovisi — radi bez | 🟢 |
| 7 | Email filter u /Persons | Copilot identitet | potvrda uz filter-schema; Phone(=) živo radi | 🟡 (F-M365) |
| 8 | Auth MCP servera (Entra) | tuđi pozivi na akcije | dizajn u F-M365 | 🟡 |
| 9 | Azure TPM/RPM kvota | 429 oluje | retry+backoff živ; kvota se diže | 🟢 |
| 10 | Dostava (platforma) | user blokirao bota | DLQ+alarm; trajni bound | 🟢 |
| 11 | Infobip SPOF | svi kanali down | servis-seam čini providera zamjenjivim; DLQ preživi | 🟢 |
| 12 | LLM model drift | tiha regresija | PIN verzija + benchmark gate | 🟢 |
| 13 | Redis SPOF | kratki zastoj (AOF čuva) | prag >500 aktivnih → managed/HA | 🟡 prag |
| 14 | Jutarnji burst | latencija raste | queue apsorbira (~75-150 msg/min/worker); prag → ×N recept | 🟡 prag |
| 15 | Poison message | crash loop | msg_lock + DLQ bez auto-retryja — testom dokazano | 🟢 |
| 16 | Rotacija secreta (ngrok era!) | procurjeli kredencijali | k8s secret + rolling restart | 🟡 TODO ops |
| 17 | Proaktivne poruke vs WA 24h | neisporuka | samo reply danas; proaktivno TEK s template approvalom | 🟢 ograđeno |

Performance okviri: latencija turna tipično 2-6s (capovi 15s executor / 90s
turn) · trošak ~$0.001-0.002/poruci (gpt-4o-mini, 2 poziva/turn) · kapacitet
~120 vozača komotno na 1 workeru.

### §22.5 REGISTAR VIŠKA — egzaktno što je prijelazna skela (izmjereno 2026-07-04)

**Ukupno viška: ~7.2 MB configa + ~4.5k linija koda + pripadni testovi.**
Sve umire u Fazi 4 (osim gate-ovisnih redaka) — svako brisanje iza testa +
benchmarka, po Strangler Fig pravilu.

| Višak | Veličina | Umire | Zamjena |
|---|---|---|---|
| `config/tool_data.json` | 3.8 MB (950 alata) | Faza 4 | `actions.json` ~30 akcija (~30 KB) |
| `config/processed_tool_registry.json` | 3.4 MB (boot-obavezan u skeli) | Faza 4 | ne treba — action_registry čita actions.json |
| Stage A u `router/llm_router.py` | dio od 375 L | Faza 4 | 30 shema direktno u prompt |
| `router/catalog_scoper.py` (korisnici: engine, llm_router) | 210 L | Faza 4 | ne treba (nema što subsetirati) |
| `router/anchor_vocab.py` + anchor cache | 340 L | Faza 4 | ne treba |
| `router/tool_schema_builder.py` (950-specifična suppresija) | 270 L | Faza 4 | `action_registry.openai_tools()` |
| `v2/intent_type.py` (action picker pre-stage) | 159 L | Faza 4 ako mjereno suvišan | direktan tool-call |
| `v2/driver_basics.py` (anchor index za self-pitanja) | 279 L | Faza 4 ako mjereno suvišan | akcija/RAG put |
| `services/registry/` (swagger_parser 660 + embedding_engine 462 + entity_mappings 547 + tool_store 106 + __init__ 225) | 2.0k L | Faza 4 | akcije se pišu ručno, ne generiraju |
| Skripte: `sync_tools`, `regenerate_anchors`(×2), `regenerate_tkb_examples`, `regenerate_tool_data` | 5 skripti | Faza 4 | — |
| `config/tenants/` (_default dev-seed za catalog_scoper) | 2 filea | Faza 4 | `tenant_settings.actions_enabled` u DB (§16) |
| 950-vezani benchmarki (`tests/benchmarks/*` paraphrase setovi) | ~10 fileova | Faza 4 | actions golden set (isti dual-seed protokol) |
| `v2/flow_engine.py` | 870 L | **GATE**: World A → umire (backend orkestrira); World B → POSTAJE BFF jezgra (nije višak) | /actions ili on sam |

**NIKAD višak (trajna lista — brisanje = neispravan build):** crisis_detector ·
pii_scrubber · input_sanitizer · output_sanitizer · special_intents+gdpr_audit ·
mutation_gate+pending_mutation · pending_params+param_ui · pending_clarify+
clarify_ui · identity · rate_limiter · conversation_history · api_gateway
resilience (breaker/SSRF/idempotency) · formatter · kanali (whatsapp/viber
service) · type_resolver · api_error_translator · telemetry · k8s.

---

## §23 REDOSLIJED GRADNJE

### 23.1 Za graditelja OD NULE — dva patha, izbor NIJE tvoj nego stanja svijeta

**Pravilo izbora:** postoji li `/actions` Business API (ili World B BFF nalog)?
**DA → PATH T (ciljni build).** NE → PATH P, i to samo uz eksplicitnu potvrdu
da se gradi prijelazno stanje.

**PATH T — CILJNI build (bez ijednog retka skele iz §22.5):**
```
 1. config.py (env §4, uklj. BUSINESS_API_URL) + errors.py + database + skeleton
 2. api_gateway + token_manager (+SSRF, breaker, Idempotency-Key) — testovi
 3. webhook_simple (HMAC, dedup, parse, channel, XADD, DLQ) — test_webhook*
 4. worker (XREADGROUP, msg_lock, ACK protokol, outbound pump,
    klasifikacija §7.3, split §7.4, heartbeat) — test_worker_*
 5. WhatsAppService (s 4 hooka!) + ViberService — test_*_service
 6. engine SAFETY slojevi (§8 koraci 8-13) — po modulu test
 7. identity + tenant_resolver (strict binding) + tenant_config (§16)
 8. actions.json (~30 akcija, §11) + action_registry + action_validator
 9. router = 1 LLM tool-call nad 30 shema (§9.1) + top-3 clarify + formatter
10. pending trojka (params/mutation/clarify) + param_ui + coercion
11. executor → POST /actions/* + api_error_translator + telemetry + history
12. auth_preflight (§13, probe IZ actions.json) + contract harness (§17.3:
    fixture po akciji!) + readiness
13. E2E razgovori nad akcijama + actions golden set benchmark
14. k8s deploy + runbook → pilot → §21.2 potpunost po akciji
    [NIGDJE: tool_data, registry machinery, sync_tools, anchor, action picker]
```

**PATH P — PRIJELAZNI build (današnja stvarnost repoa; skela markirana):**
```
 koraci 1-7 identični PATH T (jezgra je ISTA — zato migracija uopće radi)
 8p. ⚠[SKELA] registar: sync_tools iz Swaggera → tool_data + processed_registry
 9p. ⚠[SKELA] router = Model A kaskada (§9.2: picker + anchor top-50 + scoping)
10-14. identično PATH T (pending/executor/preflight/e2e/k8s — sve ostaje
    kroz migraciju; benchmark = dual-seed nad 950-imenima dok skela živi)
 → zatim §23.2 Faza 1+ dodaje /actions PORED skele i skida je po sposobnosti
```

### 23.2 Migracija na /actions (Strangler Fig — NIŠTA se ne briše prije zamjene)
```
FAZA 0 (✅ GOTOVA 2026-07-04): mrtav kod van (8 fileova) · Viber adapter ·
  error-code fix · auth_preflight + contract harness
FAZA 1 (gate: World A/B): actions.json (prvih ~5: book_vehicle, add_mileage,
  report_incident, list_trips, vehicle_status) + action_registry +
  action_validator + executor grana + tenant_settings — SVE iza V2_USE_ACTIONS,
  stari put ostaje default; benchmark ≥ stari na oba seeda
FAZA 2: sposobnost-po-sposobnost (+fixture+flag+benchmark u CHANGELOG;
  regresija = revert TE akcije)
FAZA 3: povlačenje stare putanje PO sposobnosti TEK kad je nova dokazana u
  produkciji (pilot+telemetrija)
FAZA 4 (kraj): Stage A van · tool_data 950→30 · SHA1 alias van ·
  processed_tool_registry van — svako brisanje iza testa+benchmarka
FAZA 5 (opcionalno): mcp/server.py za Copilot · F2 RAG · F-Web /chat
NIKAD ne brisati (bez obzira na fazu): crisis_detector · pii_scrubber ·
  input_sanitizer · special_intents · mutation_gate+pending_mutation ·
  pending_params+param_ui · identity · rate_limiter · gateway resilience · k8s
```

---

## §24 OTVORENI GATEOVI (izvan koda — čekaju odluke/treće strane)

1. **World A/B** (tko gradi /actions; sastanak s backend vlasnikom) — gate F1.
   World A = backend gradi (bot tanak, logika reusable za QB/Copilot);
   World B = bot BFF (poopćeni flow_engine; brzo ali debeo bot) — preporuka:
   hibrid (B za 2-3 akcije odmah → migracija na A).
2. OAuth scope grant za /actions (pisana potvrda).
3. `BUSINESS_API_URL` host.
4. **Viber sender registracija kod Infobipa (ops; NAJDUŽI lead time — pokrenuti
   odmah).**
5. M1 addendum odgovori (§17.2; prioritet idempotency + timezone).
6. Bulk /Tenants scope (nije blokator) · Email filter (F-M365) · Entra auth
   dizajn MCP servera (F-M365) · AKS vs VM za pilot.

---

## §25 KAKO KORISTITI OVAJ DOKUMENT KAO PROMPT

1. **Prvo odaberi build path (§23.1):** postoji li `/actions` API (ili World B
   nalog) → PATH T; skelu (PATH P) gradi SAMO uz eksplicitnu potvrdu da
   /actions još ne postoji. NIKAD ne preskači invarijante §1. Ciljno stanje
   koje isporučuješ je §0.5 — sve iz §22.5 je dug, ne feature.
2. Svaki modul dobiva test u istom koraku; suite mora biti zelena NAKON SVAKOG
   koraka (ne na kraju). `tests/v2/test_architecture.py` manifest ažuriraj uz
   svaki novi v2 modul.
3. Svaki novi izlazni put = redak u §21.3 + test.
4. Gdje vanjski ugovor nije potvrđen (§17.2, §24) — gradi tolerantno (multi-
   fallback parse, config umjesto pretpostavke, glasan log umjesto tihog dropa)
   i zapiši pretpostavku.
5. Definicija gotovog = §21.2. Samoocjena: ako IJEDNA stavka §1 ili §21 ne
   stoji, sustav NIJE gotov — nastavi dok ne stoji.
