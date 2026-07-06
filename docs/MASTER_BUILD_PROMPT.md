# MASTER BUILD PROMPT — MobilityONE Conversation Engine (ciljni sustav)

> **⛔ OVO JE TVOJ SYSTEM PROMPT, GRADITELJU (AI ili programer).** Iz OVOG JEDNOG
> dokumenta izgradi kompletan, produkcijski **Conversation Engine** za
> MobilityONE. Dokument je samostojeći: misija, invarijante, kritična znanja
> (mine!), ciljana arhitektura, struktura, svi ugovori (config / SQL shema /
> API), flowovi, kod-primjeri, testni i deploy plan.
>
> **PROTOKOL (obavezan):**
> 1. Pročitaj redom: §0 → §0.5 (CILJANA ARHITEKTURA) → §1 (invarijante) →
>    §1.5 (mine) → §1.6 (build-integrity) → §4.5 (config disciplina) — pa gradi.
> 2. Nakon SVAKOG koraka: testovi tog koraka zeleni + samoprovjera §25.
> 3. Nešto nije specificirano? Konzervativni default + ZAPIŠI odluku u
>    DECISIONS.md. Nešto proturječi? §0.5 / §1 / §1.5 pobjeđuju sve ostalo.
> 4. NIKAD ne isporuči korak koji krši ijedan redak §1, ponavlja minu §1.5, ili
>    pada na build-integrity gateu §1.6 / §4.5.
> 5. Gotovo = §21, ne tvoj osjećaj. Samoocjena <10/10 → nastavi raditi.
>
> **Verzija:** v3.4 (2026-07) — nakon adversarijalnog audita (35 agenata):
> zatvoreno 15 stvarnih + 16 djelomičnih rupa. +§28 KONVERZACIJSKA PRAVILA
> (out-of-scope, multi-intent, promjena teme, ton — K12), +§29 OPS/DATA
> LIFECYCLE (GDPR retencija+erasure za ai.*, admin surface, observability,
> outbox-heartbeat, migracije, rollback), pooštren §20.1 (mypy+ruff+benchmark
> gate+mine-manifest), §26 dopunjen (konverzacijski moduli+tooling; prune deps).
> Ranije (v3.3): +REUSE-MAPA (§26), +ENGINEERING & PROŠIRIVOST (§27),
> +LLM promptovi (§14.1), +CI (§20.1), +atomic claim (§5.1). Ciljna arhitektura po reviewu
> vlasnika: **jedan `mobilityone-ai` servis na postojećem AKS-u, SQL `ai` schema u
> postojećoj bazi, BEZ Redisa / zasebnog workera / PostgreSQL / KEDA** — sav mozak,
> mine i ugovori PREŽIVLJAVAJU, re-homani na jednostavniju infrastrukturu.
>
> **STACK (FIKSIRAN — nema pogađanja, K2):** `mobilityone-ai` je **Python 3.12 +
> FastAPI** (repo se gradi/tipizira na 3.12 — Dockerfile, ruff, mypy),
> kontejneriziran (Docker), deployan na postojeći AKS kao standardni
> container. Razlog: imamo KOMPLETAN, testiran Python sustav (1756 testova, svih
> 14 mina riješeno) — kontejner se na AKS deploya jednako kao .NET servis i dijeli
> istu bazu / ingress / Key Vault / App Insights. `ai` schema DDL može kreirati
> Boris (EF migracija) ILI mi (`db/schema.sql`) — svejedno, oblik je §5. Ako
> MobilityONE STROGO traži .NET servis, ovaj dokument je 1:1 port (isti model
> podataka, isti flow, isti mozak — samo drugi jezik); ali default je Python.

---

## §0 MISIJA

Korisnik (vozač flote) pošalje poruku prirodnim jezikom kroz bilo koji kanal
(WhatsApp, Teams, Web Chat, M365 Copilot): *"pukla mi je guma na ZG-1234-AB"*,
*"rezerviraj auto sutra 9-15"*, *"pokaži moja zadnja putovanja"*. Sustav mora:

1. **prepoznati namjeru** (1 od ~30 poslovnih akcija),
2. **pozvati TOČNO pravi MobilityONE API** (param punjenje, šifrarnici,
   ekstrakcija iz responsa — sve točno),
3. **odgovoriti na hrvatskom**, sažeto i utemeljeno SAMO na podacima iz API-ja.

**Podjela odgovornosti (pravilo koje sve oblikuje):**
```
ADAPTERI          = pretvaraju kanal-specifičan format ↔ interni model
CONVERSATION ENG. = jezik + sigurnost + JEDNA čista akcija po turnu  (mozak)
MOBILITYONE API   = poslovna pravila + podaci (Fleet backend)
```

**Načelo #1 (od vlasnikova reviewa):** ovo NIJE "WhatsApp bot". Ovo je
**Conversation Engine**; kanali su samo adapteri. Svi adapteri zovu isti
`ConversationService`. To spašava mjesece refaktoringa kad dođu Teams/Copilot/Web.

---

## §0.5 CILJANA ARHITEKTURA (NORMATIVNA)

```
   WhatsApp   Teams   Web Chat   M365 Copilot   (future channels)
       │        │         │            │
       ▼        ▼         ▼            ▼
   ┌───────────────────────────────────────────────┐
   │  mobilityone-ai  (jedan servis, postojeći AKS) │
   │  ingress: /api/ai/*                            │
   │                                                │
   │  ┌─ ADAPTERI (kanal ↔ interni model) ────────┐ │
   │  │ Infobip · Teams · Copilot · WebChat        │ │
   │  └───────────────────┬────────────────────────┘ │
   │            upiši u ai.Message → vrati 200        │
   │  ┌───────────────────▼────────────────────────┐ │
   │  │ OUTBOX PETLJA (background, isti proces)     │ │
   │  │   vadi ai.Message(status=received)          │ │
   │  └───────────────────┬────────────────────────┘ │
   │  ┌───────────────────▼────────────────────────┐ │
   │  │ ConversationService  (MOZAK)                │ │
   │  │  safety → identitet → routing(LLM) →        │ │
   │  │  validacija → params → Da/Ne → izvršenje →  │ │
   │  │  formatter(HR)                              │ │
   │  └──────┬───────────────────────┬──────────────┘ │
   └─────────┼───────────────────────┼────────────────┘
             │                       │
        Azure OpenAI          MobilityONE API
      (odluka + format)     (Bearer + x-tenant + Idempotency-Key)
             │                       │
   ┌─────────▼───────────────────────▼────────────────┐
   │  SQL SERVER (postojeća baza)                      │
   │    dbo.*          (Fleet — Boris/core tim)        │
   │    ai.*           (NAŠA schema — §5)              │
   └───────────────────────────────────────────────────┘

   Postojeći AKS dijeli:  Key Vault · Managed Identity · App Insights
                          isti ingress · isti CI/CD · isti monitoring
```

**Deployment topologija (na postojećem AKS-u):**
```
mobilityone-api        (Fleet core — postoji)
mobilityone-forms      (postoji)
identityserver         (OAuth — postoji)
mobilityone-ai   ⟵ NOVO (naš servis; Boris skele deployment, standardno)
     │
SQL Server:  dbo.*  +  ai.*   (jedna baza, dvije scheme)
Azure OpenAI · Key Vault · App Insights
```

**Zašto zaseban `mobilityone-ai` deployment a ne u postojećem API-ju:** AI kod
će vrlo brzo postati drugačiji od core Fleet logike — zaseban lifecycle, zaseban
scaling, zaseban CI. ALI dijeli sve ostalo (baza, ingress, monitoring, CI/CD).

**URL struktura:**
```
/api/ai/webhooks/infobip     ← WhatsApp/Viber inbound
/api/ai/webhooks/teams       ← Teams inbound
/api/ai/chat                 ← Web Chat (sync)
/api/ai/admin                ← admin (token-gated)
/api/ai/copilot   (kasnije)  ← M365 Copilot
/api/ai/mcp       (kasnije)  ← MCP server
```
Dodavanje kanala = novi adapter + nova ruta, BEZ promjene osnovne arhitekture.
**Verzioniranje:** klijent-okrenute rute nose verziju (`/api/ai/v1/chat`…) ILI
`Accept: application/vnd.ai.v1+json` — da budući breaking format ne razbije kanale.

**Što NAMJERNO NEMA** (nije opravdano za ovu veličinu; dodaje se tek kad ima
smisla): PostgreSQL · Redis · KEDA · zaseban worker-deployment · zaseban
admin-api servis · drugi AKS · AGIC. Trajnost/red/idempotencija koje je Redis
nekad davao **žive u SQL-u** (outbox, §5) — funkcija se seli, ne briše (§1.5 M10).

---

## §1 NEPOVREDIVI INVARIJANTI (krše li se — build je neispravan)

| # | Invarijanta | Mehanizam (v3.0) |
|---|---|---|
| 1 | **Nijedna mutacija bez eksplicitne potvrde korisnika** (Da/Ne) — LLM *bira* akciju, nikad ne *izvršava* write sam | mutation_gate; echo parametara prije slanja |
| 2 | **PII se scrubba PRIJE ijednog LLM poziva** (OIB/IBAN/telefon → [REDACTED]) i prije logova | pii_scrubber; PII filter na App Insights/logging |
| 3 | **Tenant strict-binding**: identitet iz kanala → person_id + TenantId iz MobilityONE; bez TenantId NEMA API poziva; tenant NIKAD iz env-defaulta ni teksta | identity.resolve; x-tenant header |
| 4 | **Krizni signal (suicid) → hotline poruka** (Plavi telefon 116 123), terminal, prije routinga | crisis_detector |
| 5 | **GDPR intenti** (brisanje/izvoz) → audit zapis (`ai.Feedback`/audit) + definiran postupak | special_intents + audit |
| 6 | **Korisnik UVIJEK dobije odgovor ILI poruka završi kao `ai.Message.status=failed` + alarm** — nikad tiha smrt (§21) | outbox status + App Insights alert |
| 7 | **Idempotencija**: dedup inbound (unique constraint) + `Idempotency-Key` na MobilityONE mutacije + outbound "poslano" status | §5 SQL ograničenja |
| 8 | **Redoslijed poruka istog korisnika** se čuva | outbox obrađuje po `created_at` per user + claim |
| 9 | **Prompt-injection obrana** na ulazu (input_sanitizer) i na izlazu iz API podataka (output_sanitizer) | postoji |
| 10 | **Fail-closed rubovi**: HMAC bez tajne/potpisa → 401; auth strict gate traži POZITIVNU verifikaciju (§13) | webhook adapter, auth_preflight |
| 11 | **AI ne vidi interne parametre** (person_id, tenant_id = inject; šifre = backend/resolver) — AI puni SAMO što korisnik izgovori | actions.json granica §11 |
| 12 | **Svaki novi izlazni put iz koda = novi redak u §21 + test** | CI pravilo |

