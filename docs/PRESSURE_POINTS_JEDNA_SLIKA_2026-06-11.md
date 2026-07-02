# PRESSURE POINTS + JEDNA SLIKA — što napadamo, kako garantiramo, koji files

*Radni fokus-dokument (Filip, 2026-06-11). Master referenca ostaje
[ACTIONS_TEHNICKA_SPECIFIKACIJA](ACTIONS_TEHNICKA_SPECIFIKACIJA_2026-06-11.md) —
ovdje su pressure pointovi, mehanizmi garancije, skice i files na koje se
fokusiramo SAD.*

---

## §0 JEDNA SLIKA — cijeli sustav, hosting i garancije na jednom ekranu

```
════════════════════════ VANJSKI SVIJET ════════════════════════════════════
  [WhatsApp user]   [Viber user]          [M365 Copilot user]   [Web user]
        │                │                        │                  │
  ┌─────▼────────────────▼─────┐                  │                  │
  │        INFOBIP CLOUD        │                  │                  │
  │  WA kanal      Viber kanal  │                  │                  │
  │  (živ)         (⚠ PP4:      │                  │                  │
  │                 sender      │                  │                  │
  │                 approval!)  │                  │                  │
  └─────────────┬───────────────┘                  │                  │
                │ HTTPS webhook                     │ MCP protokol     │ /chat (F-Web)
════════════════▼═══════ NAŠ CLUSTER (AKS / VM) ═══▼══════════════════▼═════
  namespace: mobility-bot                    ┌──────────────┐
  ┌──────────────────────────┐               │ mcp/server.py │ (F-M365,
  │ INGRESS (nginx+TLS cert) │               │ ⚠ Entra auth  │  omata iste
  └──────────┬───────────────┘               └──────┬───────┘  akcije)
  ┌──────────▼───────────────┐                      │
  │ bot-api ×2 (HPA 2-4)     │  HMAC → dedup → XADD │
  └──────────┬───────────────┘                      │
  ┌──────────▼───────────────┐                      │
  │ REDIS (AOF, noeviction)  │  stream+queue+state  │
  └──────────┬───────────────┘                      │
  ┌──────────▼──────────────────────────────────────▼──────────────────────┐
  │ bot-worker ×1 (V2Engine)                                               │
  │  safety(rate/PII/inject) → identity → LLM decision → action_validator  │
  │  → params(ask/coerce/codebook) → Da/Ne confirm → executor              │
  │  [⚙ auth_preflight na startu: token scope + route probe — PP1]         │
  └───────┬──────────────────────────────┬─────────────────────────────────┘
  ┌───────▼───────┐              ┌───────▼──────────┐
  │ POSTGRES      │              │ AZURE OPENAI     │ (kvote — §14 spec)
  │ user_mappings │              │ gpt-4o-mini      │
  │ tenant_settings│             │ decision+format  │
  └───────────────┘              └──────────────────┘
                │ POST /actions/* (Bearer + x-tenant + Idempotency-Key)
════════════════▼═══════════ M1 CLOUD (MobilityOne) ════════════════════════
  ┌────────────────────┐   ┌─────────────────────────┐   ┌────────────────┐
  │ IdentityServer     │   │ BUSINESS API /actions/* │   │ DOMAIN API     │
  │ (OAuth token —     │   │ (⚠ PP3: gradi Damir,    │──▶│ 950 granularnih│
  │  scope = PP1)      │   │  contract testovi naši)  │   │ CRUD ruta      │
  │                    │   │ hosting = BUSINESS_API_  │   │                │
  └────────────────────┘   │ URL env (⚠ PP-SS2)      │   └────────────────┘
                           └─────────────────────────┘
════════════════════════════════════════════════════════════════════════════
 GARANCIJE (gdje koja živi):  PP1 preflight→worker startup · PP2 validator+
 contract testovi→prije executora/CI · PP3 per-action flag+circuit breaker ·
 odgovor uvijek→worker fallback trojka + DLQ+alarm (spec §17)
```

**Mini master flow (detalji: spec §11, 30 koraka):**
```
poruka → HMAC → dedup → stream → worker → safety → identity(→tenant) →
LLM bira 1 od ~30 akcija → validator → [fali param? pitaj] → [šifrarnik? riješi]
→ [write? "Potvrđuješ? Da/Ne"] → POST /actions/X → Business API orkestrira →
JSON → hrvatski → outbound (channel tag) → korisnik      [svaki izlaz = poruka]
```