---

## §1.5 KRITIČNA ZNANJA — MINE (simptom → uzrok → fix)

**Svaka je STVARNO eksplodirala ili bi eksplodirala. Ponoviš li ijednu, build
NIJE nepogrešiv. (Naslijeđeno iz žive implementacije; M10/M14 re-homani s Redisa
na SQL.)**

| # | Mina | Simptom | Uzrok | OBAVEZNI fix |
|---|---|---|---|---|
| M1 | **Error-code klasifikacija** | trajne greške (mrtav token, 400/422) se retryaju umjesto odustaju; 429 ignorira Retry-After | usporedba `error_code` s KRATKIM literalom umjesto pravom `ErrorCode` VRIJEDNOSTI (`"VALIDATION_PHONE_INVALID"`) | permanent skup sadrži `ErrorCode.*.value`; rate-limit grana prima pravu vrijednost |
| M2 | **Adapter recipient hook** | isporučena poruka se šalje PONOVNO (duplikati) | success-log/parse čita polje kojeg kanal-specifičan payload nema (Viber `messages[]` nema top-level `to`) → KeyError NAKON slanja, progutan | recipient/parse je HOOK po adapteru, ne pretpostavka jednog oblika |
| M3 | **Auth preflight `verified`** | mrtvi kredencijali PROĐU startup | dead creds daju status 0/transport-error, NE 401 | strict gate traži POZITIVAN dokaz: token dohvaćen + bar jedan pravi HTTP odgovor (≥200) — §13 |
| M4 | **Per-channel duljina** | odgovor stigne TIHO ODREZAN | jedan globalni prag > kanalov limit | limit po adapteru (WhatsApp 4096, Viber 1000, Teams/Web drugo) |
| M5 | **GDPR svježe čitanje** | stale stanje AUTORIZIRA brisanje | GDPR put čitao keširano/staro | GDPR resolve/binding čita SVJEŽE iz izvora, ne iz session cachea |
| M6 | **Tenant strict-binding** | korisnik vidi TUĐE podatke | fallback na env-default tenant kad izvor ne vrati TenantId | bez TenantId → korisnik ODBIJEN; NIKAD env/tekst (INV-3) |
| M7 | **Envelope guessing** | "imaš N stavki" krivo/prazno | MobilityONE list-envelope nije uniforman (pogađa `Data/Result/Items/value`…) i ne zna gdje je `total` | dok backend ne potvrdi ugovor (§17): envelope-aware parse + NE tvrdi broj koji ne znaš |
| M8 | **Naive datetime TZ** | rezervacija ±1-2h (tiha korupcija) | naive ISO bez offseta, backend TZ nepoznat; parser računa u Europe/Zagreb a test/kod negdje u UTC | ugovoriti TZ (§17); interno UVIJEK Europe/Zagreb i zapisati; testovi u istoj zoni kao parser |
| M9 | **Idempotency-Key** | timeout NAKON upisa + retry = DUPLA mutacija | backend ne deduplicira po headeru | `Idempotency-Key` (UUID stabilan kroz retry) na SVAKU mutaciju; tražiti backend dedup ≥10min (§17) |
| M10 | **Outbox "gotovo tek nakon slanja"** (bivši ACK-nakon-enqueue) | pod restart usred obrade = odgovor izgubljen | poruka označena `answered` PRIJE nego je odgovor stvarno poslan | `ai.Message.status`: `processing`→`answered` TEK nakon uspješnog outbound; na startu re-obradi zaglavljene `processing` |
| M11 | **PII prije LLM-a** | OIB/IBAN procuri u Azure/logove | scrub POSLIJE LLM-a ili samo na izlazu | `pii_scrubber` PRIJE ijednog LLM poziva (INV-2) |
| M12 | **actions.json boot** | tiho krivo s pola-učitanim katalogom | malformiran katalog toleriran | fail-fast: malformiran/prazan → servis se NE digne (§11) |
| M13 | **filter/useandfor suppress** | LLM izmišlja `Filter` → 422 | tehnički query-param izložen LLM-u | suppress iz LLM sheme (§9) |
| M14 | **Dedup PRIJE obrade** (bivši non-text-nakon-locka) | Infobip retry → dupli odgovor | dedup se radio prekasno | unique constraint na `ai.Message(channel, provider_message_id)` — duplikat odbijen na upisu, prije obrade |

> **Pravilo:** prije nego zatvoriš dio koji dira slanje, auth, tenant, PII,
> datetime, katalog ili outbox — pročitaj pripadnu minu i dodaj njen test.

---

## §1.6 BUILD-INTEGRITY PRAVILA — protiv NEPOTPUNOG / VIŠKA / SLOMLJENOG builda

Svako ima GATE (mehanizam koji hvata kršenje). Kršenje IJEDNOG = build NIJE gotov.

**A) ANTI-NEPOTPUN**
- BI-1: Sposobnost je "gotova" SAMO uz END-TO-END dokaz (poruka uđe → točan odgovor izađe), NIKAD "modul postoji". Gate: e2e test po sposobnosti.
- BI-2: Svaki dio stanja koji se ZAPIŠE ima i ČITAČA (pending params/mutation/clarify u `ai.UserSession`). Gate: grep/test da svaki write ima read.
- BI-3: Svaka akcija u `actions.json` ima: izvršni put + `inject` + (ako mutation) confirm. Gate: test da executor rutira bez greške; mutation bez confirma = fail.
- BI-4: Svaki podatak koji sloj PROIZVEDE ima POTROŠAČA (channel→adapter, tenant→x-tenant, `total`→formatter). Gate: šav-test.
- BI-5: Svaki modul napisan da se pozove NA STARTU MORA biti pozvan (auth_preflight; outbox loop se STARTA). Gate: startup test.

**B) ANTI-VIŠAK**
- BI-6: Svaki config field ima čitača IZVAN config-a, preko `settings`. Gate: dead-config test.
- BI-7: Svaki modul ima test; svaki test ima modul. Gate: enforced manifest test.
- BI-8: Svaka `ai.*` tablica ima ŽIVOG pisca. Gate: grep-test model→writer.
- BI-9: NIJEDAN artefakt iz "izbačeno" liste (Redis klijent, zaseban worker, k8s Redis/Postgres manifesti, KEDA). Gate: grep da ne postoje.

**C) ANTI-SLOMLJEN-FLOW**
- BI-10: Svaki `ai.Message`/`ai.UserSession` zapis ima definiran skup polja (§5) + čitač s DEFAULTOM. Gate: kontraktni test.
- BI-11: `ErrorCode` se klasificira po VRIJEDNOSTI ne literalu (M1). Gate: test s pravim vrijednostima.
- BI-12: Adapter je JEDINO mjesto koje zna kanal-specifičan format; ConversationService je channel-agnostičan. Gate: grep da `engine/` ne importa NI adapter-SDK (Infobip/Bot Framework) NI `adapters/` sam (`from adapters`, `import infobip`) — oboje = crven build.
- BI-13: Svaki ŠAV ima test: adapter→ai.Message · outbox→engine · engine→MobilityONE(auth) · engine→adapter(send). Gate: e2e/kontraktni test.
- BI-14: Nijedan izlaz ne "propušta šutke" — vodi u odgovor ILI `status=failed`+alarm, i ima redak u §21. Gate: garancija-odgovora tablica raste sa svakim izlazom.

---

## §2 CONVERSATIONSERVICE + ADAPTERI

**Ključni dizajn: mozak ne zna koji je kanal.** Adapter pretvara kanal-specifičan
inbound u interni `InboundMessage`, i interni `OutboundReply` natrag u
kanal-specifičan format.

```python
# interni model — jedini jezik koji ConversationService razumije
@dataclass
class InboundMessage:
    channel: str            # "whatsapp" | "viber" | "teams" | "web" | "copilot"
    sender: str             # kanal-specifičan ID (telefon / AAD id / session id)
    text: str
    provider_message_id: str
    raw: dict               # original payload (za debug/audit)

@dataclass
class OutboundReply:
    channel: str
    recipient: str
    text: str
    idempotency_key: str

class ChannelAdapter(Protocol):
    def parse_inbound(self, raw: dict) -> list[InboundMessage]: ...
    async def send(self, reply: OutboundReply) -> SendResult: ...
    def verify_signature(self, body: bytes, headers: dict) -> bool: ...
    MAX_LEN: int            # kanalov limit (M4)

class ConversationService:            # MOZAK — channel-agnostičan (§8)
    async def process(self, msg: InboundMessage) -> OutboundReply | None: ...
```

Adapteri (`adapters/infobip.py`, `teams.py`, `webchat.py`, `copilot.py`) su
JEDINO mjesto s kanal-specifičnim znanjem. Dodavanje kanala = novi adapter koji
implementira `ChannelAdapter`, i nova ruta `/api/ai/webhooks/<kanal>`.

---

## §3 STRUKTURA REPOA (jedan servis)

```
mobilityone-ai/
├── config.py                       # pydantic Settings (env §4; secreti iz Key Vault)
├── main.py                         # FastAPI app: /api/ai/* rute + startup (outbox loop, preflight)
├── db/
│   ├── schema.sql                  # ai.* DDL (§5); Boris smije kreirati i EF-om — DDL je isti
│   └── repository.py               # aioodbc/SQLAlchemy pristup; claim_next_inbound (§5.1) atomičan
├── adapters/
│   ├── base.py                     # ChannelAdapter Protocol + InboundMessage/OutboundReply
│   ├── infobip.py                  # WhatsApp + Viber (HMAC, parse, send; M2/M4)
│   ├── teams.py · webchat.py · copilot.py
├── outbox/
│   └── loop.py                     # background: vadi ai.Message(received) → process → send (§7)
├── engine/                         # MOZAK (ConversationService) — channel-agnostičan
│   ├── conversation_service.py     # orkestrira slojeve (§8)
│   ├── safety/  rate_limiter.py · pii_scrubber.py · input_sanitizer.py · output_sanitizer.py · crisis_detector.py
│   ├── identity.py · special_intents.py
│   ├── routing/ llm_router.py · action_registry.py · action_validator.py
│   ├── params/  param_ui.py · type_resolver.py     (stanje u ai.UserSession)
│   ├── mutation_gate.py
│   ├── executor.py                 # poziv MobilityONE API-ja (§12)
│   ├── formatter.py                # JSON → hrvatski (§14)
│   └── api_error_translator.py
├── mobilityone/
│   ├── api_gateway.py              # HTTP + auth + x-tenant + Idempotency-Key + circuit breaker
│   ├── token_manager.py            # OAuth (Managed Identity ILI client_credentials)
│   └── auth_preflight.py           # scope introspection + route probe (§13)
├── config/actions.json             # ~30 akcija (§11)
└── tests/                          # po modulu + e2e razgovori + contract fixturei
```

---

## §4 KONFIGURACIJA (env; secreti iz Key Vaulta)

| Var | Obavezno | Značenje |
|---|---|---|
| `SQL_CONNECTION_STRING` | ✅ | postojeća baza (schema `ai`); iz Key Vaulta |
| `MOBILITYONE_API_URL` | ✅ | Fleet API host (`dev-k1…io`) |
| `MOBILITYONE_AUTH_URL` / `CLIENT_ID` / `CLIENT_SECRET` | ✅¹ | OAuth (¹ ili Managed Identity — §12) |
| `AZURE_OPENAI_ENDPOINT` / `_API_KEY`² | ✅ | LLM (² preferirano Managed Identity umjesto keya) |
| `AZURE_OPENAI_DEPLOYMENT_NAME` | ✅ | ime chat deploymenta (PIN verziju) |
| `INFOBIP_BASE_URL` / `_API_KEY` / `_SECRET_KEY` | WA/Viber | Infobip account + HMAC tajna |
| `INFOBIP_SENDER_NUMBER` | WA | WhatsApp sender broj |
| `VIBER_SENDER` | Viber | registrirano IME sendera (ne broj); neset = Viber off |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | ✅ prod | App Insights (telemetrija + alarmi) |
| `AUTH_PREFLIGHT` / `_STRICT` | default log-only | §13 |
| `MOBILITY_REQUIRED_SCOPES` | opc. | scopeovi koje token MORA nositi |
| `ADMIN_TOKEN_1..N` + `_USER` | admin | admin rute (jedini gate) |
| `APP_ENV` | ✅ | `production` aktivira validatore (npr. HMAC obavezan) |

**Sve tajne iz Key Vaulta preko Managed Identity — NIKAD u kodu ni gitu (§4.5).**

### 4.5 CONFIG DISCIPLINA (no-hardcoding — precizno, NIJE "sve u env")
| Vrsta | Ide u | Nikad |
|---|---|---|
| Tajne (keys, secreti, connection string, HMAC salt) | **Key Vault / env** | kod, git, placeholder-only u `.env.example` |
| Hostovi / URL / per-OKOLINA vrijednosti | **env** (`settings`) | hardkodirano |
| Ponašanje / algoritamske konstante (timeouti, pragovi) | **kod** | env (osim ako treba per-deploy tuning) |
| Nova config var | env **samo uz živog čitača** | "za svaki slučaj" (mrtav knob) |

Gate: secret-scan u CI; grep-lint na hardkodirane hostove/URL-ove; dead-config
test; svaki env potrošač preko `settings` (ne raštrkani `os.environ`).

---

## §5 SQL `ai` SCHEMA + OUTBOX (srce trajnosti — zamjena za Redis)

**Prednost sheme u postojećoj bazi:** postojeći backupi, maintenance, monitoring,
zero DBA posla.

```sql
-- ai.Message = DURABLE INBOX+OUTBOX (zamjena za Redis stream + outbound queue)
CREATE TABLE ai.Message (
    Id                  BIGINT IDENTITY PRIMARY KEY,
    ConversationId      BIGINT NULL REFERENCES ai.Conversation(Id),
    Channel             NVARCHAR(20)  NOT NULL,     -- whatsapp/viber/teams/web
    Direction           NVARCHAR(10)  NOT NULL,     -- inbound / outbound
    ProviderMessageId   NVARCHAR(128) NULL,
    Sender              NVARCHAR(128) NULL,
    Recipient           NVARCHAR(128) NULL,
    Text                NVARCHAR(MAX) NULL,
    Status              NVARCHAR(20)  NOT NULL,      -- received→processing→answered / failed
    Error               NVARCHAR(MAX) NULL,
    IdempotencyKey      NVARCHAR(128) NULL,
    TenantId            NVARCHAR(64)  NULL,
    CreatedAt           DATETIME2     NOT NULL DEFAULT SYSUTCDATETIME(),
    ProcessedAt         DATETIME2     NULL,
    CONSTRAINT UQ_inbound_dedup UNIQUE (Channel, ProviderMessageId)  -- M14 dedup
);
CREATE INDEX IX_Message_pending ON ai.Message(Status, CreatedAt) WHERE Direction='inbound';

CREATE TABLE ai.Conversation ( Id BIGINT IDENTITY PK, Channel NVARCHAR(20),
    Sender NVARCHAR(128), TenantId NVARCHAR(64), StartedAt DATETIME2, LastActivityAt DATETIME2 );

-- ai.UserSession = STANJE RAZGOVORA (zamjena za Redis pending_* + conv_history)
CREATE TABLE ai.UserSession (
    Sender      NVARCHAR(128) PRIMARY KEY,
    TenantId    NVARCHAR(64),
    State       NVARCHAR(MAX),      -- JSON: {pending_params|pending_mutation|pending_clarify, history[-5:]}
    ExpiresAt   DATETIME2,          -- lazy cleanup (zamjena za Redis TTL)
    UpdatedAt   DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
);

CREATE TABLE ai.ToolCallLog (    -- telemetrija + audit (zamjena za routing:accuracy_log)
    Id BIGINT IDENTITY PK, Sender NVARCHAR(128), TenantId NVARCHAR(64),
    Action NVARCHAR(64), Params NVARCHAR(MAX), Result NVARCHAR(20),
    LatencyMs INT, CreatedAt DATETIME2 DEFAULT SYSUTCDATETIME() );

CREATE TABLE ai.Feedback (       -- "nije točno" korekcije (bivši hallucination_reports, sad ŽIV)
    Id BIGINT IDENTITY PK, MessageId BIGINT, WrongAction NVARCHAR(64),
    CorrectAction NVARCHAR(64), Note NVARCHAR(MAX), CreatedAt DATETIME2 DEFAULT SYSUTCDATETIME() );

CREATE TABLE ai.Channel (        -- registar kanala (sender→config)
    Channel NVARCHAR(20) PRIMARY KEY, Enabled BIT, Config NVARCHAR(MAX) );
```

### Mapiranje: što je Redis radio → gdje ide sad
| Redis | v3.0 (SQL / in-memory) |
|---|---|
| stream (red) | `ai.Message.Status` (received→processing→answered) |
| dedup `wh_dedup` | `UQ_inbound_dedup` unique constraint (M14) |
| idempotencija slanja `sent:` | `ai.Message` outbound + `IdempotencyKey` unique |
| pending_* + conv_history | `ai.UserSession.State` (JSON) + `ExpiresAt` |
| redoslijed po korisniku | outbox: obradi po `CreatedAt` per Sender + claim |
| rate-limit / circuit-breaker | **in-memory** (ok za 1 pod; skalira li se → per-pod) |
| telemetrija | `ai.ToolCallLog` |
| DLQ + garancija | `Status=failed` + `Error` + App Insights alarm |

**Čišćenje isteklih sesija** (zamjena za Redis auto-TTL): lazy (`WHERE ExpiresAt >
NOW()` na čitanju) + periodični `DELETE` job (scheduled task ili App Insights-triggered).

### 5.1 Atomičan claim (najosjetljiviji dio — mora biti točan, K3)
Outbox vadi SLJEDEĆU poruku za obradu tako da JE dva poda/taska ne uzmu istu.
SQL Server atomičan claim (jedan round-trip, bez race-a):
```sql
-- claim_next_inbound(): uzmi 1 najstariju 'received' i odmah je zaključaj
UPDATE TOP (1) ai.Message WITH (READPAST, ROWLOCK, UPDLOCK)
   SET Status = 'processing', ProcessedAt = SYSUTCDATETIME()
OUTPUT inserted.Id, inserted.Channel, inserted.Sender, inserted.Text,
       inserted.ProviderMessageId, inserted.TenantId
 WHERE Id = (SELECT TOP (1) Id FROM ai.Message WITH (READPAST, ROWLOCK, UPDLOCK)
             WHERE Direction = 'inbound' AND Status = 'received'
             ORDER BY CreatedAt);   -- ORDER BY = redoslijed (INV-8)
```
`READPAST` preskače retke koje drugi task već drži → dva taska nikad ne obrade
istu poruku, bez eksplicitnog locka. **Redoslijed po korisniku:** za striktni
per-Sender redoslijed dodaj `AND NOT EXISTS (SELECT 1 FROM ai.Message m2 WHERE
m2.Sender = ai.Message.Sender AND m2.Status='processing')` (ne uzimaj drugu
poruku istog korisnika dok prva nije gotova).

**Pretpostavka (K3, eksplicitno):** ciljano **1 pod** za ~120 vozača — in-memory
rate-limit/circuit-breaker su per-proces, redoslijed trivijalan. **Na 2+ poda:**
atomičan claim gore i dalje radi (SQL je izvor istine); mijenja se samo: (a)
rate-limit postaje per-pod (2×, sitnica), (b) per-Sender redoslijed oslanja se
na `NOT EXISTS` guard gore umjesto na single-pod slijednost. Ništa se ne lomi,
samo se ta dva mjesta uključe.