---

## §1 PP-AUTH: "Hoće li auth uspješno proći? Kako to GARANTIRAŠ?"

**Što je već dokazano (ne garantiram — pokazujem):** OAuth `client_credentials`
flow **radi živo** — `token_manager.py` dobiva tokene u produkciji, identity
`/Persons` pozivi prolaze svaki dan. *Mehanizam* autha nije rizik.

**Tvoj uvid je točan i mijenja dizajn:** scope se **vidi iz tokena** — pa ga ne
trebamo "nadati se", nego **pročitati na startu**. Provjerio sam kod:
`token_manager.py:171` scope TRAŽI u token requestu, ali **nigdje ne provjerava
što je token stvarno dobio**. To je rupa koju zatvaramo novim modulom:

### `services/auth_preflight.py` (⟵ NOVO, ~80 linija) — dvoslojna garancija

```python
# SLOJ 1 — token introspection (tvoj uvid: scope je U tokenu)
import base64, json

def read_token_claims(access_token: str) -> dict:
    """JWT payload decode — bez signature verify (čitamo VLASTITI token,
    ne validiramo tuđi). Vraća claims: scope, aud, exp, client_id…"""
    payload_b64 = access_token.split(".")[1]
    payload_b64 += "=" * (-len(payload_b64) % 4)          # base64 padding
    return json.loads(base64.urlsafe_b64decode(payload_b64))

async def check_scopes(token_manager, required_scopes: set) -> list[str]:
    token = await token_manager.get_token()
    claims = read_token_claims(token)
    granted = set(str(claims.get("scope", "")).split())    # space-separated per OAuth2
    missing = sorted(required_scopes - granted)
    if missing:
        logger.error("AUTH PREFLIGHT: token NEMA scope(ove): %s (granted: %s)",
                     missing, sorted(granted))
    return missing

# SLOJ 2 — route probing (empirijski; radi ČAK I AKO M1 ne koristi granularne
# scope claimove — jer mjeri STVARNO ponašanje, ne deklaraciju)
async def probe_actions(gateway, actions: dict, tenant_id: str) -> dict:
    """Za svaku akciju: benign poziv → {akcija: http_status}. 403 = nema
    ovlasti; readiness FAIL ako ijedna potrebna akcija vrati 403."""
    results = {}
    for name, spec in actions.items():
        resp = await gateway.call(method="GET",  # ili HEAD/OPTIONS po ugovoru
                                  service="", path=spec["execution"]["action"],
                                  tenant_id=tenant_id)
        results[name] = resp.status_code
    return results
```

**Gdje se kvači:** worker startup (loud log + readiness signal) +
`scripts/verify_production_readiness.py` (CI/deploy gate) + dev smoke.
**Runtime mreža (već postoji):** 401→auto refresh (gateway), 403→HR objašnjenje
korisniku (api_error_translator), telemetry broji 403 po ruti.

**Garancija = lanac, ne obećanje:**
```
dokazan živi flow → preflight scope check (start) → route probe (dev/start)
→ runtime 401 refresh → runtime 403 HR prijevod + alarm
```
Ako scope fali → znamo **na startu, prije prvog korisnika** — ne iz produkcijske
greške. (Ostaje i ugovorna stavka §8#7 — pisana potvrda — ali sad je redundanca,
ne jedina linija obrane.)

**Files:** `services/auth_preflight.py` + `tests/test_auth_preflight.py` +
hook u `verify_production_readiness.py` + poziv u `worker.py` startup.

---

## §2 PP-POZIV: "Da TOČNO i POTPUNO pozovemo svaku rutu — kako garantiraš?"

**Dokaz da temelj već radi:** živi test 2026-05-30 = **0×422** — "bot strukturno
ispravno gradi pozive" (DAMIR_ACCURACY_UGOVOR:27). Plus 1749 testova + 4 E2E
koja **asertaju egzaktan poziv** (method/service/path/body/tenant/idempotency).
Točnost *konstrukcije poziva* nije teorija — izmjerena je.

**Za /actions svijet — garancijski lanac PO AKCIJI (svaka karika ima svoj file):**