**Testiranje (pošteno):** `READPAST`/`UPDATE TOP…OUTPUT` su SQL-Server-specifični
— NE vrte se na SQLite. Zato: `repository.py` unit-testovi mockaju claim
(testira se logika petlje/statusa), a SAM atomični claim + concurrency
(dva taska ne uzmu istu poruku) testira se **integration testom protiv LocalDB/
SQL Servera** (`-m integration`, gated, ne u svakom PR-u). Bez ovog integration
testa, concurrency claima NIJE dokazan (BI-13 šav-test).

---

## §6 ULAZNI RUB — webhook adapteri

```python
# main.py — jedna ruta po kanalu, isti obrazac
@app.post("/api/ai/webhooks/infobip")
async def infobip_webhook(request: Request):
    raw = await request.body()
    if not adapters["infobip"].verify_signature(raw, request.headers):   # HMAC, fail-closed (INV-10)
        raise HTTPException(401)
    for msg in adapters["infobip"].parse_inbound(json.loads(raw)):        # → InboundMessage[]
        await repo.insert_inbound(msg)   # UPSERT s UQ dedup (M14); duplikat = no-op
    return {"status": "queued"}          # brz 200 — obrada ide async (§7)
```
HMAC, dedup, i parse žive u adapteru. Infobip očekuje brz `200`; odgovor se šalje
ZASEBNO (§7) — zato je async decoupling OBAVEZAN čak i u jednom servisu.

---

## §7 OUTBOX PETLJA (async obrada bez Redisa/workera)

```python
# outbox/loop.py — background task startan iz main.py lifespan
async def outbox_loop(repo, engine, adapters, shutdown):
    while not shutdown.is_set():
        msg = await repo.claim_next_inbound()          # UPDATE TOP(1) ... SET Status='processing'
        if msg is None:                                #   ORDER BY CreatedAt  (redoslijed, INV-8)
            await asyncio.sleep(0.5); continue          #   OUTPUT claimed row  (atomičan claim)
        try:
            reply = await engine.process(msg)           # MOZAK (§8), budžet 90s
            if reply:
                res = await adapters[reply.channel].send(reply)   # tek SAD šalji
                await repo.mark_answered(msg, reply, res)         # M10: answered TEK nakon slanja
            else:
                await repo.mark_answered(msg, None, None)
        except Exception as e:
            await repo.mark_failed(msg, e)              # Status=failed + Error → App Insights alarm
            log_exception("engine_error", e)
# STARTUP RECOVERY: na bootu resetiraj Status='processing'→'received' (zaglavljeni od restarta)
```
- **Trajnost:** poruka je u `ai.Message` prije `200` — restart je ne gubi.
- **Idempotencija:** `answered` tek nakon uspješnog slanja (M10); dedup na upisu (M14).
- **Redoslijed:** claim po `CreatedAt` per Sender.
- **Garancija odgovora:** svaka poruka završi `answered` ILI `failed`+alarm (INV-6, §21).

### 7.1 Outbound retry/backoff politika (K7 — eksplicitno)
Slanje odgovora preko adaptera (Infobip/Teams…) može pasti tranzijentno. Politika
(mapirano na `ai.Message` outbound retku, `Attempt` stupac):
```
adapter.send() vrati SendResult(error_code po ErrorCode VRIJEDNOSTI — M1!):
  PERMANENTNA greška (VALIDATION_PHONE_INVALID, FORBIDDEN, BAD_REQUEST…)
     → Status=failed odmah + alarm (retry nema smisla)
  RATE_LIMITED (GATEWAY_RATE_LIMITED)
     → zakazano ponovno slanje za `Retry-After` sekundi (ScheduledAt stupac)
  TRANZIJENTNA (timeout, 5xx, conn reset)
     → backoff 5·2^attempt s (5/10/20), do MAX_ATTEMPTS=3, pa Status=failed+alarm
Zakazana slanja: outbox petlja uzima i outbound retke gdje ScheduledAt <= now.
```
Klasifikacija greške ide po `ErrorCode.*.value`, NIKAD po kratkom literalu (M1).

---

## §8 CONVERSATIONSERVICE — master tok (MOZAK; channel-agnostičan)

```
engine.process(InboundMessage) — budžet 90s (timeout → HR poruka + status)
━ SAFETY ━ rate_limiter (in-mem) · pii_scrubber (PRIJE LLM-a!) · input_sanitizer
           · crisis_detector (suicid→hotline, terminal)
━ IDENTITET ━ identity.resolve(channel, sender) → person_id + TenantId
           (STRICT: bez TenantId → enrollment poruka, terminal; §16)
           · special_intents (GDPR/welcome/handover, terminal)
━ STANJE ━ pending nastavci iz ai.UserSession (params / Da-Ne / izbor 1-2-3)
━ ODLUKA ━ llm_router: 30 akcija direktno → {action | clarify | answer}
           (Azure retry ×3; total fail → siguran HR fallback)
━ PARAMS ━ action_validator (anti-halucinacija) · coercion (HR datumi→ISO, Europe/Zagreb)
           · fali required → pitaj + zapamti u ai.UserSession · codebook → backend/type_resolver
           · inject person_id/tenant_id (AI ih NE generira)
━ WRITE ━ policy.mutation → echo parametara + "Potvrđuješ? (Da/Ne)" → ai.UserSession
           · "Da" → exec (anti-replay: claim/status) · "Ne" → clear
━ EXEC ━ executor → MobilityONE API (Bearer + x-tenant + Idempotency-Key; §12)
           2xx→dalje · 4xx→api_error_translator→HR · 5xx/timeout→generička HR, pending ostaje
━ IZLAZ ━ formatter → HR (grounding, izlazni PII scrub, točan `total`)
           · append u ai.UserSession.history (zadnjih 5) · ai.ToolCallLog zapis
           · return OutboundReply → outbox šalje preko adaptera
```

---

## §9 ROUTING — ~30 akcija direktno u LLM
```
tekst + history[-3:] + identity sažetak
→ gpt-4o-mini tool-call nad SVIH ~30 shema iz actions.json (stanu u prompt)
→ { kind: action | clarify | answer, action?, params?, text? }
→ nesigurno → TOP-3 clarify (1/2/3; "nije točno" → reoffer + ai.Feedback zapis)
```
Točnost nosi KVALITETA OPISA u actions.json (description + use_when + examples),
ne retrieval mašinerija. `action_validator` = anti-halucinacija (nepoznata
akcija/polje/tip → clarify). `filter`/`useandfor` suppressani iz sheme (M13).

---

## §10 PARAM LIFECYCLE (8 stanica)
```
① LLM EKSTRAKCIJA (opisi+examples kritični) → ② VALIDACIJA (akcija/polja/tip→clarify)
→ ③ COERCION ("sutra 9h"→ISO, Europe/Zagreb; M8) → ④ MISSING-REQUIRED LOOP
   (pitaj → zapamti u ai.UserSession → sljedeće polje; "odustani"→abort)
→ ⑤ CODEBOOK (backend mapira semantiku; type_resolver fallback)
→ ⑥ IDENTITY INJECT (person/tenant — AI ne puni) → ⑦ ECHO u confirm poruci
→ ⑧ SLANJE + backend validacija (strukturiran 4xx → HR)
```
Nijedna stanica ne "propušta šutke"; ništa se ne šalje bez ⑦ (echo + Da).

---

## §11 ACTIONS.JSON — glavni ugovor (jedino što AI "vidi")
```jsonc
{ "name": "report_incident",
  "ai": { "description": "Prijava kvara/štete/nezgode na vozilu.",
    "use_when": ["pukla mi je guma","auto ne pali","imao sam nezgodu"],
    "parameters": {                          // SAMO što korisnik izgovori
      "registration_plate": {"type":"string","required":true,
        "description":"Registracija vozila.","examples":["ZG-1234-AB"]},
      "description": {"type":"string","required":true,
        "description":"Opis kvara korisnikovim riječima.","examples":["pukla guma"]},
      "incident_type": {"type":"string","required":false,"codebook":"CaseType",
        "description":"Vrsta; ako korisnik ne kaže, backend default.","examples":["kvar","šteta"]}
    }},
  "execution": { "method":"POST", "action":"/actions/report-incident" },  // ili granularna ruta
  "policy": { "mutation":true, "inject":["person_id","tenant_id"] } }
```
```jsonc
// WRITE s periodom
{ "name":"book_vehicle",
  "ai":{ "description":"Rezervacija vozila za period.",
    "use_when":["rezerviraj auto","trebam vozilo sutra","bookiraj kombi"],
    "parameters":{
      "date_from":{"type":"datetime","required":true,"description":"Početak.","examples":["sutra 9h"]},
      "date_to":{"type":"datetime","required":true,"description":"Kraj.","examples":["do 15h"]},
      "vehicle_hint":{"type":"string","required":false,"description":"Konkretno vozilo; prazno=backend nudi.","examples":["kombi"]}}},
  "execution":{"method":"POST","action":"/actions/book-vehicle"},
  "policy":{"mutation":true,"inject":["person_id","tenant_id"]} }
// READ (bez confirma — nije mutation)
{ "name":"list_trips",
  "ai":{ "description":"Prikaz korisnikovih putovanja.",
    "use_when":["moja putovanja","zadnje vožnje","gdje sam bio"],
    "parameters":{ "limit":{"type":"integer","required":false,"description":"Koliko zadnjih.","examples":["10"]}}},
  "execution":{"method":"GET","action":"/actions/list-trips"},
  "policy":{"mutation":false,"inject":["person_id","tenant_id"]} }
```
Boot fail-fast (malformiran → servis se NE digne, M12). Reload bez deploya
(`/api/ai/admin` cache-invalidate). Novi param uvijek `required:false` (aditivno).

> **FAZA 1 (dogovoreno) = TOČNO 2 akcije:** `book_vehicle` (demo za srijedu) i
> `report_incident` (prijava štete). KONKRETNI M1 pozivi (auth, Persons,
> MasterData, AvailableVehicles, VehicleCalendar) + payload booking-a su u
> **`docs/M1_API_PLAYBOOK.md`**. Ostalih ~28 akcija je BUDUĆNOST (§27 ih pušta
> bez dirania jezgre). Za Fazu 1: booking ide dvokorak u botu
> (`GET AvailableVehicles` → `POST VehicleCalendar`) dok backend ne izloži jednu
> `/actions/book-vehicle`.

**Tri vrste parametara:** korisnik izgovori → `ai.parameters`; iz identiteta →
`policy.inject`; interni kod/default → backend (AI ne dira).

---

## §12 EXECUTOR + MOBILITYONE API (auth)
- **Async** poziv preko `api_gateway.call`; NIKAD sync.
- Auth: **Managed Identity** (preferirano na AKS-u) ILI OAuth `client_credentials`
  (`token_manager`, cache). Header: `Authorization: Bearer` (401→refresh),
  `x-tenant: {TenantId}`, `Idempotency-Key` (UUID) na SVAKU mutaciju (M9).
- Circuit breaker po servisu; budžet 15s. List-serializacija query paramova
  (`Filter` joina `" and "`). 4xx→`api_error_translator` (HR); 5xx→generička+pending ostaje.
- **Auth = jedini razlog da uopće imamo token** — služi isključivo za CRUD pozive
  prema MobilityONE-u.

---

## §13 AUTH PREFLIGHT (garancija — startup, ne produkcijski 403)
- Sloj 1 introspection: dekodiraj VLASTITI JWT → `granted_scopes` vs `MOBILITY_REQUIRED_SCOPES`.
- Sloj 2 probe: benign GET (`/Persons?Rows=1`) → 401/403 = auth problem.
- `verified` = token dohvaćen BEZ greške I bar jedan pravi HTTP odgovor (≥200);
  mrtvi kredencijali daju status 0, NE 401 (M3). Strict (`AUTH_PREFLIGHT_STRICT=1`):
  odbij start ako `not ok OR not verified`. Pokreće se na startupu servisa.

---

## §14 FORMATTER (JSON → hrvatski)
Grounding (samo podaci iz JSON-a) · izlazni PII scrub · liste s točnim `total`
("imaš 27, prikazujem 10") · envelope-aware (M7) · odgovor <500 tokena · HR datumi.

## §14.1 LLM PROMPT-TEMPLATE-I (srž — konkretni system promptovi, K1)

> Ovo su STVARNI promptovi koje graditelj koristi. `temperature=0` svugdje.
> Tool-i za router se generiraju iz `actions.json` (§11); ne piše ih se ručno.

**ROUTER (odluka) — system prompt + tool-call:**
```
SYSTEM:
Ti si router flotnog asistenta. Korisnik piše na hrvatskom. Tvoj JEDINI zadatak:
odabrati TOČNO JEDNU akciju iz ponuđenih alata i izvući SAMO parametre koje je
korisnik EKSPLICITNO izgovorio.
PRAVILA:
- Ako korisnik jasno traži akciju → pozovi taj alat s izvučenim parametrima.
- Ako je dvosmisleno (2+ akcije moguće) → NE pogađaj; vrati kind="clarify" s
  kratkim pitanjem koja opcija.
- Ako je pitanje iz dokumenata/pravilnika (ne akcija) → kind="answer".
- NIKAD ne izmišljaj parametre koje korisnik nije rekao. Prazno polje je OK —
  sustav će pitati.
- NIKAD ne postavljaj interne ID-eve, tenant, ni šifre — to nije tvoj posao.
- Datume ostavi kako je korisnik rekao ("sutra 9h") — sustav ih pretvara.
- Ako poruka NIJE ni akcija ni pitanje o flotnim podacima/pravilniku (npr.
  "koliko je sati", vic, vrijeme, "tko si ti", opće ćaskanje) → kind="out_of_scope";
  NE izmišljaj akciju, NE forsiraj najbliži tool.
- Ako korisnik u ISTOJ poruci JASNO traži DVIJE različite akcije ("rezerviraj auto
  I prijavi kvar") → to NIJE clarify. Obradi jednu kao PRIMARY, vrati
  pending_followup="{sažetak drugog intenta}". Ako je jedan sigurnosni
  (report_incident/kvar/nezgoda) → on je PRIMARY bez obzira na redoslijed; inače
  primary = prvi spomenut.
KONTEKST: {identity_sažetak}   POVIJEST (zadnja 3 turna): {history}
Alati: {tools iz actions.json — action_registry.openai_tools()}
tool_choice = "auto"   (dopušta clarify/answer/out_of_scope put)
```
Izlazi rutera: `action | clarify | answer | out_of_scope` (+ `pending_followup`).
Ponašanje po njima je u §28 (KONVERZACIJSKA PRAVILA).

**FORMATTER (odgovor) — system prompt:**
```
SYSTEM:
Pretvori PRILOŽENI JSON u kratak, prirodan hrvatski odgovor vozaču.
STROGO:
- Koristi ISKLJUČIVO podatke iz JSON-a. Ne izmišljaj brojeve, imena, statuse.
- Ako je lista skraćena, reci točan ukupan broj: "imaš {total}, prikazujem {n}".
- Bez tehničkog žargona, bez ID-eva osim ako su korisniku korisni (npr. broj prijave).
- Kratko (2-4 rečenice). Datumi u hrvatskom formatu (12.06.2026. 09:00).
- Ako JSON nosi grešku, objasni ŠTO i KAKO dalje, ljudski.
KORISNIKOV UPIT: {original_text}
JSON: {api_response}
```

**CONFIRM ECHO (mutation gate — nije LLM, deterministički):**
```
"{glagol akcije} — provjeri prije slanja:
 • {param_label}: {vrijednost}  (za svaki popunjeni param, _render_param_echo)
Potvrđuješ? (Da/Ne)"
```

**CLARIFY (top-3) — deterministički iz kandidata:**
```
"Nisam siguran što točno želiš. Jesi li mislio:
 1️⃣ {opis akcije 1}   2️⃣ {opis akcije 2}   3️⃣ {opis akcije 3}
Odgovori brojem, ili napiši drugačije."
```
Odgovori "1/2/3" → pending_clarify u `ai.UserSession`; "nije točno" → reoffer + `ai.Feedback`.

---

## §15 KANALI (adapteri — svaki implementira `ChannelAdapter`, §2)

| Kanal | Ruta | verify_signature | identitet (sender) | send | MAX_LEN |
|---|---|---|---|---|---|
| WhatsApp | `/api/ai/webhooks/infobip` | HMAC `X-Hub-Signature-256` (INFOBIP_SECRET_KEY) | telefon | Infobip `/whatsapp/1/message/text`, `from`=INFOBIP_SENDER_NUMBER | 4096 |
| Viber | isto (integrationType razlikuje) | isti HMAC | telefon | Infobip `/viber/2/messages`, `sender`=VIBER_SENDER (IME); `messages[]` (M2) | 1000 |
| Teams | `/api/ai/webhooks/teams` | Bot Framework JWT (Azure Bot auth) | AAD objectId/UPN → email | Bot Framework `activity.reply` | ~28k |
| Web Chat | `/api/ai/chat` (SYNC — §15.1) | session token / cookie | web session id | u HTTP response (nema outbox) | n/a |
| M365 Copilot | `/api/ai/mcp` (kasnije) | Entra ID token na MCP endpointu | user email | Copilot renderira (mi vraćamo alat rezultat) | n/a |

- **Infobip (WA+Viber):** jedan adapter, kanal iz `integrationType`. Split po `MAX_LEN` (M4); recipient/payload hook po kanalu (M2).
- **Teams:** identitet preko AAD → email → `/Persons?Filter=Email`. Za start može biti stub adapter (contract fiksan, implementacija kad Teams dođe na red).
- **Copilot:** MCP server izlaže ISTE akcije iz `actions.json`; Copilot je mozak, mi dajemo alate; write akcije nose annotation `requiresConfirmation` → Copilot UI renderira potvrdu (naš Da/Ne gate je za chat kanale).

### 15.1 Web Chat — SYNC dual-path (K5, mora biti eksplicitno)
Web nema Infobip-ov "brz 200" zahtjev pa NE ide kroz outbox — ali dijeli **isti
mozak**:
```python
@app.post("/api/ai/chat")                        # SYNC — request/response
async def chat(req: ChatRequest):
    msg = InboundMessage(channel="web", sender=req.session_id, text=req.text, ...)
    reply = await engine.process(msg)             # ISTI ConversationService (§8)
    # (opcionalno zapiši u ai.Message radi povijesti/audita, ali NE preko outboxa)
    return { "text": reply.text if reply else "" }
```
**Ključno (BI-13):** async (webhook→outbox→engine) i sync (chat→engine) dijele
`engine.process()` — mozak se NE duplicira. Razlika je SAMO tko zove i tko šalje
odgovor: kod webhooka outbox+adapter, kod chata direktan HTTP response. Pending
stanje (params/mutation) radi jednako jer je u `ai.UserSession` (ne u memoriji).

---

## §16 TENANT (kako se STVARNO dobiva — verificirano u kodu)
```
POZNAT sender → ai.UserSession/ai.Conversation ima TenantId (cache prethodnog lookupa)
NEPOZNAT     → GET /Persons?Filter=Phone(=){broj}  (x-tenant = env default tenant, pilot=1 tenant)
             → osoba ima polje TenantId  ← TO je pravi tenant (identity.py: strict binding)
             → spremi za idući put
```
- Tenant dolazi iz `/Persons` ODGOVORA (polje `TenantId`), **NE iz tokena.**
- ⚠ OTVORENO ZA SKALIRANJE: s puno tenanta, "po telefonu naći tenant" traži
  pretragu po tenantima (ili backend lookup po broju) — cross-tenant je
  neriješen bottleneck, pitanje za Damira (§17/§24). Za pilot (1 tenant) — OK.

---