| # | Karika | Što hvata | File |
|---|---|---|---|
| 1 | schema akcije (opisi+examples) | LLM zna ŠTO ekstrahirati | `config/actions.json` (spec §3) |
| 2 | action_validator | izmišljenu akciju/polje, krivi tip | `services/v2/action_validator.py` (spec §5.2) |
| 3 | coercion | HR datume/brojeve → ISO/int | `param_ui.py` + `_coerce_llm_params` (postoji) |
| 4 | missing-param loop | nepotpun poziv → pita korisnika | `pending_params.py` (postoji) |
| 5 | codebook resolve | krivi šifrarnik kod | `type_resolver.py` (postoji) / backend (§10.2) |
| 6 | identity inject | krivi person/tenant | `executor` + `identity.py` (postoji) |
| 7 | echo + Da/Ne | korisnik VIDI točan sadržaj prije slanja | `mutation_gate` + `_render_param_echo` (postoji) |
| 8 | **contract test** | drift između nas i backenda | `tests/contract/test_actions_contract.py` ⟵ NOVO |
| 9 | dev smoke | stvarni API se slaže | probe skripte (postoje, pattern) |

**Contract fixture primjer** (po akciji jedan par — golden request/response):
```json
// tests/contract/fixtures/report_incident.json
{ "request":  { "registration_plate": "ZG-1234-AB", "description": "pukla guma",
                "person_id": "p-77", "tenant_id": "t-A" },
  "response": { "status": "success", "incident_id": "<any-string>",
                "vehicle_status": "blocked" },
  "errors":   [ { "when": "nepostojeća registracija",
                  "expect": { "error_code": "VEHICLE_NOT_FOUND" } } ] }
```

**PRAVILO (ovo je garancija):** akcija se u produkciji uključuje (per-action
flag) **TEK** kad prođe (8) contract test protiv dev backenda i (9) smoke.
Nijedna akcija "na ruke".

---

## §3 PP-BUSINESS-API: "Da bude točno izgrađen — kako, kad ga gradi Damir?"

**Iskreno: tuđi kod se ne garantira. Garantira se DETEKCIJA + IZOLACIJA:**

1. **Specifikacija prije gradnje** — 8-točki ugovor (spec §8: DTO, strukturiran
   error, šifrarnici, Idempotency-Key, {items,total}, rate-limiti, SCOPE,
   timezone). Damir ne počinje "otprilike".
2. **Provider contract testovi** — NAŠI fixturei (§2 gore) vrte se protiv
   NJIHOVOG dev okruženja; idealno ih dobiju i u svoj CI. Drift = crveni test
   kod nas, ne 500 kod korisnika.
3. **Per-action uključivanje** — `actions.json` flag po akciji; akcija koja ne
   prolazi contract = OFF (korisnik je ne vidi), ostale rade. Kill-switch bez
   deploya (spec §3.3 reload).
4. **Runtime izolacija** — strukturiran 4xx → HR poruka; 5xx → circuit breaker
   (njihov outage ne ruši bota, korisnik dobije pošten odgovor).

**Files:** isti contract testovi + `config/actions.json` enable flagovi +
postojeći `api_error_translator`/circuit breaker.

---

## §4 PP-VIBER: iskren explainer (jer si rekao da ne znaš kako se implementira)

**Koncept — zašto je ovo MALI posao:** Infobip je **agregator** — isti account,
isti API stil, isti webhook mehanizam koji već koristiš za WhatsApp; Viber je
samo **drugi kanal istog provajdera**. Ne gradi se novi bot — gradе se 2 mala
adaptera na rubu:

```
INBOUND:  Infobip portal config → naš POST /webhook/viber
          payload istog duha (sender, message text, messageId)
          → parse → stream_data{..., channel:"viber"} → ISTI stream, ISTI mozak

OUTBOUND: worker vidi channel=="viber" → POST na Infobip Viber send endpoint
          (umjesto WA endpointa) — ista retry/DLQ mašinerija
```

**⚠ KRITIČNI PUT NIJE KOD — nego registracija:** Viber business sender mora
biti **registriran i odobren preko Infobipa** (poslovni proces, tipično
**dani-tjedni**). Bez toga kanal ne radi ma kakav kod bio. **Akcija: pokrenuti
registraciju ODMAH** (Infobip portal / account manager), paralelno s razvojem.