## §17 MOBILITYONE API — ugovori (za backend tim)
| # | Zahtjev | Zašto |
|---|---|---|
| 1 | strukturiran error `{error_code, field?, message}` za SVE 4xx | deterministički HR prijevod |
| 2 | **honoriranje `Idempotency-Key`** (dedup ≥10min) | timeout+retry = dupla mutacija bez toga (M9) |
| 3 | liste vraćaju `{items, total}` + max page | "imaš 27, prikazujem 10" (M7) |
| 4 | rate-limiti (429 + `Retry-After`) | backoff kalibriran naslijepo |
| 5 | **OAuth scope za sve rute** (pisana potvrda) | živo dokazan 403 failure |
| 6 | timezone semantika naive datetimea (Europe/Zagreb ili offset) | ±1-2h tiha korupcija (M8) |
| 7 | envelope ključ liste + gdje je `total` | danas pogađamo 7 varijanti |
| 8 | kanonski format telefona u /Persons + **cross-tenant lookup po broju** | identitet je nulti korak; §16 skaliranje |

---

## §18 SCENARIJI (sequence sažeci)
```
S1 READ: "moja putovanja" → list_trips → GET → {items,total:27} → "Zadnjih 10 od 27…"
S2 WRITE+Da/Ne: report_incident → echo+confirm → "Da" → POST → INC-5521 → HR potvrda
S3 MISSING PARAM: book_vehicle bez date_to → "Do kada?" → nastavi (ai.UserSession)
S4 ŠIFRARNIK: match=None → "Kvar, Šteta ili Nezgoda?"
S5 NEPOZNAT: enrollment poruka; nijedan API poziv bez tenanta
S6 SAFETY: rate-limit · injection · crisis hotline · GDPR audit
S7 BACKEND GREŠKA: 409 → HR "nema slobodnih vozila…"; 5xx → circuit breaker + pending ostaje
S8 RESTART usred obrade: ai.Message ostaje 'processing' → na bootu re-obrađen (M10)
S9 DUPLI webhook: UQ_inbound_dedup odbije na upisu (M14) — jedna obrada
S10 TEAMS/COPILOT/WEB: isti mozak, drugi adapter
```

---

## §19 SIGURNOST / GDPR
Secreti u **Key Vaultu** preko **Managed Identity** (ne u kodu/gitu) · HMAC
fail-closed · PII scrub pre-LLM + na logovima · input/output sanitizer · tenant
strict-binding · admin token gate · GDPR: erasure + audit (`ai.Feedback`/audit
tablica) · PII pseudonimizacija saltana. **App Insights** za monitoring + alarme
(DLQ-ekvivalent: `ai.Message.Status=failed` → alert rule).

---

## §20 DEPLOY (postojeći AKS)
```
mobilityone-ai:  1 deployment (Boris skele — standardno), ingress /api/ai/*
  · dijeli: SQL Server (schema ai) · Key Vault · Managed Identity · App Insights · CI/CD
  · liveness/readiness: /api/ai/health, /api/ai/ready (HTTP — servis IMA port)
  · outbox loop startan u lifespan; na bootu recovery 'processing'→'received'
NEMA: Redis · zaseban worker · KEDA · Postgres · drugi AKS · AGIC (izbačeno namjerno)
Skaliranje: 1 pod PROCIJENJENO dovoljan za ~120 vozača (nije izmjereno pod
  opterećenjem — potvrditi load testom prije oslanjanja). Na 2+ poda: rate-limit
  per-pod (sitnica), outbox claim ostaje atomičan (SQL UPDATE), redoslijed očuvan.
  Pragovi (>150 msg/min → 2 poda; >500 korisnika → SQL read replica) su PROCJENE
  s brojem, ne izmjerene granice — mjeriti telemetrijom (§29.3), ne pretpostaviti.
```

### 20.1 CI/CD PIPELINE (K6 — objektivan "gotovo" gate)
```yaml
# .github/workflows/ci.yml (ili Borisov Azure DevOps ekvivalent — isti gateovi)
env: { APP_ENV: testing, SQL_CONNECTION_STRING: <test-sqlite/localdb>, ...mock secreti }
steps:
  - ruff check .                                  # lint — vidi pooštrenje dolje
  - mypy engine adapters mobilityone outbox db    # TYPE-CHECK jezgre (§27.5 "type hints svugdje")
  - pytest -m "not integration" --cov=engine --cov=adapters --cov=mobilityone \
           --cov-fail-under=85                     # coverage gate ≥85%
  - pytest tests/contract -q                       # offline contract fixturi (§17/§21)
  - python -c "from config import get_settings; get_settings()"   # config se učita
  - test_architecture (enforced manifest, BI-7) + test_dead_config (BI-6) + mine-manifest (dolje)
gate: nijedan crveni korak → merge blocked. Coverage <85% → blocked.
# deploy-blocking (ne per-PR, gated live LLM/DB kredencijalima):
  - pytest -m benchmark   # LLM-eval nad tests/benchmarks na 2 seeda (seed1337+2024),
                          # prag top1≥90 top3≥97 halluc==0 diffano vs pinana baseline
  - pytest -m integration # atomični claim concurrency (§5.1) + restart-recovery (S8/M10)
```
**RUFF POOŠTREN (zatvara: default ignore potkopava minu M1):** za JEZGRU
(`engine/adapters/mobilityone/outbox/db`) NE nasljeđuj legacy suppressione —
`select += ["ASYNC","TRY"]`, makni `E722`(goli except)/`B904`(raise bez from)/
`F401`(mrtvi import) iz global ignore; legacy suppression SAMO per-file za
`scripts/*`,`tests/*`,`alembic/*`. Goli `except:`/`raise` bez `from` u jezgri =
crven build (usklađeno s M1 ErrorCode-by-value, BI-11).

**OBAVEZNI test-gateovi (ne samo coverage %):**
- **MINE-MANIFEST:** svaka mina M1-M14 (§1.5) ima imenovani regression test;
  `test_mine_manifest` provjeri da svih 14 postoji (kao BI-7 za module).
- **SCENARIO-MANIFEST:** svaki S1-S13 (§18/§28) ima e2e test; manifest gate.
- **RED-TEAM e2e:** ne samo string-sanitizer (`test_prompt_injection`) nego e2e
  napadi (injection kroz cijeli turn, PII u izlazu, cross-tenant pokušaj).
- **BENCHMARK GATE:** promjena `AZURE_OPENAI_DEPLOYMENT_NAME` ILI §14.1 promptova
  zahtijeva zelen 2-seed benchmark vs pinana baseline PRIJE deploya (§22#8).
- **CONCURRENCY:** atomični claim (§5.1) — 2 taska ne uzmu istu poruku — dokazan
  integration testom protiv LocalDB (SQLite ne podržava READPAST).
- Reproducibilnost: ovisnosti lockane (`pip-tools requirements.lock --generate-hashes`
  ili `uv.lock`); CI instalira iz locka.
- **ZADRŽI postojeći tooling (§26):** `ruff`, `mypy`, `.pre-commit-config.yaml`,
  `Dockerfile` (multi-stage, non-root) — NE gubi ih u evoluciji.
- Offline testovi protiv **SQLite/LocalDB**; live contract/integration gated env-om.
- Deploy TEK nakon zelenog CI + `verify_production_readiness` (preflight ok AND verified).

---

## §21 ACCEPTANCE + GARANCIJA ODGOVORA
**Akcija POSTOJI tek sa svih 6:** actions.json entry (two-level opisi) · offline
contract fixture · live contract PASS · smoke na dev API · e2e razgovorni test ·
uključena u benchmark. **Sustav GOTOV:** svih ~30 akcija × 6 + agregat
**90/97/0** (top-1/top-3/halucinacije) na dual-seed + 2 tjedna pilota + showstopper
bez 🔴 + auth preflight `ok AND verified` uz live kredencijale.

**Garancija "uvijek odgovor" (INV-6) — po putu:**
- **ASYNC (webhook kanali):** svaka poruka završi `ai.Message.Status=answered`
  (poslano) ILI `=failed` + `Error` + App Insights alarm. Restart-safe (M10),
  dedup (M14), redoslijed (INV-8).
- **SYNC (`/api/ai/chat`, §15.1):** garancija JE HTTP odgovor — engine vrati
  tekst; iznimka/timeout → HTTP 200 sa sigurnom HR fallback porukom (nikad prazan
  body ni 500 korisniku). Opcionalni `ai.Message` zapis za povijest/audit.

Honest bound: finalna DOSTAVA je na Infobip/Teams/Web platformi. **Svaki novi
izlaz = novi redak ovdje + test.**

---

## §22 SHOWSTOPPER REGISTAR
| # | Rizik | Mitigacija | Status |
|---|---|---|---|
| 1 | Boris još nije skelirao `mobilityone-ai` servis | standardni AKS deployment; naša odgovornost = kod | 🟡 čeka |
| 2 | `ai` schema DDL nije potvrđen | §5 prijedlog → potvrda s Damirom/Borisom (EF vs raw SQL) | 🟡 |
| 3 | OAuth scope / Managed Identity prava za API | auth_preflight + pisana potvrda (§17#5) | 🟡 |
| 4 | Cross-tenant lookup po broju (skaliranje) | §16/§17#8 — pitanje za Damira; pilot=1 tenant OK | 🟡 |
| 5 | Viber sender registracija (Infobip) | ops akcija (dani-tjedni); kod spreman | 🟡 |
| 6 | Timezone / Idempotency / envelope (§17) | ugovori s backendom | 🟡 |
| 7 | Infobip SPOF | adapter sloj čini providera zamjenjivim | 🟢 |
| 8 | LLM model drift | PIN verzija + benchmark gate | 🟢 |
| 9 | 1 pod SPOF | AKS restart brz; ai.Message trajan → nema gubitka; prag → 2 poda | 🟢 |

---

## §23 REDOSLIJED GRADNJE (od nule)
```
 1. config.py (env §4, Key Vault) + main.py skeleton + /api/ai/health
 2. db/schema.sql (ai.*) + repository.py (Message/UserSession/ToolCallLog)
 3. adapters/base.py + infobip.py (HMAC, parse, send; M2/M4) — testovi
 4. webhook rute → repo.insert_inbound (dedup M14) → 200
 5. outbox/loop.py (claim/process/mark; M10 recovery) — testovi
 6. engine SAFETY (rate/PII/injection/crisis) — po modulu test
 7. identity (strict tenant §16) + special_intents
 8. actions.json + action_registry + action_validator
 9. llm_router (30 shema) + formatter
10. params (pending u ai.UserSession) + mutation_gate
11. executor + api_gateway (auth §12) + api_error_translator
12. auth_preflight (§13) + contract harness (§17)
13. e2e razgovori (S1-S10) + benchmark
14. App Insights + deploy na AKS (Boris) → pilot
```

---

## §24 OTVORENI GATEOVI (vanjski)
1. Boris skele `mobilityone-ai` deployment (standardno).
2. `ai` schema DDL potvrda (EF migracija vs raw SQL — Borisov standard).
3. Auth: Managed Identity vs client_credentials + scope grant.
4. Cross-tenant lookup po broju (§16) — dizajn za skaliranje.
5. Backend ugovori §17 (idempotency, timezone, envelope, error shape).
6. Viber sender registracija (Infobip, ops).
7. Granularne rute vs `/actions` Business API (tko orkestrira — World A/B) — može i granularno za start.

---

## §25 SAMOPROVJERA GRADITELJA (nakon svakog koraka + prije "gotovo")
```
[ ] Suite koraka ZELENA (ne "kasnije")?
── INVARIJANTE §1 ── write bez Da/Ne? PII prije LLM? tenant bez env-defaulta?
   crisis/GDPR terminal? svaki izlaz→answered ILI failed+alarm? idempotencija?
── MINE §1.5 ── slanje: ErrorCode.value ne literal (M1); recipient hook (M2)?
   auth: verified ne samo ok (M3)? outbox: answered TEK nakon slanja (M10)?
   dedup na upisu (M14)? datetime Europe/Zagreb (M8)?
── BUILD-INTEGRITY §1.6 ── sposobnost ima e2e (BI-1)? svaki write ima read (BI-2)?
   config/modul/tablica ima potrošača (BI-6/7/8)? nema Redis/worker artefakata (BI-9)?
   adapter je jedini s kanal-formatom (BI-12)? svaki šav ima test (BI-13)?
── CONFIG §4.5 ── nijedna tajna u git; nijedan hardkodiran host; env preko settings?
── KONVERZACIJA §28 (K12) ── out-of-scope ima graciozan izlaz (ne halucinira akciju)?
   multi-intent ne gubi drugi intent? promjena teme usred params ne guta poruku? ton "ti"?
── OPS §29 ── outbox-heartbeat→ready (petlja ne umre tiho)? GDPR retencija+erasure
   za ai.*? admin messages/routing-log/pause? App Insights metrike+alarmi? migracije?
── TEST-RIGOR §20.1 ── mypy gate? ruff pooštren (goli except u jezgri crven)?
   benchmark deploy-gate? mine-manifest (M1-M14)? concurrency-claim integration test?
── PRIJE "GOTOVO" ── svih ~30 akcija ×6 (§21)? benchmark 90/97/0? showstopper bez 🔴?
   preflight ok AND verified? Key Vault/Managed Identity za sve tajne?
```
**Ako je i jedan red crven — samoocjena NIJE 10/10. Nastavi raditi.**

---

## §26 REUSE-MAPA (ako evoluiraš POSTOJEĆI Python sustav, ne gradiš od nule)

> Dvije upotrebe ovog prompta: **(A) svjež build** — gradi po §23 od nule.
> **(B) evolucija** — imamo radni Python sustav (1756 testova, 14 mina riješeno);
> tada NE piši ispočetka mozak — RE-HOME-aj infrastrukturu. Mapiranje:

**ZADRŽI (mozak — kopiraj ~1:1, samo stanje ide u `ai.UserSession` umjesto Redisa):**
| Postojeće | → Cilj (v3.1) |
|---|---|
| `services/v2/engine.py` (V2Engine) | `engine/conversation_service.py` (isti slojevi; pending/history → `ai.UserSession`) |
| `services/v2/{rate_limiter,pii_scrubber,input_sanitizer,output_sanitizer,crisis_detector,identity,special_intents,mutation_gate,param_ui,type_resolver,api_error_translator,conversation_history}.py` | `engine/*` (logika NETAKNUTA) |
| `services/v2/{meta_intents,multi_intent_detector,negation_handler,intent_type,driver_basics}.py` | `engine/*` — **konverzacijski moduli (§28): out-of-scope, multi-intent, negacija — ne izgubiti/reinventirati!** |
| `services/router/llm_router.py` + `action_registry`/`validator` | `engine/routing/*` |
| `services/formatter/llm_formatter.py` | `engine/formatter.py` |
| `services/whatsapp_service.py` + `viber_service.py` | `adapters/infobip.py` (VEĆ su adapteri! M2/M4 ostaju) |
| `services/api_gateway.py` · `token_manager.py` · `auth_preflight.py` | `mobilityone/*` (netaknuto) |
| `config/actions.json` · svih 14 mina · pripadni testovi | ostaju |
| **TOOLING:** `ruff`/`mypy` config (pyproject) · `.pre-commit-config.yaml` · `Dockerfile` (multi-stage, non-root) · `tests/` infra | ZADRŽATI — ne gubiti u evoluciji |

**RE-PLUMBAJ (storage/infra — jedini pravi posao):**
| Postojeće | → Cilj |
|---|---|
| Postgres `user_mappings` + `tenant_resolver` | SQL `ai` schema + `db/repository.py` |
| Redis (stream/queue/pending/history/dedup/rate-limit/sent) | `ai.Message` (outbox) + `ai.UserSession` + in-memory (§5 mapa) |
| `webhook_simple.py` (FastAPI webhook) | `adapters/*` + `main.py` rute → upis u `ai.Message` |
| `worker.py` (zaseban proces, Redis consumer) | `outbox/loop.py` (background task u ISTOM servisu, §7) |

**OBRIŠI:** Redis klijent · k8s Redis/Postgres manifesti · KEDA · zaseban
worker-deployment · (ako ideš ravno na ~30 akcija) 950-skela (tool_data/registry/
anchor — bila prijelazna). BI-9 to i provjerava.
**PRUNE OVISNOSTI (`requirements*.txt`/`pyproject`):** makni `redis` (Redis
maknut), `fastapi-limiter` (rate-limit in-memory), `asyncpg`/`psycopg2*` (cilj je
SQL Server → dodaj `pyodbc`/`aioodbc`), `scikit-learn`+`numpy` (mrtvi bez 950-skele).
BI-9 gate grepa i manifest ovisnosti — mrtva teška ovisnost = crven build.

**Redoslijed evolucije (Strangler-safe):** 1) SQL `ai` schema + repository →
2) adapteri iz postojećih *_service.py → 3) outbox loop zamijeni worker →
4) prebaci pending/history s Redisa na `ai.UserSession` → 5) makni Redis →
6) deploy kao jedan servis. Suite mora ostati zelena na SVAKOM koraku.

---

## §27 ENGINEERING STANDARDS + PROŠIRIVOST (kako da bude enterprise, ne gomila se, scalable)

> Ovo je "KAKO implementirati", ne "što". Cilj: dodavanje kanala/akcije NIKAD ne
> dira jezgru; kod ostaje čist i skalabilan. Ovo je K11 rubrike.

### 27.1 Ports & Adapters (hexagonalno) — zašto se ništa NE gomila
```
      ADAPTERI (rub — mijenja se često)              JEZGRA (mozak — stabilna)
  Infobip/Teams/Web ─implements─▶ ChannelPort ─┐
  MobilityONE API   ─implements─▶ FleetPort    ─┤
  Azure OpenAI      ─implements─▶ LLMPort       ─┼─▶ ConversationService
  SQL repository    ─implements─▶ StorePort     ─┘    ovisi SAMO o PORTOVIMA
                                                       (sučeljima), NE o konkretnim
                                                       Infobip/SQL/OpenAI klasama
```
**Dependency inversion:** jezgra ovisi o SUČELJIMA, ne o konkretnim klasama.
Posljedica: zamjena providera (Infobip→drugi, Azure→drugi LLM) = novi adapter,
jezgra NETAKNUTA. To je jedini razlog zašto dodavanje ne gomila kod.

### 27.2 EXTENSION RECIPES (korak-po-korak — dokaz da je proširivo)
**Dodati NOVI KANAL (npr. Teams):**
```
1. adapters/teams.py: implementiraj ChannelAdapter (parse_inbound/send/verify_signature/MAX_LEN)
2. registriraj u adapter-registry: ADAPTERS["teams"] = TeamsAdapter()
3. main.py: dodaj rutu @app.post("/api/ai/webhooks/teams")  (3 linije, isti obrazac §6)
4. tests/test_teams_adapter.py
→ NULA promjena u ConversationService, routeru, executoru, SQL-u.
```
**Dodati NOVU AKCIJU (npr. add_mileage):**
```
1. config/actions.json: dodaj entry (schema §11) — opis + use_when + examples + params
2. ako backend orkestrira (/actions/*): NIŠTA VIŠE.  ako granularno: 1 mapiranje u executor
3. tests/contract/fixtures/add_mileage.json + e2e razgovorni test
4. uključi (per-action flag)
→ NULA promjena u routeru/mozgu (router čita actions.json dinamički).
```
**Dodati MobilityONE endpoint:** proširi api_gateway mapiranje; jezgra netaknuta.

> **Test proširivosti (BI):** ako dodavanje kanala/akcije traži izmjenu
> `conversation_service.py` ili `llm_router.py` — dizajn je POGREŠAN. Jezgra je
> zatvorena za izmjenu, otvorena za proširenje (Open/Closed).

### 27.3 SCALABILITY (konkretno, ne fraza)
- **Servis je STATELESS** — SVE stanje u SQL (`ai.Message`, `ai.UserSession`).
  Horizontalno skaliranje = samo dodaj pod (SQL je source of truth, claim atomičan §5.1).
- Nema in-process stanja koje se gubi (osim rate-limit/breaker — per-pod, OK).
- **SQL kao granica:** connection pooling; indeks `IX_Message_pending`; pri
  volumenu → read replica ili particija po TenantId (TEK kad metrika pokaže).