**Pošteno ograničenje:** točan JSON format Viber payloada/endpointa potvrđujemo
iz Infobip dokumentacije pri implementaciji — ali arhitektura adaptera je
STABILNA bez obzira na format jer je izoliran u točno 2 funkcije
(`parse_inbound`, `send`) u `services/channels/viber.py`.

**Files:** `services/channels/viber.py` (⟵ NOVO), `/webhook/viber` ruta u
`webhook_simple.py`, outbound grana u `worker.py`, `tests/test_channels_dispatch.py`.

---

## §5 PP-HOSTING: "Gdje ćemo to sve hostati? Koja je arhitektura?"

**Odgovor: hosting je RIJEŠEN i NAPISAN** — `k8s/` folder sadrži produkcijske
manifeste (11 fileova + runbook): namespace, bot-api ×2 s HPA+PDB (webhook
nikad ne pada), bot-worker ×1 Recreate, Redis (AOF + noeviction — red poruka
NE SMIJE evictati), Postgres (ili managed), ingress + cert-manager TLS =
**javni HTTPS webhook URL (zamjena za ngrok)**, secrets, NetworkPolicy
(zero-trust), migrate Job. Kutije i tko-gdje: skica u §0.

**Gdje to vrtjeti — dvije opcije, ista slika:**

| | AKS (Azure Kubernetes) | 1 VM + docker-compose |
|---|---|---|
| HA prijema | ✅ (2 api poda, rolling) | ❌ (1 proces, restart = kratki gap; Infobip retry ublažava) |
| Trošak | ~150-250 €/mj (mali node pool) | ~30-60 €/mj |
| Effort | manifesti gotovi; `kubectl apply -k k8s/` | compose postoji; playbook postoji |
| Kada | produkcija/više tenanta | pilot (~120 vozača je OK) |

**Preporuka:** pilot smije na VM (jeftino, playbook postoji), produkcija na AKS
— **ista slika, isti kod, samo druga podloga**; prelazak je runbook, ne prepis.
Vanjski servisi u obje varijante isti: Azure OpenAI (kvote), Infobip (webhook
config), M1 cloud (IdentityServer + Domain + budući Business API →
`BUSINESS_API_URL` env, spec §3.3).

---

## §6 PP-KVALITETA: "Je li scalable, enterprise, load-bearing, inteligentno dizajniran?"

Checklista kriterij → mehanizam → dokaz (detalji: spec §14 + §17):

| Kriterij | Mehanizam | Dokaz |
|---|---|---|
| HA prijema poruka | api ×2, RollingUpdate maxUnavailable=0, HPA | k8s/api.yaml |
| Ništa se ne gubi | Redis AOF + noeviction; stream buffer; DLQ ×2; ack-nakon-enqueue | testovi + spec §17 (20 putanja) |
| Idempotency (3 razine) | webhook dedup, msg_lock, outbound sent: + Idempotency-Key prema API-ju | testirano (edge fixes) |
| Izolacija kvarova | circuit breaker (executor+gateway), per-action kill-switch | kod + testovi |
| Backpressure | queue apsorbira burst (latencija raste, gubitka nema) | dizajn §14 + PP10 računica |
| Observability | TelemetryEvent po odluci, DLQ depth alarmi, routing-log endpoint | živi kod |
| Security | HMAC fail-closed, PII scrub pre-LLM, injection guard, SSRF, tenant strict-binding, NetworkPolicy, non-root | kod + k8s + SECURITY.md |
| GDPR | consent, erasure endpoint (dry-run+real), PII u logovima maskiran, tenant offboarding | živi kod + spec §12 |
| Testiranost | 1749 testova, 4 E2E s exact-call asertacijama, benchmark protokol | CI zelen |
| Rollback | feature flag kill-switch + k8s rollout undo + per-action OFF | spec §3.3 + runbook |
| **Honest limits** | worker ×1 (put do ×N definiran: Redis per-sender lock), Redis 1 replica (→managed uz prag), dostava=platforma | spec §14 — bez ovoga bi red iznad bio marketing |

**Presuda:** da — uz DVA dizajnirana (ne skrivena) limita s definiranim putem
nadogradnje. To JE inteligentan dizajn: limiti su odluke s pragovima, ne rupe.

---

## §7 DODATNI PRESSURE POINTS (proaktivno nađeni — tražio si da ih istaknem)

| PP | Simptom | Rješenje | Files/vlasnik | Status |
|---|---|---|---|---|
| PP7 Infobip SPOF (svi kanali kroz 1 providera) | Infobip down = svi kanali down | adapter sloj (channels/) čini providera zamjenjivim; DLQ preživi outage pa isporuči; dual-provider = svjesna buduća odluka | channels/ dizajn | 🟢 mitigirano dizajnom |
| PP8 LLM model update mijenja ponašanje (temp 0 ≠ vječno isto) | tiha regresija točnosti | PIN deployment verziju u Azure; benchmark gate (oba seeda) na SVAKI model bump | bench skripte (postoje) | 🟢 protokol postoji |
| PP9 Redis SPOF (1 replica) | restart = kratki zastoj (AOF čuva podatke) | danas: AOF+brzi k8s restart dovoljno; prag: >500 aktivnih korisnika → managed Redis/HA | k8s/redis.yaml | 🟡 prag definiran |
| PP10 jutarnji burst (svi vozači 7:30) | latencija raste u redu | queue apsorbira bez gubitka; kapacitet ~MAX_CONCURRENT×(60/4s)≈75-150 poruka/min po workeru; prag za scale-out: sustained > toga → Redis per-sender lock (recept u k8s/README) | worker | 🟡 prag definiran |
| PP11 poison message (poruka koja ruši engine) | crash loop | VEĆ riješeno: msg_lock + iznimka→DLQ inbound (bez auto-retryja) — dokazano testom | worker (postoji) | 🟢 zatvoreno |
| PP12 rotacija secreta | istekli/procurjeli kredencijali | k8s secret update + rolling restart (runbook red); ODMAH: rotirati sve iz ngrok ere | ops runbook | 🟡 TODO ops |
| PP13 proaktivne poruke (podsjetnici) vs WhatsApp 24h prozor | poruke van prozora se ne isporučuju / template pravila | danas bot SAMO odgovara → nema problema; proaktivno = WA template approval — NE graditi dok se ne odobri | budući flag | 🟢 ograđeno |

*(Svi upisani i u kanonski registar: spec §14.4.)*

---

## §8 FILES FOKUS — točno što rješava koji PP

| PP | File(ovi) | Test | Kada |
|---|---|---|---|
| PP1 auth | `services/auth_preflight.py` + hook u `verify_production_readiness.py` + worker startup | `tests/test_auth_preflight.py` | **SAD** (skeleton radi i protiv postojećih ruta; /actions probe čim postoje) |
| PP2 poziv | `config/actions.json` + `services/v2/action_validator.py` + `tests/contract/test_actions_contract.py` + fixtures | contract suite | fixture template **SAD**; punjenje uz World A/B |
| PP3 Business API | isti contract testovi + per-action flagovi | isti | uz Damirov dev |
| PP4 Viber | `services/channels/viber.py` + `/webhook/viber` + worker grana | `tests/test_channels_dispatch.py` | kod uz F0; **registracija sendera ODMAH** |
| PP5 hosting | `k8s/*` (napisano) / compose za VM pilot | YAML valid + smoke | odluka AKS vs VM ovaj tjedan |
| PP6 kvaliteta | — (checklista nad postojećim) | postojeća suite | kontinuirano |
| PP7-PP13 | po tablici §7 | po retku | pragovi/TODO ops |

## §9 REDOSLIJED NAPADA

```
OVAJ TJEDAN (ništa ne ovisi o Damiru):
  1. Pokreni Viber sender registraciju kod Infobipa   ← najduži lead time!
  2. auth_preflight.py skeleton + test (radi već nad postojećim rutama)
  3. Contract fixture template + prva 3 fixturea (booking/mileage/incident)
  4. Odluka AKS vs VM za pilot (tablica §5) + rotacija ngrok-era secreta (PP12)

PONEDJELJAK (sastanak s Damirom):
  5. World A/B (tko gradi /actions) — GATE svega
  6. Scope potvrda + BUSINESS_API_URL (gdje će /actions živjeti)
  7. 8-točki ugovor (spec §8) = predaja specifikacije

POSLIJE (fazno, PLAN_KONVERGENCIJA):
  8. Prvih 5 akcija iza flaga → contract+smoke → ON → mjerenje → širenje
```