- **LLM je usko grlo prije infre** — 2 poziva/turn; kvota se diže zahtjevom;
  async svugdje da jedan spori LLM poziv ne blokira druge.
- Pragovi rasta su ODLUKE s brojem (§20), ne nagađanja: >150 msg/min → 2 poda;
  >500 korisnika → SQL read replica. Ne gradi unaprijed (YAGNI).

### 27.4 ANTI-BLOAT DISCIPLINA (pozitivno)
- **YAGNI:** gradi za DANAS (1 pod, ~120 vozača). Kompleksnost TEK kad metrika
  pokaže potrebu. Svaki "za svaki slučaj" je budući mrtav kod.
- Svaki modul: JEDNA odgovornost + test + potrošač (BI-6/7/8).
- **Config-driven > code-driven** gdje god ide (actions.json, ai.Channel) —
  nova sposobnost bez deploya.
- **Brisanje je feature:** mrtav kod van (dead-config test, enforced manifest).

### 27.5 KOD STANDARDI (enterprise)
- Type hints svugdje; `@dataclass` za modele; `async` za sav I/O.
- **Dependency Injection:** ovisnosti se INJEKTIRAJU kroz konstruktor, NE kreiraju
  unutar modula (testabilnost — svaki dio testabilan u izolaciji s fake portovima).
- Jezgra NE importa adapter-SDK (Infobip/Bot Framework) — BI-12; grep to čuva.
- Structured logging (App Insights) + correlation_id kroz cijeli turn (webhook→outbox→engine→send).
- Svaki javni put ima test; contract testovi na SVAKOJ granici (port).
- Greške po `ErrorCode` VRIJEDNOSTI (M1); nikad gole iznimke koje se gutaju.

### 27.6 WHATSAPP-FIRST (i zašto dodavanje ostalog nije problem)
Za pilot gradiš SAMO `adapters/infobip.py` + `/api/ai/webhooks/infobip`. Teams/
Web/Copilot su **contract-stubovi** (sučelje fiksno, implementacija kad dođu na
red). Kad dođe Teams: recipe 27.2 — 4 koraka, jezgra netaknuta. To je cijela
poanta ports&adapters dizajna: **WhatsApp danas, ostalo bez boli sutra.**

---

## §28 KONVERZACIJSKA PRAVILA (kako bot RAZGOVARA — K12)

> Mozak zna ŠTO napraviti; ovo je KAKO priča. Ova pravila su i test-checklist
> (svako ima e2e). Postojeći moduli koji ovo IMPLEMENTIRAJU i ZADRŽAVAJU se
> (§26): `meta_intents.py`, `multi_intent_detector.py`, `special_intents.py`,
> `negation_handler.py`, `output_sanitizer.py`.

**Persona / ton:** neformalno **"ti"** (vozači, ne korporativno), kratko (2-4
rečenice), hrvatski, topao ali profesionalan. Emoji SAMO funkcionalno (1️⃣2️⃣3️⃣ u
clarify listi); inače bez emoji-spama. Isti ton kroz SVA 4 glasa (formatter,
clarify, confirm echo, welcome).

**OUT-OF-SCOPE (`kind=out_of_scope`) — HIGH, deterministički, 0 API/LLM poziva:**
```
"koliko je sati" / vic / vrijeme / "tko si ti" →
  "Ja sam AI asistent za tvoj vozni park — mogu ti {Faza 1: rezervirati vozilo
   ili prijaviti kvar}. S tim ti rado pomažem."
```
NIKAD ne halucinira akciju na off-topic (mina: stari router je forsirao najbliži
tool → izabrao bi list_trips na "koliko je sati"). Novi router ima granu
out_of_scope (§14.1).

**VIŠE-INTENT u jednoj poruci — MED:** ("rezerviraj auto i prijavi kvar")
detektira `multi_intent_detector`; obradi PRIMARY (sigurnosni intent uvijek
primary), `pending_followup` spremi u `ai.UserSession`; nakon uspješnog exec-a
primarne akcije, u IZLAZ umetni: *"Riješeno. Rekao si i {B} — da to sad?"*
Drugi intent se NIKAD tiho ne gubi (osobito ako je report_incident).

**PROMJENA TEME usred prikupljanja parametara — HIGH:** dok pending_params čeka
vrijednost, PRIJE nego tekst tretiraš kao param, proslijedi ga routeru kao
kandidat. Ako router s povjerenjem prepozna DRUGU akciju ILI tekst signalizira
novu namjeru ("zapravo…", "ipak…", "ne, radije…") → NE spremaj kao param;
potvrdi prekid (*"Ok, ostavljam {stara akcija}. {nova}?"*) + očisti pending.
Inače nastavi kao odgovor na param. (Obrni default raw-fallbacka u param_ui: ne
gutaj sve kao string.) "odustani" → čisti abort (kao dosad).

**Mješoviti / engleski ulaz:** razumij engleski, ali ODGOVARAJ na hrvatskom
(vozači pišu HR; poneki upišu "book a car"). Ne prekidaj razgovor zbog jezika.

**Zatvaranje:** "hvala"/"bok"/"to je sve" → kratak topao close ("Nema na čemu,
javi se kad god trebaš!"), bez pokušaja nove akcije.

**Scenariji (dodaci §18):** S11 OFF-TOPIC ("koliko je sati" → out_of_scope
template → 0 executor poziva) · S12 MULTI-INTENT ("auto i kvar" → primary=incident,
followup=booking, oba obrađena) · S13 TOPIC-CHANGE (usred booking params "ipak
prijavi kvar" → potvrdi prekid, clear pending, re-route). Svaki = e2e test (BI-1)
koji asertira da executor NIJE krivo pozvan i drugi intent nije izgubljen.
**§21 garancija:** out_of_scope i close su VALIDNI izlazi (smislen odgovor), ne
samo mehanički status.

---

## §29 OPS, DATA LIFECYCLE & MIGRACIJE (pravi sustav "u potpunosti")

### 29.1 GDPR — retencija + erasure za `ai.*` (HIGH — nose PII)
```sql
-- RETENCIJA (periodični job, isti scheduled task kao §5 session-cleanup):
DELETE FROM ai.Message      WHERE CreatedAt < DATEADD(day,-90, SYSUTCDATETIME());
DELETE FROM ai.Conversation WHERE LastActivityAt < DATEADD(day,-90, SYSUTCDATETIME());
DELETE FROM ai.ToolCallLog  WHERE CreatedAt < DATEADD(day,-90, SYSUTCDATETIME());
DELETE FROM ai.Feedback     WHERE CreatedAt < DATEADD(day,-180, SYSUTCDATETIME());
-- ai.UserSession: već ExpiresAt (§5).
-- ERASURE (Art.17, po Senderu, transakcijski — brisanje na zahtjev):
BEGIN TRAN;
  DELETE FROM ai.Message WHERE Sender=@s; DELETE FROM ai.UserSession WHERE Sender=@s;
  DELETE FROM ai.ToolCallLog WHERE Sender=@s; DELETE FROM ai.Conversation WHERE Sender=@s;
COMMIT;
```
Erasure ide kroz admin GDPR put (isti obrazac kao postojeći `/admin/gdpr-process`).

### 29.2 ADMIN / OPS SURFACE (vidljivost + kill-switch — iza ADMIN_TOKEN)
```
GET  /api/ai/admin/messages?status=&limit=N   → zadnjih N ai.Message (failed/stuck/processing)
GET  /api/ai/admin/routing-log?tenant=&limit=N→ ai.ToolCallLog (zamjena za Redis routing-log)
POST /api/ai/admin/pause {channel}            → ai.Channel.Enabled=0 (kill-switch po kanalu)
```
`ai.Channel.Enabled` MORA imati čitača (BI-2): webhook §6 provjeri Enabled prije
`insert_inbound` (disabled → 200 ali ne queue). Time kanal-pause stvarno radi.

### 29.3 OBSERVABILITY (App Insights — imenovane metrike + alarmi)
| Metrika | Alarm |
|---|---|
| turn latency (p50/p95) | p95 > 15s |
| LLM cost / dan (token count × cijena) | > dnevni budžet → alarm (loop-breaker: max N turnova/sesija) |
| error rate (`ai.Message.Status=failed` count) | > prag → alarm (DLQ-ekvivalent) |
| outbox lag (najstariji `received` age) | > 60s → petlja zaglavila (vidi 29.4) |
correlation_id kroz cijeli turn (webhook→outbox→engine→send) u svakom logu.

### 29.4 HEALTH / READINESS + OUTBOX HEARTBEAT (zatvara "outbox tiho umre" — HIGH)
```
GET /api/ai/health  → proces živ (200)
GET /api/ai/ready   → SQL dosežljiv I outbox petlja živa (heartbeat < 30s stara)
```
Outbox petlja upisuje heartbeat (timestamp) svaki ciklus; ako stane/zaglavi →
`ready` pada → k8s restarta pod + alarm. **Bez ovoga garancija §21 ne vrijedi**
(poruke bi tiho stajale u `received`).

### 29.5 SCHEMA MIGRACIJE (post-v1)
Migracije: `db/migrations/NNN_opis.sql` (forward-only, numerirane) ILI EF (Borisov
standard) — ista dualnost kao §5. **Expand-contract, ADITIVNO:** `ADD COLUMN NULL`/
`CREATE INDEX` dok stari pod radi; drop/rename/alter-type TEK kad je stari kod
izvan prometa. CI: `migrate step → THEN deploy`; boot preflight "schema na
očekivanoj verziji". Rollback sheme = nova forward migracija, ne in-place undo.

### 29.6 DEPLOY ROLLBACK + SECRET ROTATION
- **Rollback:** `kubectl rollout undo`; in-flight poruke su u `ai.Message`
  (trajne) → novi/stari pod ih preuzme, ništa se ne gubi (expand-contract čini
  shemu kompatibilnom oba smjera tijekom rollouta).
- **Secret rotation runbook:** Key Vault nova vrijednost → rolling restart
  (token_manager forsira refresh na 401). Rotirati: MOBILITY_CLIENT_SECRET,
  INFOBIP_API_KEY/SECRET, Azure key — SVE zalijepljeno u chat/ngrok-era ODMAH.
