# Tehnička specifikacija: `/actions`-based Fleet AI Bot

*Developer-facing spec. Namjena: iz ovog dokumenta programer (ili AI) može
izgraditi/migrirati sustav. Sadrži strukturu foldera, config sheme, primjere
koda, data-contracte i flow dijagrame za svaki tip sposobnosti.*

**Verzija:** 2026-06-11 · **Cilj:** tanki bot (prijevod jezika + sigurnost) nad
~30 poslovnih akcija; sva orkestracija i poslovna pravila u Business API-ju.

---

## 0. Kako čitati ovaj dokument

| Sekcija | Za koga |
|---|---|
| §1 Arhitektura + §2 Struktura foldera | brzi mentalni model |
| §3 Action registry (sheme + primjeri) | **glavni ugovor** — ovo AI vidi i ovo se mijenja bez koda |
| §4 Request lifecycle | kako poruka putuje kroz sve gateove |
| §5 Moduli + kod | konkretna implementacija po sloju |
| §6 Data contracts | točan JSON na svakoj granici |
| §7 Flows po sposobnosti | READ / WRITE / missing-param / šifrarnik / RAG |
| §8 Business API strana | što Damirov tim gradi (contract) |

Legenda u kodu: `# ⟵ NOVO` = dodati · `# ⟵ MIJENJA` = izmijeniti postojeće ·
`# (postoji)` = već u repou, reuse.

---

## 1. Arhitektura (high-level)

```
                         ┌──────────────────────────────────────────────┐
                         │                 KANALI                        │
     WhatsApp ──┐        │  webhook_simple.py  (HMAC, +channel tag)      │
     Viber ─────┼───────▶│  → Redis stream "whatsapp_stream_inbound"     │
     (Web/M365) ┘        └──────────────────────────────────────────────┘
                                        │
                                        ▼  worker.py (consumer loop)
     ┌─────────────────────────────────────────────────────────────────────┐
     │  AI BACKEND  (services/v2/engine.py — V2Engine)   „TANKI BOT"        │
     │                                                                     │
     │  SIGURNOST      rate_limiter → pii_scrubber → input_sanitizer        │
     │  IDENTITET      identity.resolve(phone) → person_id, tenant_id, veh. │
     │  ETIKA/PRAVO    crisis_detector · special_intents (GDPR)             │
     │  RAZUM (LLM)    llm_router → { action | clarify | answer }           │
     │  VALIDACIJA     action_validator (shema + anti-halucinacija)  ⟵ NOVO │
     │  PARAM COLLECT  pending_params (pita+pamti ako fali required)         │
     │  ŠIFRARNICI     type_resolver (tekst → tenant kod)  (postoji)         │
     │  POTVRDA        mutation_gate (Da/Ne za write)                        │
     │  IZVRŠENJE      executor → api_gateway → POST /actions/*      ⟵ MIJENJA│
     │  FORMAT         llm_formatter (JSON → hrvatski)                       │
     └─────────────────────────────────────────────────────────────────────┘
                                        │  POST /actions/<name>  {čisti payload}
                                        ▼
     ┌─────────────────────────────────────────────────────────────────────┐
     │  BUSINESS API  /actions/*     (MobilityOne — Damirov dio)            │
     │  validira pravila · orkestrira granularne pozive · mapira šifrarnike │
     └─────────────────────────────────────────────────────────────────────┘
                                        │  (interno)
                                        ▼
     ┌─────────────────────────────────────────────────────────────────────┐
     │  DOMAIN API  (950 granularnih CRUD ruta)  — data layer              │
     └─────────────────────────────────────────────────────────────────────┘
```

**Pravilo odgovornosti:** bot = *jezik + sigurnost + jedna čista akcija*.
Business API = *pravila + orkestracija*. Domain API = *podaci*.

---

## 2. Ciljna struktura files i foldera

```
mobilityone-whatsapp-bot/
├── config/
│   ├── actions.json                 # ⟵ NOVO: ~30 akcija (zamjenjuje tool_data.json 950)
│   ├── entity_translations_hr.json  # (postoji) HR nazivi entiteta
│   └── tenants/                      # (postoji) per-tenant scoping/overridi
├── services/
│   ├── router/
│   │   └── llm_router.py            # ⟵ MIJENJA: bez Stage A (30 akcija idu direktno u LLM)
│   ├── formatter/
│   │   └── llm_formatter.py         # (postoji) JSON → hrvatski
│   ├── v2/
│   │   ├── engine.py                # ⟵ MIJENJA: dispatch nad akcijama
│   │   ├── action_registry.py       # ⟵ NOVO: učita+validira actions.json, daje LLM tool schema
│   │   ├── action_validator.py      # ⟵ NOVO: validacija AI outputa prije executora
│   │   ├── executor.py              # ⟵ MIJENJA: grana → POST /actions/<name>
│   │   ├── type_resolver.py         # (postoji) šifrarnik: tekst → tenant kod
│   │   ├── pending_params.py        # (postoji) missing-param loop
│   │   ├── param_ui.py              # (postoji) HR pitanja + parsanje
│   │   ├── mutation_gate.py         # (postoji) Da/Ne za write
│   │   ├── pending_mutation.py      # (postoji) state confirm gatea
│   │   ├── identity.py              # (postoji) phone → person/tenant/vehicle
│   │   ├── crisis_detector.py       # (postoji) suicid → hotline (NE briši)
│   │   ├── special_intents.py       # (postoji) GDPR/welcome/handover (NE briši)
│   │   ├── pii_scrubber.py · input_sanitizer.py · rate_limiter.py   # (postoji)
│   │   ├── conversation_history.py  # (postoji) rolling kontekst (5 turnova/30min)
│   │   ├── knowledge/               # ⟵ NOVO (Faza 2): RAG nad tenant dokumentima
│   │   │   ├── rag_retriever.py      #   pretraga (embeddings nad car-policy/putni-nalog…)
│   │   │   └── doc_store.py          #   upload/indeks dokumenata po tenantu
│   │   └── telemetry.py             # (postoji)
│   ├── channels/                    # ⟵ NOVO: adapteri po kanalu (edge)
│   │   ├── whatsapp.py               #   (izdvoji iz whatsapp_service.py)
│   │   └── viber.py                  #   Viber inbound parse + outbound send (Infobip)
│   ├── tenant_config.py             # ⟵ NOVO (F1): bot-side tenant postavke (§12; kod primjer tamo)
│   ├── mcp/
│   │   └── server.py                # ⟵ NOVO (F-M365): MCP server — omata ISTE akcije za Copilot (§11.3 S10)
│   ├── api_gateway.py               # (postoji) HTTP + circuit breaker + SSRF + idempotency
│   ├── token_manager.py             # (postoji) OAuth client_credentials
│   └── admin_auth.py                # (postoji)
├── webhook_simple.py                # ⟵ MIJENJA: +channel tag, +/webhook/viber
│                                     #   (F-Web: + sync /chat fasada u main.py — isti engine, bez dupliranja)
├── worker.py                        # ⟵ MIJENJA: outbound grananje po channel tagu
├── k8s/                             # (postoji) produkcijski deploy (NE briši)
├── Dockerfile · docker-compose.yml  # (postoji)
├── alembic/versions/004_tenant_settings.py   # ⟵ NOVO (F1): migracija za §12
└── tests/                           # svaki novi modul + svoj test:
    ├── test_action_registry.py · test_action_validator.py      ⟵ NOVO (F1)
    ├── test_tenant_config.py · test_channels_dispatch.py        ⟵ NOVO (F0/F1)
    ├── test_mcp_server.py                                       ⟵ NOVO (F-M365)
    └── tests/v2/test_e2e_actions.py  # E2E razgovori nad akcijama ⟵ NOVO (F1)
```

> Migracija je **fazna** (vidi `PLAN_KONVERGENCIJA_10_OD_10`): `actions.json` živi
> PORED `tool_data.json` iza feature-flaga dok se ne dokaže, pa se staro povlači.
>
> **Potpunost liste (križno provjereno):** svaki modul spomenut u §5 (kod),
> §12 (tenant_config), §11.3 S10 (mcp/server) i §17 (guarantee mehanizmi) je u
> ovom stablu — stablo ⊇ spec. Faze: F0=odmah, F1=uz /actions, F-M365=uz Copilot.

---

## 3. Action registry — glavni ugovor (`config/actions.json`)

Ovo je **jedini** dio koji AI "vidi" i koji se mijenja **bez izmjene koda**.
Svaka akcija ima 3 bloka: `ai` (za LLM), `execution` (kamo puca), `policy` (gateovi).

### 3.1 Schema (formalno)

```jsonc
{
  "name": "string (snake_case, ≤64)",         // ime akcije = OpenAI tool name
  "ai": {
    "description": "kada koristiti (HR/EN)",   // LLM po ovome bira akciju
    "use_when":   ["primjeri fraza korisnika"],
    "parameters": {                            // SAMO poslovni parametri koje AI puni
      "<param>": {
        "type": "string|integer|number|boolean|date|datetime",
        "format": "date|date-time|''",         // tehnički opis
        "description": "što znači u kontekstu", // poslovni opis (OBAVEZNO)
        "examples": ["…"],                      // jako podiže točnost ekstrakcije
        "required": true,
        "codebook": "CaseType|null"            // ako je šifrarnik: kako ga razriješiti (§7.4)
      }
    }
  },
  "execution": {
    "method": "POST|GET",
    "action": "/actions/<name>"                // Business API ruta
  },
  "policy": {
    "mutation": true,                          // true → Da/Ne confirm gate
    "inject": ["person_id","tenant_id"]        // bot/backend puni iz identiteta (AI ne vidi)
  }
}
```

### 3.2 Puni primjer — `report_incident` (WRITE)

```json
{
  "name": "report_incident",
  "ai": {
    "description": "Prijava kvara, štete, nezgode ili tehničkog problema na vozilu.",
    "use_when": ["pukla mi je guma", "auto ne pali", "imao sam nezgodu", "prijavljujem kvar"],
    "parameters": {
      "registration_plate": {
        "type": "string", "required": true,
        "description": "Registracijska oznaka vozila na kojem je problem.",
        "examples": ["ZG-1234-AB", "ZG1234AB", "moja rega"]
      },
      "description": {
        "type": "string", "required": true,
        "description": "Opis kvara/štete korisnikovim riječima.",
        "examples": ["pukla guma prednja lijeva", "ogrebotina na vratima"]
      },
      "incident_type": {
        "type": "string", "required": false, "codebook": "CaseType",
        "description": "Vrsta problema; ako korisnik ne kaže jasno, backend ostavlja default.",
        "examples": ["kvar", "šteta", "nezgoda"]
      }
    }
  },
  "execution": { "method": "POST", "action": "/actions/report-incident" },
  "policy": { "mutation": true, "inject": ["person_id", "tenant_id"] }
}
```

### 3.3 Puni primjer — `book_vehicle` (WRITE, s periodom)

```json
{
  "name": "book_vehicle",
  "ai": {
    "description": "Rezervacija službenog vozila za određeni period.",
    "use_when": ["rezerviraj auto", "trebam vozilo sutra", "bookiraj kombi za petak"],
    "parameters": {
      "date_from": { "type": "datetime", "format": "date-time", "required": true,
        "description": "Početak rezervacije.", "examples": ["2026-06-12T09:00:00", "sutra 9h"] },
      "date_to":   { "type": "datetime", "format": "date-time", "required": true,
        "description": "Kraj rezervacije.", "examples": ["2026-06-12T15:00:00", "do 15h"] },
      "vehicle_hint": { "type": "string", "required": false,
        "description": "Ako korisnik traži konkretno vozilo (marka/registracija). Prazno = backend nudi slobodno.",
        "examples": ["kombi", "ZG-1234-AB"] }
    }
  },
  "execution": { "method": "POST", "action": "/actions/book-vehicle" },
  "policy": { "mutation": true, "inject": ["person_id", "tenant_id"] }
}
```

> **Granica AI ↔ backend (ključno):** `ai.parameters` sadrži SAMO ono što korisnik
> izgovori. Interno (`AssigneeType:1`, `EntryType:0`, tenant defaulti, computed
> polja) **nikad nije ovdje** — to backend dodaje. Šifrarnici (`incident_type`)
> nose `codebook` marker i razrješuju se po §10.2.

### 3.3 Operativna semantika `actions.json` (boot, reload, versioning, flag)

| Aspekt | Pravilo |
|---|---|
| **Boot validacija** | fail-fast na startu (isti pattern kao tool_data.json danas: malformiran file / prazna `actions` / akcija bez `ai.description` → `RuntimeError`, pod se ne digne — bolje glasno nego tiho krivo) |
| **Reload bez deploya** | `POST /admin/cache-invalidate` (POSTOJEĆI endpoint pattern u repou) → `ActionRegistry.load()` ponovno; izmjena opisa/parametra akcije = edit JSON + 1 HTTP poziv |
| **Versioning** | akcije su ADITIVNE: novi parametar = uvijek `required:false`; breaking promjena = NOVO ime (`book_vehicle_v2`) dok staro ne istekne — za k8s rollouta žive stari+novi pod istovremeno i oba moraju raditi |
| **Feature flag** | `V2_USE_ACTIONS=1` uključuje action-mode PORED postojećeg routera (Strangler Fig — PLAN_KONVERGENCIJA); `=0` je kill-switch natrag na staro |
| **Hosting akcija** | `BUSINESS_API_URL` env (default = `MOBILITY_API_URL`) — gdje `/actions/*` živi je CONFIG, ne pretpostavka; odvojeni host = 1 env var (§5.3) |

---

## 4. Request lifecycle (detaljni flow)

Poruka *"pukla mi je guma na ZG-1234-AB"* → odgovor:

```
webhook_simple.py
  HMAC verify → stream_data{sender,text,message_id,tenant_id,channel} → XADD
        │
worker.py  ── XREADGROUP ── V2Engine.process_message(phone, text, channel)
        │
engine._dispatch_message:
  ├─ rate_limiter.check ──────────────── blok? → cooldown poruka
  ├─ pii_scrubber.scrub ──────────────── OIB/IBAN → [REDACTED]
  ├─ input_sanitizer.sanitize ────────── prompt-injection? → blok
  ├─ identity.resolve(phone) ─────────── person_id, tenant_id, own vehicle
  ├─ crisis_detector.detect ──────────── suicid? → hotline (terminal)
  ├─ special_intents.detect ──────────── GDPR/welcome? → terminal
  ├─ PENDING continuation? ───────────── mid-param/confirm? → nastavi state
  │
  ├─ llm_router.route(text, actions_schema, history[-3:])
  │      └── vrati: { kind: "action"|"clarify"|"answer", action?, params?, text? }
  │
  ├─ if kind=="answer"  → llm_formatter → reply           (npr. RAG/policy, §7.5)
  ├─ if kind=="clarify" → pošalji pitanje                 (dvosmisleno)
  └─ if kind=="action":
        ├─ action_validator.check(action, params)  ⟵ NOVO  (§5.2)
        │     fail? → clarify / re-ask
        ├─ type_resolver za `codebook` params      (§7.4)
        ├─ pending_params: fali required? → pitaj + zapamti (§7.3)
        ├─ inject identity (person_id, tenant_id)
        ├─ if policy.mutation → mutation_gate → "Da/Ne?"  (§7.2)
        │        (čeka potvrdu; state u pending_mutation)
        └─ executor.execute(action, params, identity)     (§5.3)
                 └── api_gateway → POST /actions/report-incident
                          → Business API orkestrira → vrati JSON
        │
  └─ llm_formatter.format(json) → hrvatski → enqueue_outbound(channel)
        │
worker.py outbound → grananje po channel → WhatsApp/Viber send
```

---

## 5. Ključni moduli s primjerima koda

> Kod je **ciljna implementacija** usklađena s postojećim async patternima
> (`api_gateway.call`, `TokenManager.get_token`, dataclass stil). Ne sync `requests`.

### 5.1 `action_registry.py` — učitaj akcije + daj LLM tool schema (⟵ NOVO)

```python
# services/v2/action_registry.py
from __future__ import annotations
import json
from dataclasses import dataclass
from pathlib import Path

_TYPE_MAP = {"date": "string", "datetime": "string"}  # JSON-schema tipovi za LLM

@dataclass
class ActionRegistry:
    actions: dict            # name -> action dict (iz actions.json)

    @classmethod
    def load(cls, path: Path) -> "ActionRegistry":
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(actions={a["name"]: a for a in data["actions"]})

    def openai_tools(self) -> list[dict]:
        """Pretvori ~30 akcija u OpenAI tools=[] schema. Bez Stage A retrievala —
        30 akcija stane direktno u prompt."""
        out = []
        for name, a in self.actions.items():
            props, required = {}, []
            for pname, p in a["ai"]["parameters"].items():
                props[pname] = {
                    "type": _TYPE_MAP.get(p["type"], p["type"]),
                    "description": p["description"]
                    + (f" Primjeri: {', '.join(p['examples'])}." if p.get("examples") else ""),
                }
                if p.get("required"):
                    required.append(pname)
            out.append({"type": "function", "function": {
                "name": name,
                "description": a["ai"]["description"]
                + "\nUse when: " + "; ".join(a["ai"].get("use_when", [])),
                "parameters": {"type": "object", "properties": props, "required": required},
            }})
        return out
```

### 5.2 `action_validator.py` — validacija AI outputa PRIJE izvršenja (⟵ NOVO)

```python
# services/v2/action_validator.py
from dataclasses import dataclass

@dataclass
class ValidationResult:
    ok: bool
    error: str = ""
    missing_required: list[str] = None

def validate(action_name: str, params: dict, registry) -> ValidationResult:
    # 1) anti-halucinacija: akcija mora postojati
    spec = registry.actions.get(action_name)
    if spec is None:
        return ValidationResult(False, error=f"unknown_action:{action_name}")
    schema = spec["ai"]["parameters"]
    # 2) nepoznati parametri (LLM izmislio polje)
    for p in params:
        if p not in schema:
            return ValidationResult(False, error=f"unknown_param:{p}")
    # 3) tip/format (grubo; coercion je već u _coerce_llm_params)
    for pname, pdef in schema.items():
        if pname in params and params[pname] is not None:
            if pdef["type"] in ("integer",) and not _is_int(params[pname]):
                return ValidationResult(False, error=f"bad_type:{pname}")
    # 4) required koji fale → NIJE greška: ide u pending_params (pita korisnika)
    missing = [n for n, d in schema.items()
               if d.get("required") and not params.get(n)]
    return ValidationResult(True, missing_required=missing)
```

### 5.3 `executor.py` — grana prema `/actions/*` (⟵ MIJENJA, ostaje async)

```python
# services/v2/executor.py  (izvod ciljne grane)
async def execute_action(self, action_name: str, params: dict, identity: dict):
    spec = self._registry.actions[action_name]
    # inject iz identiteta (AI ovo NE vidi — policy.inject)
    body = dict(params)
    for key in spec["policy"].get("inject", []):
        if identity.get(key) is not None:
            body[key] = identity[key]
    # async poziv preko postojećeg gatewaya (token + x-tenant + Idempotency-Key + SSRF)
    # HOSTING: gdje Business API živi NIJE pretpostavka nego config —
    # BUSINESS_API_URL env (default = MOBILITY_API_URL ako je isti host).
    # Ako Damir hosta /actions odvojeno, mijenja se 1 env var, ne kod.
    resp = await self._gateway.call(
        method=spec["execution"]["method"],
        service="",                                   # path je već pun ("/actions/…")
        path=spec["execution"]["action"],            # "/actions/report-incident"
        body=body if spec["execution"]["method"] == "POST" else None,
        tenant_id=identity["tenant_id"],
        base_url_override=settings.BUSINESS_API_URL,  # ⟵ NOVO (fallback: MOBILITY_API_URL)
    )
    return resp   # {success, data|error_body, status_code}
```

### 5.4 Šifrarnik resolver (§7.4) — reuse `type_resolver.py` (postoji)

```python
# već postoji: services/v2/type_resolver.py
#   rows = await executor.get_codebook(tenant, "CaseType")   # GET /…Types
#   pairs = type_resolver.rows_to_pairs(rows)                # [(id, "Kvar"), (id,"Šteta")]
#   code, _ = type_resolver.match(user_text, pairs)          # "pukla guma" → id kvara
# U /actions svijetu PREFERIRAMO da ovo radi Business API (opcija C, §7.4).
```

### 5.5 Viber adapter (⟵ NOVO, edge — mozak netaknut)

> **⚠ OPS PREREQUISITE (bez ovoga kanal NE RADI bez obzira na kod):**
> Viber business sender mora biti **registriran i odobren kod Infobipa**
> (poslovni proces, tipično dani-tjedni!) + Viber sender u secretima. Pokreni
> registraciju ODMAH, paralelno s razvojem — approval je kritični put Viber
> kanala, ne kod. (Isti princip vrijedi za budući WhatsApp broj novog tenanta.)

```python
# webhook_simple.py  ── novi inbound
@router.post("/viber")
async def viber_inbound(request: Request):
    raw = await request.body()
    if not verify_infobip_signature(raw, request.headers.get("X-Signature","")):
        raise HTTPException(401)
    p = json.loads(raw)
    redis = await get_redis()
    await redis.xadd("whatsapp_stream_inbound", {
        "sender": p["sender"], "text": p["message"]["text"],
        "message_id": p["messageId"],
        "tenant_id": await resolve_tenant_for_phone(p["sender"]) or "",
        "channel": "viber",                          # ⟵ ključni tag
    }, maxlen=100_000, approximate=True)
    return {"status": "queued"}

# worker.py  ── outbound grananje
async def _dispatch_outbound(self, channel: str, to: str, text: str, idem: str):
    if channel == "viber":
        await self._infobip.send_viber(to=to, text=text, idempotency_key=idem)
    else:
        await self._send_whatsapp(to=to, text=text, idempotency_key=idem)
```

---

## 6. Data contracts (JSON na svakoj granici)

```
① LLM → bot   (router output)
{ "kind":"action", "action":"report_incident",
  "params": { "registration_plate":"ZG-1234-AB", "description":"pukla guma" } }

② bot → Business API   (nakon validacije, inject, confirm)
POST /actions/report-incident      Headers: Authorization, x-tenant, Idempotency-Key
{ "registration_plate":"ZG-1234-AB", "description":"pukla guma",
  "person_id":"p-77", "tenant_id":"t-A" }        // interni kodovi NISU ovdje

③ Business API → bot   (rezultat, čisti sažetak)
{ "status":"success", "incident_id":"INC-5521", "vehicle_status":"blocked" }

④ bot → korisnik   (formatter → hrvatski)
"Prijavio sam kvar na vozilu ZG-1234-AB (guma). Broj prijave INC-5521,
 vozilo je blokirano do servisa."
```

---

## 7. Flows po tipu sposobnosti (dijagrami)

### 7.1 READ akcija (npr. `list_trips` — "moja zadnja putovanja")
```
LLM → {action:list_trips, params:{limit:10}}
  → validator OK → (nema mutation) → executor GET /actions/list-trips
  → Business API vrati listu → formatter → "Zadnjih 5 putovanja: …"
```

### 7.2 WRITE akcija s potvrdom (`report_incident`)
```
LLM → {action:report_incident, params:{plate, description}}
  → validator OK → inject person_id/tenant_id
  → mutation_gate: "Prijavit ću kvar na ZG-1234-AB (guma). Potvrđuješ? (Da/Ne)"
       │ user "Da"  → executor POST /actions/report-incident → success → formatter
       │ user "Ne"  → odustani, clear pending
       └ TTL/izmjena identiteta → re-ask (postojeći stale-confirm guard)
```

### 7.3 Missing-param loop (fali `date_to` u `book_vehicle`)
```
LLM → {action:book_vehicle, params:{date_from:"sutra 9h"}}   // date_to fali
  → validator: missing_required=[date_to]
  → pending_params.save(...) → "Do kada trebaš vozilo?"
       (korisnikov sljedeći turn: "do 15h")
  → parse → date_to=...T15:00 → sve required puna → nastavi (confirm → execute)
```

### 7.4 Šifrarnik / codebook (`incident_type` → tenant `CaseType`)
```
OPCIJA C (preporuka):  AI šalje semantiku ("kvar"); Business API mapira → CaseType po tenantu.
OPCIJA B (fallback u botu, ako backend ne mapira):
   type_resolver: GET /CaseTypes?tenant=A → [(3,"Kvar"),(5,"Šteta")]
     → match("kvar") → 3 → pošalji incident_type_id=3
   ako nema jednoznačnog matcha → clarify: "Je li to kvar, šteta ili nezgoda?"
```

### 7.5 RAG / knowledge answer (Faza 2 — "smijem li autom na godišnji?")
```
LLM router prepozna "policy pitanje" → kind:"answer" (ne akcija)
  → knowledge/rag_retriever: embed(upit) → top-k chunkova iz tenant "car_policy.pdf"
  → llm_formatter(upit + chunkovi, "odgovori SAMO iz dokumenata") → HR odgovor + izvor
```

---

## 8. Business API strana (što Damirov tim gradi) — contract

Bot šalje **jedan čisti poziv**; sva težina je ovdje. Primjer (FastAPI skica):

```python
# ŽIVI NA MOBILITYONE SERVERU (Damir)
@app.post("/actions/report-incident")
async def report_incident(body: IncidentIn, x_tenant: str = Header(...)):
    # 1) resolve identifikatora (registracija → VehicleId)
    vehicle = await domain.get_vehicle_by_plate(body.registration_plate, x_tenant)
    if not vehicle:
        return JSONResponse(400, {"error_code": "VEHICLE_NOT_FOUND",
                                  "message": "Vozilo s tom registracijom ne postoji."})
    # 2) šifrarnik: semantika → tenant kod
    case_type = await codebook.map("CaseType", body.incident_type or "kvar", x_tenant)
    # 3) orkestracija granularnih poziva (redoslijed + pravila)
    inc = await domain.create_incident(vehicle_id=vehicle.id, case_type=case_type,
                                        description=body.description, status="open")
    await domain.block_calendar(vehicle_id=vehicle.id, reason=body.description)
    # 4) čisti sažetak natrag botu
    return {"status": "success", "incident_id": inc.id, "vehicle_status": "blocked"}
```

**Ugovor koji tražimo od backenda PO AKCIJI (kompletan — ovo je checklist za
Damira prije nego počne graditi):**

| # | Zahtjev | Zašto |
|---|---|---|
| 1 | ruta + input DTO (čista poslovna polja) | §3 granica AI↔backend |
| 2 | strukturiran error `{error_code, field?, message}` za SVE 4xx | HR prijevod korisniku (§13.2 ⑧) |
| 3 | tko mapira šifrarnike (preporuka: backend — §10.2 opcija C) | per-tenant kodovi |
| 4 | **honoriranje `Idempotency-Key`** headera (dedup prozor ≥ 10 min) | bot ga VEĆ šalje na svaku mutaciju; bez backend dedupa mrežni timeout+retry = dupla rezervacija |
| 5 | **READ liste vraćaju `{items, total}`** + max page size | bez `total` bot ne može reći "imaš 27, prikazujem 10" (S1 primjer to zahtijeva) |
| 6 | objavljeni **rate-limiti** (429 + `Retry-After` header) | botov backoff je danas kalibriran naslijepo |
| 7 | **OAuth scope grant za SVE `/actions/*` rute našem client_id-u** — pisana potvrda PRIJE prvog deploya | ⚠ DOKAZANO ŽIVO da je ovo failure mode: test 2026-05-30 — glavne blokade bile **403 scope ("bot nema ovlasti")** na granularnim rutama (DAMIR_ACCURACY_UGOVOR:27). Bez granta: SVAKI poziv nove akcije = 403 = mrtav bot |
| 8 | **timezone semantika datetime polja**: dokumentirati tretman naive ISO (preporuka: backend tretira kao Europe/Zagreb, ILI ugovoriti offset format `2026-06-12T09:00:00+02:00`) | bez toga rezervacije mogu biti pomaknute 1-2h — radi-ali-krivo (tiha korupcija) |

*(Zahtjevi 4/5/6/8 namjerno preklapaju M1_ZAHTJEV_ADDENDUM #3/#1-2/#6/#4 —
addendum je pitanje za DANAŠNJI granularni API, ovo je UGOVOR za novi /actions
sloj. Zahtjev 7 je NOV — naučen iz živog testa.)*

---

## 9. Otvorene odluke (sažetak — puni deep-dive u §10)

| # | Odluka | Preporuka |
|---|---|---|
| 1 | Granica AI-puni vs backend-puni parametre | `ai.parameters` = samo poslovni; interno u backendu |
| 2 | Gdje šifrarnici (`CaseType` per-tenant) | Business API mapira semantiku (opcija C); `type_resolver` fallback |
| 3 | Registracija → VehicleId | backend resolve (World A) ili bot pre-resolve (rana validacija) |
| 4 | Tko gradi `/actions` | **pitanje za Damira** — World A (backend) vs World B (bot BFF) |
| 5 | RAG/knowledge | Faza 2, zasebna sposobnost `answer_from_policy` |
| 6 | Auth MCP servera (tko smije zvati naše akcije iz M365) | Entra ID token validacija — dizajn u F-M365 fazi (S10) |

---

## 10. Detaljne odluke (deep-dive)

Svaka odluka iz tablice §9 razrađena: **što je izazov → opcije (s kodom/dijagramom)
→ preporuka → posljedica za implementaciju.**

### 10.1 Granica: koji parametar puni AI, a koji backend

**Izazov:** akcija ima 3 vrste parametara. Ako AI pokuša puniti krivu vrstu →
halucinacija (izmisli `CaseType:2`) ili sigurnosni problem (sam postavi `TenantId`).

```
                       ┌─────────────────────────────────────────────┐
 "pukla mi je guma     │  AI PUNI (iz teksta)                         │
  na ZG-1234-AB"  ────▶│    registration_plate = "ZG-1234-AB"         │
                       │    description        = "pukla guma"         │
                       ├─────────────────────────────────────────────┤
                       │  BOT/BACKEND INJECT (iz identiteta)          │
   identity.resolve ──▶│    person_id  = "p-77"   (nikad iz teksta!)  │
                       │    tenant_id  = "t-A"                        │
                       ├─────────────────────────────────────────────┤
                       │  BACKEND DODAJE (interno mapiranje)          │
   Business API    ───▶│    CaseType   = 3        (šifrarnik, §10.2)  │
                       │    Status     = "open"   (default)          │
                       │    EntryType  = 0        (computed)          │
                       └─────────────────────────────────────────────┘
```

**Pravilo za `actions.json`:** u `ai.parameters` ide **isključivo prva skupina**
(poslovni, korisnik ih izgovori). Druga skupina ide u `policy.inject`. Treća nije
nigdje u botu — backend je dodaje.

**Test za razvrstavanje novog parametra** (kad dizajniraš akciju):
| Pitanje | Da → |
|---|---|
| Korisnik ovo izgovori u poruci? | `ai.parameters` (AI puni) |
| Dolazi iz "tko je korisnik" (person/tenant/vlastito vozilo)? | `policy.inject` |
| Interni kod / default / računato polje? | backend (ne spominji u botu) |

> **Rubni slučaj:** `incident_type` je *poslovni izbor* (korisnik ga može reći)
> ALI je i *šifrarnik* (kodiran po tenantu). Zato je u `ai.parameters` **s
> markerom** `"codebook":"CaseType"` — AI izvuče semantiku ("kvar"), a razrješenje
> u broj ide po §10.2.

---

### 10.2 Šifrarnici (`CaseType` različit po tenantu) — 3 opcije s kodom

**Izazov:** `CaseType` je kodiran (1/2/3), tenant-specifičan, živi u backendu. AI
mora "pukla guma" pretvoriti u točan broj za TOG tenanta.

```
Tenant A:  1=Kvar   2=Šteta   3=Nezgoda
Tenant B:  1=Nezgoda 2=Kvar   4=Vandalizam        ← isti tekst, drugi broj!
```

**Opcija A — statični enum u schemi (❌ NE):**
```json
"incident_type": { "enum": [1, 2, 3] }   // puca: tenant B ima druge brojeve
```

**Opcija B — bot razrješuje (reuse `type_resolver.py`, već postoji):**
```python
# runtime, u botu, prije slanja akcije
rows = await executor.get_codebook(tenant_id, "CaseType")   # GET /CaseTypes
pairs = type_resolver.rows_to_pairs(rows)                    # [(3,"Kvar"),(2,"Šteta")]
code, _ = type_resolver.match(user_says="kvar", pairs)      # → 3
if code is None:                                            # nema jednoznačnog matcha
    return clarify("Je li to kvar, šteta ili nezgoda?")     # pitaj korisnika
params["incident_type_id"] = code
```
```
FLOW (opcija B):
 AI: incident_type="kvar" ──▶ bot: GET /CaseTypes?tenant=A ──▶ match "kvar"→3
        └ jednoznačno? → pošalji 3    └ dvosmisleno? → clarify pitanje korisniku
```

**Opcija C — backend razrješuje (✅ PREPORUKA za World A):**
```
 AI: incident_type="kvar"  ──POST /actions/report-incident {incident_type:"kvar"}──▶
     Business API: codebook.map("CaseType","kvar",tenant) → 3   (backend zna svoj šifrarnik)
```
AI/bot **nikad ne dira brojeve**. Backend, koji posjeduje šifrarnik, mapira semantiku.

| | A statični | B bot-resolve | C backend-resolve |
|---|---|---|---|
| Radi per-tenant? | ❌ | ✅ | ✅ |
| Tko zna kodove | nitko (hardkodirano) | bot (dohvaća) | backend (posjeduje) |
| Extra HTTP poziv | — | da (dohvat šifrarnika) | ne |
| Preporuka | nikad | fallback / World B | **default / World A** |

---

### 10.3 Registracija → VehicleId ("AI nikad ne tipka UUID")

**Izazov:** AI prepozna `"ZG-1234-AB"` (ljudski), ali akcija/baza rade s
`VehicleId` (UUID). Netko mora razriješiti tablicu → UUID.

```
 AI: registration_plate="ZG-1234-AB"
        │
        ├── World A:  pošalji tablicu → backend razriješi (GET /Vehicles?plate=…)
        │             (kao report_incident §8 — jedno mjesto, čisto)
        │
        └── World B:  bot pre-resolve  ──▶  GET /Vehicles?Filter=LicencePlate(=)ZG-1234-AB
                       nađe VehicleId → pošalji UUID
                       BONUS: rana validacija — "to vozilo ne postoji" PRIJE akcije
```

**Preporuka:** World A → backend resolve (čišće). World B ili kad želiš rano
javiti grešku → bot pre-resolve (imaš uzorak u `probe_filter.py` + `identity.py`
koji već radi lookup za vozačevo vlastito vozilo).

---

### 10.4 ⭐ Tko gradi `/actions`: World A vs World B (glavno pitanje za Damira)

**Ovo je jedna odluka koja mijenja tko radi 80% posla.** Pitanje je samo:
**GDJE živi orkestracija** (GET vehicle → POST incident `CaseType:3` → PUT calendar)?

#### World A — Business API na MobilityOne serveru (Damirov tim gradi)
```
   BOT (tanak)                              MOBILITYONE SERVER
   report_incident{plate, desc}
        │  POST /actions/report-incident
        └──────────────────────────────────▶  ┌── Business API /actions ──┐
                                               │  GET  /Vehicles           │
                                               │  POST /Incidents CaseType:3│  ← orkestracija
                                               │  PUT  /Calendar block      │    ŽIVI OVDJE
        ◀─── {success, incident_id} ──────────  └───────────────────────────┘
   (bot NE zna redoslijed ni kodove)            (Damir gradi i održava)
```
- **Bot:** samo prepozna akciju + izvuče poslovne parametre. Tanak.
- **Logika:** na backendu, na JEDNOM mjestu → i web QB i Copilot je dijele.
- **Trošak:** ovisi o Damirovom timelineu; bot je "blokiran" dok backend ne izloži akciju.

#### World B — Bot-side BFF adapter (Filip gradi, poopćen `flow_engine.py`)
```
   BOT (deblji — sadrži orkestraciju)              MOBILITYONE
   report_incident{plate, desc}
        │  (lokalna funkcija = poopćen flow_engine)
        ├─ GET  /Vehicles            ──┐
        ├─ POST /Incidents CaseType:3 ─┼──────────▶  DOMAIN API (950 granularnih)
        └─ PUT  /Calendar block      ──┘
   (bot ZNA redoslijed + kodove + filtere)          ( /actions NE postoji na serveru )
```
- **Bot:** sam orkestrira granularne pozive (kao `flow_engine.py` danas za 3 flowa, samo za ~30).
- **Logika:** u botu → web QB je ne može reuse-ati (krši "UI i AI dijele API").
- **Trošak:** brzo, neovisno o Damiru — ALI vraća "debeli bot" i moraš znati
  params/order/šifrarnike (točno ono što si u komentarima flagao kao teško + blokiran si na Swagger metapodacima).

#### Usporedba
| | **World A** (backend) | **World B** (bot BFF) |
|---|---|---|
| Tko gradi `/actions` | Damirov tim | ti (Filip) |
| Gdje živi orkestracija | MobilityOne server | u botu |
| Bot | tanak | debeo |
| Reuse (web QB, Copilot) | ✅ dijele isti `/actions` | ❌ logika zaključana u botu |
| Brzina početka | ovisi o Damiru | odmah, neovisno |
| Tko zna kodove/redoslijed/filtere | backend | ti (blokiran na Swaggeru) |
| Damirov princip "bez duplikacije" | ✅ poštuje | ❌ krši |
| Glavni rizik | Damirov timeline | tech-debt u botu |

#### Preporuka (hibrid — sigurno + brzo)
```
 Faza 1:  World B za prvih 2-3 akcije   → dokažeš vrijednost ODMAH (imaš flow_engine kao predložak),
                                          ne čekaš nikoga
 Faza 2+: migriraj na World A kako Damir isporučuje /actions
                                          → bot se stanjuje, logika seli na backend
```
To je i Strangler Fig (iz `PLAN_KONVERGENCIJA_10_OD_10`): počneš s onim što imaš,
zamijeniš komad-po-komad. **Na sastanku pitaj Damira: hoće li i kada graditi
`/actions` na backendu (World A). Ako da → čekaš i tanjiš bot. Ako ne/kasnije →
World B stopgap, s planom migracije na A.**

---

### 10.5 RAG / knowledge (Faza 2) — zasebna sposobnost, ne akcija

**Izazov:** korisnik pita iz **dokumenta** ("smijem li službenim autom na godišnji?"
→ car_policy.pdf), ne traži akciju. To nije `/actions/*` nego read-only Q&A.

```
 "smijem li autom na godišnji?"
        │
   llm_router: kind="answer" (policy pitanje, ne akcija)
        │
   knowledge/rag_retriever.py:
        embed(upit) → cosine nad chunkovima tenantovog "car_policy.pdf"
        → top-3 relevantna odlomka
        │
   llm_formatter(upit + odlomci, system="odgovori SAMO iz priloženih dokumenata,
                 citiraj izvor, ne izmišljaj") → HR odgovor + "(izvor: Pravilnik, čl. 7)"
```

**Storage (per-tenant):**
```
config/tenants/<tenant>/knowledge/
    car_policy.pdf · putni_nalozi.md · ...        ← korisnik uploada
.cache/knowledge/<tenant>/embeddings.json         ← indeks (rebuild na upload)
```
**Router grananje:** dodaš u `actions.json` "meta-akciju" `answer_from_policy`
(bez `execution.action`; umjesto `/actions/*` → RAG put). Ista kontrolna petlja
(§4), samo je izvršni sloj RAG umjesto Business API. Scaffolding: `rag_scheduler.py` (postoji).


---

## 11. TOČAN FLOW — master tok + svi scenariji

### 11.1 Master tok (numerirano, svaki korak = modul + state + error putanja)

```
━━ ULAZ ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 1. Infobip POST /webhook/{whatsapp|viber}        [webhook_simple.py]
      HMAC verify (fail→401, fail-closed) → parse payload (multi-format)
 2. Dedup:  SET wh_dedup:{message_id} NX EX 60    (duplikat→skip, 200)
 3. XADD whatsapp_stream_inbound {sender,text,message_id,tenant_id,channel}
      retry ×3 backoff; totalni fail → file-DLQ; uvijek vrati 200 Infobipu
━━ WORKER ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 4. XREADGROUP (grupa "workers")                  [worker.py]
 5. Idempotency: SET msg_lock:{sender}:{msg_id} NX EX 300 (dup→ACK+skip)
 6. Per-sender in-process lock (redoslijed poruka istog korisnika)
 7. engine.process_message(sender, text, channel) — budžet 90s
      timeout → "Obrada je trajala predugo…" + ACK
━━ ENGINE: SIGURNOSNI KREVET ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 8. rate_limiter    v2:rl:m|h:{phone} (60s/3600s bucket) → blok→cooldown msg
 9. pii_scrubber    OIB/IBAN/tel → [REDACTED] (PRIJE ijednog LLM poziva)
10. input_sanitizer prompt-injection → blok poruka
11. identity.resolve(phone)   cache v2:identity:{phone} TTL 30s
      → person_id, tenant_id, vlastito vozilo; nepoznat broj → enrollment msg (kraj)
12. crisis_detector  suicid signal → hotline poruka (terminal)
13. special_intents  GDPR delete/export (+audit zapis) / welcome / handover (terminal)
━━ ENGINE: NASTAVCI STANJA (prije svježeg routinga!) ━━━━━━━━━━━━━━━━━━━━━━━
14. pending_params?    v2_pending_params:{phone} 300s → poruka = odgovor na pitanje
15. pending_mutation?  v2:pending_mut:{phone} 300s   → poruka = Da/Ne na confirm
16. pending_clarify?   v2_pending_clarify:{phone} 300s → poruka = izbor 1/2/3
━━ ENGINE: ODLUKA (LLM) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
17. llm_router.route(text, actions_schema(~30), history[-3:], identity_summary)
      → { kind: "action"|"clarify"|"answer", action?, params?, text? }
      Azure retry ×3 backoff na 429/5xx; totalni fail → siguran fallback msg
18. kind=answer  → (RAG put §7.5 ili direktan odgovor) → korak 27
    kind=clarify → pošalji pitanje korisniku (kraj turna)
    kind=action  → nastavi ↓
━━ ENGINE: VALIDACIJA + PRIPREMA PARAMETARA ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
19. action_validator: akcija postoji? nepoznata polja? tipovi?  (§5.2)
      fail → clarify korisniku (nikad slijepo dalje)
20. coercion: HR datumi/brojevi → ISO/int (param_ui.parse + _coerce_llm_params)
21. missing required? → pending_params.save + HR pitanje (kraj turna;
      sljedeća poruka ulazi na koraku 14 i NASTAVLJA odavde)
22. codebook params (marker "codebook"): opcija C default (šalji semantiku),
      opcija B fallback (type_resolver: dohvat šifrarnika→match; dvosmisleno→clarify)
23. policy.inject: person_id/tenant_id iz identiteta (AI ih nikad ne generira)
━━ ENGINE: POTVRDA + IZVRŠENJE ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
24. policy.mutation=true → mutation_gate:
      param echo ("Provjeri prije slanja: …") + "Potvrđuješ? (Da/Ne)"
      → pending_mut.save 300s (kraj turna; "Da" ulazi na koraku 15)
      pri "Da": exec lock v2:pending_mut_exec 30s (anti dupli-klik/Infobip retry)
      + stale-confirm guard (>90s re-ask; >30s re-validate identity)
25. executor.execute → api_gateway:
      OAuth token (cache mobility:access_token) + x-tenant + Idempotency-Key
      + SSRF guard → POST /actions/<name>  (budžet 15s, circuit breaker po servisu)
26. Rezultat:  2xx → dalje │ 4xx → api_error_translator → HR objašnjenje
      │ 5xx/timeout → generička HR + pending OSTAJE (korisnik može ponoviti "Da")
━━ IZLAZ ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
27. llm_formatter: JSON → hrvatski (grounding: samo podaci iz JSON-a; izlazni
      PII scrub; liste s točnim ukupnim brojem)
28. conv_history.append (PII-scrubbed, zadnjih 5, TTL 30min)
29. enqueue_outbound {to, text, channel, idempotency_key}
30. worker outbound loop: BLMOVE → send po channel tagu (WA/Viber)
      fail: permanentna→DLQ │ tranzijentna→delayed retry ×3 backoff→DLQ
      uspjeh: SET sent:{idem_key} EX 600 (crash-recovery dedup) → LREM processing
```

### 11.2 Tablica svih stanja (Redis) — tko, što, koliko

| Ključ | Vlasnik (korak) | TTL | Svrha |
|---|---|---|---|
| `wh_dedup:{message_id}` | webhook (2) | 60s | Infobip retry dedup |
| `whatsapp_stream_inbound` | webhook→worker (3-4) | maxlen 100k | ulazni red (AOF persist) |
| `msg_lock:{sender}:{msg_id}` | worker (5) | 300s | idempotencija obrade |
| `v2:rl:m|h:{phone}` | engine (8) | 60s/3600s | rate limit |
| `v2:identity:{phone}` | engine (11) | 30s | identitet cache |
| `tenant_phone:{e164}` | identity | 300s | phone→tenant cache |
| `v2_pending_params:{phone}` | engine (14/21) | 300s | multi-turn prikupljanje parametara |
| `v2:pending_mut:{phone}` | engine (15/24) | 300s | čekanje Da/Ne |
| `v2:pending_mut_exec:{phone}` | engine (24) | 30s | anti-replay pri "Da" |
| `v2_pending_clarify:{phone}` | engine (16) | 300s | izbor 1/2/3 + "nije točno" reoffer |
| `v2_conv_history:{phone}` | engine (28) | 30min | zadnjih 5 turnova (PII-scrubbed) |
| `tenant_cfg:{tenant_id}` ⟵ NOVO | §12 | 300s | tenant config cache (DB je istina) |
| `mobility:access_token` | gateway (25) | ~expiry | OAuth cache |
| `api_err_translate:{hash}` | translator (26) | 3600s | 4xx prijevod cache |
| `whatsapp_outbound` (+`_processing`, `_delayed`, `dlq:*`) | worker (29-30) | — | izlazni red + retry + DLQ |
| `sent:{idempotency_key}` | worker (30) | 600s | outbound dedup nakon crasha |

### 11.3 Scenariji (sequence dijagrami)

**S1 — READ happy-path** (`"pokaži moja zadnja putovanja"` → `list_trips`)
```
korisnik→WA→webhook(1-3)→worker(4-7)→safety(8-13: prolaz)→router(17)
  →{action:list_trips, params:{limit:10}}→validator OK(19)→nema mutation
  →executor(25) GET /actions/list-trips→{trips:[…], total:27}
  →formatter(27) "Zadnjih 10 od ukupno 27 putovanja: …"→outbound(29-30)→korisnik
                                                    (1 LLM decision + 1 LLM format)
```

**S2 — WRITE + Da/Ne** (`"prijavljujem kvar na ZG-1234-AB, pukla guma"`)
```
…safety→router→{action:report_incident, params:{plate,desc}}→validator OK
  →inject person/tenant(23)→mutation_gate(24):
     bot: "Prijavit ću kvar na ZG-1234-AB: 'pukla guma'. Potvrđuješ? (Da/Ne)"
     [pending_mut 300s]                                        ── kraj turna 1
korisnik: "Da" → worker → engine korak 15 (nastavak stanja):
  parse_reply("Da")=execute → exec lock 30s → executor POST /actions/report-incident
  → {status:success, incident_id:INC-5521} → clear pending → invalidate identity
  → formatter: "Prijavljen kvar (INC-5521). Vozilo blokirano do servisa." ── turn 2
korisnik: "Ne" umjesto "Da" → clear pending → "U redu, odustajem."
korisnik: nešto treće → "Nisam siguran je li to Da ili Ne…" (pending ostaje)
```

**S3 — missing param multi-turn** (`"rezerviraj auto sutra od 9"` — fali `date_to`)
```
router→{action:book_vehicle, params:{date_from:"2026-06-12T09:00:00"}}
  →validator: missing_required=[date_to]→pending_params.save(collected={date_from})
  bot: "Do kada trebaš vozilo?"                                ── kraj turna 1
korisnik: "do 15" → korak 14: pending_params nastavak
  →param_ui.parse("do 15", datetime)→"2026-06-12T15:00:00"→required puni
  →nastavi na korak 22-24 (confirm)→…→execute                  ── turn 2+
korisnik umjesto odgovora: "odustani" → clear → "U redu, odustajem."
korisnik: nova nevezana poruka → state se čisti, poruka ide kao svježa (korak 17)
```

**S4 — šifrarnik clarify** (`incident_type` dvosmislen, opcija B fallback)
```
AI: incident_type="problem"→type_resolver: GET /CaseTypes(tenant)→
  [(3,Kvar),(5,Šteta),(7,Nezgoda)]→match("problem")=None (nije jednoznačno)
  bot: "Kakav problem? Dostupno: Kvar, Šteta, Nezgoda."        ── clarify turn
korisnik: "kvar"→match→3→nastavi (S2 tok)
```

**S5 — nepoznat identitet**
```
identity.resolve: Persons nema broj (ni NSN contains-fallback)→is_known=False
  bot: "Tvoj broj još nije povezan s računom… kontaktiraj managera." (terminal)
  [nikad se ne zove nijedan /actions — nema tenant konteksta = nema poziva]
```

**S6 — safety short-circuiti** (svaki PRIJE routinga, koraci 8-13)
```
rate-limit blok  → "Šalješ previše poruka, pričekaj…"      (korak 8)
prompt-injection → blok poruka                              (korak 10)
crisis signal    → hotline poruka (Plavi telefon 116 123)   (korak 12)
GDPR "obriši me" → audit zapis + potvrda postupka           (korak 13)
```

**S7 — Business API greška**
```
executor→POST /actions/book-vehicle→409 {"error_code":"VEHICLE_UNAVAILABLE",
  "message":"Nema slobodnih vozila u periodu"}
  →api_error_translator (cache 1h)→bot: "Nažalost, nema slobodnih vozila
   u tom periodu. Pokušaj drugi termin."       [strukturiran error = ugovor §8]
5xx/timeout→circuit breaker broji; pending confirm OSTAJE→korisnik može "Da" opet
```

**S8 — RAG / knowledge (Faza 2)** (`"smijem li službenim autom na godišnji?"`)
```
router→{kind:answer, capability:answer_from_policy}
  →rag_retriever: embed(upit)→top-3 chunka iz tenant car_policy.pdf (§12.4 storage)
  →formatter(system="odgovori SAMO iz priloženih odlomaka, citiraj izvor")
  →"Prema Pravilniku (čl. 7): privatno korištenje je dopušteno uz…"
[nijedan /actions poziv; read-only; ako nema relevantnih chunkova→"Nemam tu
 informaciju u dokumentima — kontaktiraj managera."]
```

**S9 — Viber krug** (isti mozak, drugi rub)
```
Infobip→POST /webhook/viber→HMAC→stream_data{…, channel:"viber"}→isti koraci 4-28
  →enqueue_outbound nosi channel:"viber"→worker outbound grana→Infobip Viber send
[V2Engine NE zna razliku — channel je samo tag na rubu; test: isti E2E s oba taga]
[⚠ prerequisite: Viber sender registriran/odobren kod Infobipa — vidi §5.5]
```

**S10 — M365 Copilot krug (preko MCP servera — OBAVEZNA komponenta, faza uz Copilot kanal)**
```
User u Teamsu: "rezerviraj mi auto sutra 9-15"
  → [Microsoft Copilot = MOZAK: razumije, ekstrahira, bira tool]
  → MCP protokol → [naš services/mcp/server.py]
       • tools = ISTE akcije iz config/actions.json (jedan izvor istine)
       • identitet: Copilot daje user email → GET /Persons?Filter=Email(=)…
         → person_id + TenantId (isti strict-binding kao phone put)
         [✓ Email polje VERIFICIRANO u Persons output_keys (34 polja);
          filterabilnost po Email = potvrditi uz M1 filter-schema odgovor —
          Phone(=) filter već živo radi, pa je rizik nizak]
       • AUTH SAMOG MCP SERVERA: prima SAMO autenticirane M365 pozive
         (Entra ID token validacija na našem MCP endpointu) — dizajn otvoren,
         vlasnik F-M365 faza (otvorena odluka #6 u §9)
       • write akcije deklarirane s MCP annotation "requiresConfirmation"
         → POTVRDU RENDERIRA COPILOT UI (M365 confirmation prompt) — naš
           Da/Ne gate je za chat kanale; ovdje istu ulogu ima Copilotov UI
  → executor put → POST /actions/book-vehicle (identičan §6 contract)
  → rezultat natrag Copilotu → COPILOT formatira odgovor korisniku
[naš V2Engine (WhatsApp mozak) je ZAOBIĐEN — Copilot je mozak; mi dajemo alate]
```
Kod-skelet (Python `mcp` SDK; gradi se TEK kad /actions postoji jer ga samo omata):
```python
# services/mcp/server.py  ⟵ NOVO (F-M365)
from mcp.server import Server
from services.v2.action_registry import ActionRegistry

registry = ActionRegistry.load(ACTIONS_JSON)
server = Server("fleet-actions")

@server.list_tools()
async def list_tools():
    return [tool_from_action(a) for a in registry.actions.values()]
    # tool_from_action: name+description+inputSchema iz ai.parameters (§3),
    # annotations={"requiresConfirmation": a["policy"]["mutation"]}

@server.call_tool()
async def call_tool(name: str, arguments: dict, *, context):
    identity = await resolve_identity_by_email(context.user_email)  # /Persons
    return await execute_action(name, arguments, identity)          # §5.3 — isti executor
```

---

## 12. TENANTI — tko je izvor istine + bot-side postavke

### 12.0 Odakle tenanti dolaze (izvor istine) — NEMA preseta

**Pitanje:** imamo li preset tenante? Moramo li ih mi kreirati/spremati?
**Odgovor (dokazano iz registryja):** NE. **M1 backend je izvor istine za
tenante** — registry sadrži **44 tenant endpointa**, uključujući puni CRUD:

```
GET    tenantmgt/Tenants              ← bulk lista SVIH tenanta (get_Tenants)
GET    tenantmgt/Tenants/{id}         ← pojedinačni (get_Tenants_id)
POST   tenantmgt/Tenants              ← kreiranje (admin op, ne bot)
PATCH/PUT/DELETE tenantmgt/Tenants/{id}
+ TenantPermissions familija (roles per user), Partners link/unlink…
```

Bot tenante **NE kreira i NE presetira** — samo ih OTKRIVA. Dva mehanizma:

```
MEHANIZAM 1 — LAZY per-user (ŽIV, DOKAZAN — radi danas u produkciji):
  prva poruka korisnika → identity.resolve(phone) → GET /Persons?Filter=Phone(=)…
  → response nosi TenantId (identity.py:487, strict binding — bez TenantId
    korisnik se odbija, nikad env-default)
  → tenant "stigne sa svakim korisnikom" — NULA pripreme unaprijed
  → bot-side settings row se auto-kreira s defaultima ako ne postoji
    (isti lazy-onboarding pattern kao postojeći upsert_user_mapping)

MEHANIZAM 2 — BULK SYNC (opcionalan boost, na startu ili cron):
  GET /Tenants → upsert svih u tenant_settings (poznata lista prije 1. poruke)
  ⚠ iskreni caveat: smije li NAŠ client_credentials listati SVE tenante
    (OAuth scope) potvrđujemo na dev accessu — pitanje je već poslano u
    M1_ZAHTJEV_endpoint_tagging §2. LAZY mehanizam NE ovisi o tome i već radi.
```

**Dakle odgovor na "možemo li to UVIJEK na početku napraviti": DA** —
mehanizam 1 garantira tenant za svakog korisnika bez ikakvog preseta;
mehanizam 2 je ubrzanje, ne uvjet.

### 12.1 Presuda o današnjem config/tenants/ folderu (iskreno)

Danas: `config/tenants/<tenant_id>/tool_subset.json` — statični fileovi u repou.
**Filipov instinkt je točan — to ne valja za produkciju:**

| Problem | Posljedica |
|---|---|
| Dodavanje tenanta = commit + build + deploy | onboarding klijenta traje sate, ne sekunde |
| Config živi u image-u | k8s pod restart = jedini način izmjene |
| Nema audita | tko je i kada mijenjao tenant config → nepoznato |
| Ne skalira | 100 tenanta = 100 foldera u repou |

**Preciznost (bitno):** problem NIJE JSON format — JSON(B) u bazi je potpuno
ispravan i koristimo ga. Problem je **file u repou** (traži commit+deploy za
izmjenu). JSON folder ostaje samo kao dev seed/fixture; produkcija ide na DB.

### 12.2 Dizajn bot-side postavki: Postgres + Redis cache + Admin API

**Točan pattern već postoji u repou** — `tenant_resolver.py` radi identično za
phone→tenant (Postgres `user_mappings` + `tenant_phone:` cache TTL 300s + purge/
invalidate). Samo ga proširujemo na tenant konfiguraciju:

```sql
-- alembic migracija 004_tenant_settings.py
-- NAPOMENA: ovo NIJE tenant registry (izvor istine za tenante je M1 — §12.0).
-- Ovo je BOT-SIDE OVERLAY postavki, keyed by M1 TenantId. Redak se kreira
-- lazy s defaultima na prvi susret s tenantom, ili bulk syncom.
CREATE TABLE tenant_settings (
    tenant_id       TEXT PRIMARY KEY,          -- = M1 TenantId (iz /Persons ili /Tenants)
    name            TEXT,                      -- display (iz GET /Tenants, informativno)
    bot_status      TEXT NOT NULL DEFAULT 'active',   -- active|paused (bot-side gate)
    settings        JSONB NOT NULL DEFAULT '{}',      -- jezik, radno vrijeme, limiti…
    actions_enabled JSONB NOT NULL DEFAULT '{}',      -- {"book_vehicle":true,…}; prazno = sve default-on
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

```python
# services/tenant_config.py  ⟵ NOVO (po uzoru na postojeći TenantResolver)
CACHE_PREFIX = "tenant_cfg:"
CACHE_TTL = 300          # isti self-healing rationale kao tenant_phone:

class TenantConfigStore:
    """Read-through: Redis → Postgres. Invalidate na svaku admin izmjenu."""

    async def get(self, tenant_id: str) -> dict | None:
        cached = await self._redis.get(f"{CACHE_PREFIX}{tenant_id}")
        if cached:
            return json.loads(cached)
        row = await self._db_fetch(tenant_id)          # SELECT … FROM tenant_settings WHERE tenant_id=:id
        if row is None:
            row = await self._db_insert_defaults(tenant_id)   # lazy: prvi susret → defaulti
        await self._redis.set(f"{CACHE_PREFIX}{tenant_id}", json.dumps(row), ex=CACHE_TTL)
        return row

    async def upsert(self, tenant_id: str, *, name, settings, actions_enabled) -> None:
        await self._db_upsert(...)                      # INSERT … ON CONFLICT DO UPDATE
        await self._redis.delete(f"{CACHE_PREFIX}{tenant_id}")   # invalidate → sljedeći read s DB

# webhook_simple.py — admin endpointi (isti admin_auth token pattern kao gdpr-process)
POST  /admin/tenants          {id, name, settings?, actions_enabled?}   → upsert
PATCH /admin/tenants/{id}     {status?|settings?|actions_enabled?}      → update+invalidate
GET   /admin/tenants/{id}     → trenutno stanje (iz DB, bypass cache)
```

### 12.3 Walkthrough: "što se dogodi kad M1 dobije NOVOG tenanta?"

```
0. MI NE RADIMO NIŠTA. (Tenant je kreiran u M1 — njihov admin, njihov posao.)
1. Prva poruka vozača nove firme:
     identity.resolve → GET /Persons → TenantId="t-novi"   (mehanizam 1, §12.0)
2. TenantConfigStore.get("t-novi") → cache miss → DB miss
     → lazy INSERT defaults u tenant_settings → keširaj 300s
3. Bot RADI za novog tenanta.     ⏱ nula pripreme, ZERO deploy, ZERO commit
   (opcionalno: bulk sync ga je već upisao prije prve poruke — mehanizam 2)

Admin API (/admin/tenants) služi SAMO za bot-postavke postojećih tenanta:
  isključi/uključi akciju, promijeni jezik, upload RAG dokumenata —
  NE za "kreiranje tenanta" (to je M1).
```

### 12.4 Per-tenant znanje (RAG dokumenti — car policy itd.)

Isti princip — **ne u repo**: upload preko admin endpointa → pohrana u Postgres
(`tenant_documents` tablica: id, tenant_id, filename, content bytea/text,
uploaded_at) ili object storage → indeksiranje (embeddings) u pozadini →
`knowledge/rag_retriever` čita indeks po tenant_id. Dodavanje dokumenta = upload,
ne deploy.

**GDPR / offboarding tenanta:** kad tenant ode, briše se `tenant_settings`
red + svi njegovi RAG dokumenti + embeddings indeks + `tenant_cfg:` cache —
isti purge pattern kao postojeći `/admin/gdpr-process` (dry-run pa stvarni).

### 12.5 Što umire s ovim redizajnom

`config/tenants/*/tool_subset.json` — u /actions svijetu suvišan dvostruko:
katalog je ~30 globalnih akcija (nema 950 za subsetirati), a per-tenant
uključivanje/isključivanje akcija je `tenants.actions_enabled` u DB. Folder
ostaje samo kao dev-seed dok migracija traje (Strangler Fig — vidi
`PLAN_KONVERGENCIJA_10_OD_10_2026-06-11.md`).

---

## 13. PARAM LIFECYCLE — od korisnikove rečenice do API polja

### 13.1 Cijeli cjevovod (jedan pogled)

```
 "rezerviraj auto sutra od 9 do 15, hitno je"
        │
 ┌──────▼──────────────────────────────────────────────────────────────────┐
 │ ① LLM EKSTRAKCIJA (llm_router, §3 opisi+examples su KRITIČNI)            │
 │    → {date_from:"sutra 9h", date_to:"15h", note:"hitno"}                │
 ├──────────────────────────────────────────────────────────────────────────┤
 │ ② VALIDACIJA (action_validator §5.2)                                     │
 │    akcija postoji? polja u shemi? grubi tipovi?  fail→clarify            │
 ├──────────────────────────────────────────────────────────────────────────┤
 │ ③ COERCION (param_ui.parse_param_value + _coerce_llm_params — postoji)   │
 │    "sutra 9h"→2026-06-12T09:00:00 · "12,5"→12.5 · "da"→true             │
 │    (HR datumi/dani u tjednu/dijelovi dana; Europe/Zagreb)                │
 ├──────────────────────────────────────────────────────────────────────────┤
 │ ④ MISSING-REQUIRED LOOP (pending_params — postoji)                       │
 │    fali polje→HR pitanje→ODGOVOR SE PAMTI (Redis 300s)→sljedeće polje    │
 │    "odustani"→abort · nova tema→state clear + svježe routanje            │
 ├──────────────────────────────────────────────────────────────────────────┤
 │ ⑤ CODEBOOK RESOLVE (§10.2: C default → backend; B fallback→type_resolver)│
 ├──────────────────────────────────────────────────────────────────────────┤
 │ ⑥ IDENTITY INJECT (policy.inject: person_id, tenant_id — AI ih NE puni)  │
 ├──────────────────────────────────────────────────────────────────────────┤
 │ ⑦ PARAM ECHO u confirm poruci (_render_param_echo — postoji)             │
 │    "Provjeri prije slanja: • Od: 12.06.2026. 09:00 • Do: … • Napomena…"  │
 │    → korisnik VIDI točno što se šalje PRIJE nego se pošalje              │
 ├──────────────────────────────────────────────────────────────────────────┤
 │ ⑧ SLANJE + BACKEND VALIDACIJA (strukturiran 400 → HR prijevod)           │
 └──────────────────────────────────────────────────────────────────────────┘
```

### 13.2 Tko hvata koju grešku (station-by-station)

| Stanica | Greška | Tko hvata | Korisnik vidi |
|---|---|---|---|
| ① | LLM izmisli polje | ② validator (unknown_param) | clarify pitanje |
| ① | LLM izmisli akciju | ② validator (unknown_action) | clarify pitanje |
| ③ | "petak u 26h" neparsabilno | coercion→None | re-ask s primjerom formata |
| ④ | korisnik šuti 5 min | Redis TTL istekne | sljedeća poruka = svježa |
| ⑤ | šifrarnik dvosmislen | type_resolver match=None | pick-lista opcija |
| ⑥ | identity nema vehicle_id | executor required-ctx guard | "ne mogu dohvatiti tvoj profil…" |
| ⑦ | korisnik vidi krivi datum | on sam kaže "Ne" | odustajanje, ništa poslano |
| ⑧ | backend odbije (pravilo) | api_error_translator | HR objašnjenje ŠTO i KAKO dalje |

**Načelo:** nijedna stanica ne "propušta šutke" — svaka greška ima definiran
ishod koji korisnik razumije, i **ništa se ne piše u backend bez ⑦ (echo + Da)**.

---

## 14. SCALABILITY & ENGINEERING PROOF

### 14.1 Kako svaki sloj skalira

| Sloj | Danas | Bottleneck | Put skaliranja |
|---|---|---|---|
| webhook api | ×2 replike, stateless, HPA 2-4 | — (verify+XADD, featherweight) | samo replike |
| Redis | ×1, AOF everysec, noeviction | RAM (384MB cap) | queue TTL-ovi drže footprint malim; managed Redis za HA |
| worker | ×1 (namjerno — per-sender redoslijed) | LLM+API latencija po turnu | consumer grupa VEĆ podržava ×N; prije toga per-sender lock seliti u Redis (recept: k8s/README) |
| Azure LLM | gpt-4o-mini | TPM/RPM kvota | 2 poziva/turn (decision+format) ⇒ ~120 vozača komotno; kvota se diže zahtjevom |
| Business API | Damirova strana | njihov capacity | ugovoriti SLA/limite (M1 addendum §6) |
| Postgres | user_mappings + tenants | — (KB reda veličine) | managed PG |

**Telemetrija /actions puta (mjerenje 90/97/0):** postojeći `TelemetryEvent`
shape se NE mijenja — `tool_picked`=ime akcije, `error`=`validation_fail:<razlog>`
/ `action_http_<code>`, `latency_ms`, `clarify` — isti KQL upiti, novi prostor
vrijednosti. Golden-set harvester (build_golden_set.py) radi bez izmjena.

**Cost po turnu (gruba procjena, gpt-4o-mini):** 2 LLM poziva (decision ~2-4k
input tokena za 30 akcija + format ~1-2k) ≈ **$0.001-0.002/poruci** → 1000
poruka ≈ $1-2. S gpt-4o ~15-20×. Nije bottleneck ni na 100× volumenu.

**Latency budžet po turnu:** webhook <50ms · queue hop <100ms · safety+identity
<200ms (cache hit) · LLM decision 0.5-2s · executor→/actions 0.2-2s (cap 15s) ·
LLM format 0.5-1.5s → **tipično 2-6s**; tvrdi capovi: 15s executor / 90s turn.

### 14.2 Failure-mode matrica (ponašanje je već implementirano i testirano)

| Kvar | Ponašanje | Mehanizam (postoji) |
|---|---|---|
| Redis pun | webhook 503 → Infobip retry (ništa se ne gubi tiho) | noeviction + retry |
| M1/Business API down | fail-fast poruka, bez gomilanja | circuit breaker (executor+gateway) |
| Azure 429/5xx | retry ×3 backoff → siguran fallback | llm_router retry petlja |
| worker pod restart | poruke ČEKAJU u streamu; nastavlja novi pod | consumer grupa + AOF |
| crash usred slanja odgovora | requeue iz processing liste; bez duplikata | sent:{idem} dedup 600s |
| dupli webhook (Infobip retry) | jedna obrada | wh_dedup + msg_lock |
| dupli "Da" (double-tap) | jedna mutacija | exec lock 30s + Idempotency-Key |

### 14.3 Iskreni okvir tvrdnji (bez ovoga bi "radi" bila laž)

**DOKAZANO (mjerljivo, danas):**
- 1749 testova zeleno + 4 E2E razgovora kroz produkcijski factory s pravim
  registrom (read, write+confirm, decline, reoffer) + ruff čist CI.
- Svi safety/param/state mehanizmi iz §11 SU živi kod u produkcijskom putu.
- Garancija odgovora korisniku — formalno dokazana enumeracijom svih izlaznih
  putanja u **§17** (svaka završava porukom ili DLQ+alarm).
- k8s manifesti (YAML-validirani) + runbook; resilience mehanizmi iz §14.2.

**DIZAJN s definiranim protokolom validacije (postaje "dokazano" tek kad prođe):**
- `/actions` integracija (Business API još ne postoji). Protokol:
  ① contract-testovi na §6/§8 ugovore → ② smoke na dev M1 (postojeće probe
  skripte) → ③ benchmark ≥ 90/97/0 na golden setu (dual-seed) → ④ 2 tjedna
  zelenog pilota. Redoslijed uvođenja: `PLAN_KONVERGENCIJA_10_OD_10_2026-06-11.md`
  (Strangler Fig — ništa se ne briše prije dokazane zamjene).
- Tenants DB redizajn (§12) — pattern je preslika VEĆ dokazanog tenant_resolvera.

---

### 14.4 SHOWSTOPPER REGISTAR — sve što bi značilo "neće raditi" (trajna tablica)

Pravilo (kao §17.3): svaki poznati "neće raditi zbog toga" rizik MORA biti ovdje
s vlasnikom i statusom. Nema nepraćenih showstoppera.

| # | Stavka | Simptom ako fali | Mitigacija | Vlasnik | Status |
|---|---|---|---|---|---|
| 1 | World A/B — `/actions` uopće ne postoji | nema se što zvati | odluka na sastanku; World B stopgap postoji (flow_engine predložak) | Filip+Damir (pon.) | 🔴 GATE |
| 2 | **OAuth scope za `/actions/*` rute** | SVAKI poziv = 403 (⚠ dokazano živo 2026-05-30: "403 scope — bot nema ovlasti", DAMIR_ACCURACY_UGOVOR:27) | ugovor §8 #7 — pisana potvrda PRIJE deploya + smoke test scope-a na dev-u | Damir/M1 | 🔴 OPEN |
| 3 | Business API hosting (koji host?) | 404/DNS na svakom pozivu ako je drugi host | `BUSINESS_API_URL` env (§3.3, §5.3) — config, ne pretpostavka | Filip (config), Damir (info) | 🟡 TRACKED |
| 4 | Viber sender registracija kod Infobipa | Viber kanal mrtav bez obzira na kod (approval = dani-tjedni) | pokrenuti registraciju ODMAH, paralelno s razvojem (§5.5) | Filip/ops | 🟡 TRACKED |
| 5 | Timezone semantika /actions datetimea | rezervacije pomaknute 1-2h (radi-ali-krivo) | ugovor §8 #8 | Damir/M1 | 🟡 TRACKED |
| 6 | Bulk `GET /Tenants` scope | bulk sync ne radi (LAZY put NE ovisi — radi i bez) | potvrda na dev accessu; lazy je default | M1 | 🟢 NIJE blokator |
| 7 | Email filterabilnost u /Persons | Copilot identitet treba drugi lookup | potvrda uz filter-schema odgovor; Phone(=) već živo radi | M1 | 🟡 TRACKED (F-M365) |
| 8 | Auth MCP servera (Entra ID) | tuđi pozivi na naše akcije | dizajn u F-M365 fazi (§9 #6, S10) | Filip (F-M365) | 🟡 TRACKED |
| 9 | Azure TPM/RPM kvota | 429 oluje na skoku volumena | retry+backoff živ; kvota se diže zahtjevom (§14.1) | Filip/AZ | 🟢 mitigirano |
| 10 | Dostava poruke (WA/Viber platforma) | korisnik blokirao bota / kanal down | DLQ+alarm (§17) — trajni bound, nitko ne kontrolira | — | 🟢 bound |

**Kriterij zatvaranja registra prije go-livea:** nijedan 🔴; svi 🟡 imaju
potvrđen datum/odgovor ili degradaciju koja ne ruši sustav.

---

## 15. USKLAĐENOST SA ŠEFOVIM OVERVIEW DOKUMENTOM (docx v2.1)

Šefov dokument je kanonski overview; ova specifikacija je njegova razrada.
Mapiranje njegovih 7 komponenti → naša implementacija:

| Šefova komponenta | Naš modul(i) | Napomena |
|---|---|---|
| 1. Channels (WA/Viber/Web/M365) | `webhook_simple.py` + `services/channels/` | WA živ; Viber = §5.5; Web/M365 kasnije faze |
| 2. AI Backend (thin) | `V2Engine` | postaje thin migracijom orkestracije u /actions; safety slojevi ostaju (nisu "business logika" — pravna/etička obveza) |
| 3. OpenAI (decision+conversation) | `llm_router` + `llm_formatter` | + naš dodatak: Da/Ne gate između decision i execution (šef ga u QB mailu sam traži — human-in-the-loop) |
| 4. Tool Config (ai/execution) | `config/actions.json` (§3) | identična struktura kao njegov primjer `book_vehicle` |
| 5. MCP (execution+auth) | danas: `executor.py`+`api_gateway.py`+`token_manager.py` (funkcionalni ekvivalent); cilj: **+ `services/mcp/server.py` — OBAVEZAN** jer je M365 Copilot kanal u šefovom docu (S10). Gradi se čim /actions postoji (samo ga omata) |
| 6. Business API /actions | — (Damirova strana) | ugovor u §8; World A/B odluka u §10.4 |
| 7. Domain/Granular API | 950 M1 Swagger ruta | ispod Business API-ja; bot ih (nakon migracije) ne zove direktno |

**Njegov `/chat` endpoint vs naš async ulaz:** njegov dijagram implicira sync
`/chat`. Naš WA/Viber ulaz je **async** (webhook→stream→worker) — to je ispravno
za messaging kanale (Infobip očekuje brz 200; obrada traje sekunde; retry
semantika). Za Web kanal (kasnije) izlaže se sync `/chat` fasada koja interno
koristi ISTI engine — bez dupliranja.

**Njegov MCP input format** `{tool, input, user:{email,phone,token}}` ≡ naš
contract §6 ②: `tool`→ime akcije u ruti, `input`→body (poslovni parametri),
`user`→naš identity inject (kod nas razriješen PRIJE poziva: phone→person_id/
tenant_id kroz identity.py; per-user token nije primjenjiv na WA — §10.4).

**Tri ispravka pseudokoda iz ranijih skica** (da implementacija ne zaluta):
1. executor je `services/v2/executor.py`, **async** preko `api_gateway.call`
   (`executor.py:90/187`) — ne sync `requests.post` (blokirao bi event-loop);
2. auth ide kroz `TokenManager.get_token()` + `x-tenant` + `Idempotency-Key`
   + SSRF guard — ne ručno slaganje headera i hardkodirani URL;
3. **write akcije uvijek kroz mutation_gate (Da/Ne) prije poziva** — "OpenAI
   odlučuje" znači *bira akciju*, ne *izvršava bez potvrde*.

---

## 16. DOKUMENTACIJSKA KARTA (egzaktno: što ostaje, što je apsorbirano)

Session docs (9) — odluka po svakom:

| # | File | Odluka | Gdje je sadržaj |
|---|---|---|---|
| 1 | `ACTIONS_TEHNICKA_SPECIFIKACIJA_2026-06-11.md` | ✅ **KEEP — MASTER** | ovaj dokument (§0-§16) |
| 2 | `USPOREDBA_ARHITEKTURA_STARA_NOVA_2026-06-11.md` | ✅ KEEP | stara vs nova, prvo lice (za šefa) |
| 3 | `PLAN_KONVERGENCIJA_10_OD_10_2026-06-11.md` | ✅ KEEP | migracijski plan (faze, gate na World A/B) — referenciran iz §12.5/§14.3 |
| 4 | `M1_ZAHTJEV_ADDENDUM_2026-06-11.md` | ✅ KEEP | zapis poslanog zahtjeva M1 timu |
| 5 | `SEF_ARHITEKTURA_USPOREDBA_2026-06-11.md` | 🗑 OBRISAN | apsorbiran u §15 (mapiranje 7 komponenti) |
| 6 | `ACTIONS_ROUNDTRIP_TEHNICKI_2026-06-11.md` | 🗑 OBRISAN | apsorbiran u §4 (lifecycle), §10.4 (World A/B), §15 (3 ispravka) |
| 7 | `DIZAJN_ACTIONS_OTVORENA_PITANJA_2026-06-11.md` | 🗑 OBRISAN | apsorbiran u §9 + §10 (deep-dive) |
| 8 | `ROADMAP_DO_GOTOVOG_2026-06-11.md` | 🗑 OBRISAN | pisan za staru arhitekturu; važeći dijelovi (deploy/pilot/mjerenje) u §14 |
| 9 | `_draft_poruka_damiru.md` | 🗑 OBRISAN | poruka poslana — svrha ispunjena |

Ne diraju se: `docs/SUSTAV/` (16 — referenca ŽIVOG sustava), stariji `docs/` (37),
root `*.md` — dokumentiraju postojeći sustav, nisu session-višak.


---

## 17. DOKAZ: korisnik UVIJEK dobije odgovor

**Tvrdnja (precizno):** za svaku primljenu poruku bot ili (a) pošalje odgovor,
ili (b) parkira odgovor u DLQ **s alarmom** (operater vidi) — nikad tiha smrt.
**Honest bound:** finalna DOSTAVA ovisi o Infobip/WhatsApp/Viber platformi
(korisnik blokirao bota, kanal down) — to ni jedan sustav ne može garantirati;
naša granica odgovornosti je "predano platformi ili DLQ+alarm".

### 17.1 Enumeracija SVIH izlaznih putanja (mapirano na §11 korake)

| # | Izlaz iz flowa (korak §11) | Što korisnik dobije | Dokaz (kod/test) |
|---|---|---|---|
| 1 | HMAC fail (1) | ništa (napadač, ne korisnik) — 401 | webhook_simple HMAC fail-closed |
| 2 | dupli webhook (2) | ništa (već odgovoren original) | wh_dedup + msg_lock, test_webhook |
| 3 | Redis pun na XADD (3) | odgovor NAKON Infobip retryja | 503→Infobip retry; noeviction (k8s/redis.yaml) |
| 4 | rate-limit (8) | "Šalješ previše poruka…" | rate_limiter + test |
| 5 | prompt-injection (10) | blok poruka | input_sanitizer + test_prompt_injection |
| 6 | nepoznat broj (11) | enrollment poruka | engine unknown-phone gate |
| 7 | crisis signal (12) | hotline poruka | crisis_detector + test |
| 8 | GDPR/special (13) | potvrda postupka | special_intents + audit + test |
| 9 | clarify / param-ask / confirm (18/21/24) | pitanje (Da/Ne, param, izbor) | pending_* stores + testovi |
| 10 | akcija OK (25-27) | formatirani HR odgovor | E2E test_e2e_trips_scenario (4 scenarija) |
| 11 | Business API 4xx (26) | HR objašnjenje ŠTO i KAKO dalje | api_error_translator + test |
| 12 | Business API 5xx/timeout (26) | generička HR + pending OSTAJE (retry "Da") | executor + circuit breaker testovi |
| 13 | engine vrati None/prazno (7) | "Greška pri obradi poruke…" | **worker.py:945** fallback |
| 14 | engine timeout 90s (7) | "Obrada je trajala predugo…" | **worker.py:964** |
| 15 | engine iznimka (7) | generička HR + poruka u dlq:inbound | **worker.py:981** + _store_dlq |
| 16 | outbound send fail — tranzijentan (30) | odgovor nakon retry ×3 (backoff 5/10/20s) | _send_whatsapp + test_worker_edge_fixes |
| 17 | outbound fail — permanentan/iscrpljen (30) | DLQ + `dlq_growing` alarm u health logu | _store_outbound_dlq + health reporter |
| 18 | crash NAKON slanja, prije ACK-a | NEMA duplikata pri requeue | sent:{idem} dedup 600s + test |
| 19 | worker pod restart usred obrade | odgovor od novog poda (stream čuva poruku) | consumer grupa + AOF; ack-tek-nakon-enqueue |
| 20 | iznimka u samoj outbound petlji | poruka u DLQ + petlja ŽIVI dalje | audit fix (pump-death) + test_aud3_* |

**Zašto je tablica potpuna:** redci 1-12 pokrivaju svaki *odlučni* izlaz iz §11
(svaki `→ kraj turna` ili `terminal`); redci 13-15 su worker safety-net koji
hvata SVE što engine ne vrati uredno (tri jedina ishoda poziva: vrijednost /
timeout / iznimka — sva tri pokrivena); redci 16-20 pokrivaju izlazni put i
crash prozore. Ne postoji izlaz koji nije u jednoj od te tri klase.

### 17.2 Što je od ovoga POPRAVLJENO u auditu (prije NIJE vrijedilo!)

Iskrenost: prije audita 2026-06-11 garancija NIJE vrijedila — 4 rupe su nađene
i zatvorene s testovima: outbound pump death (UnboundLocalError ubijao petlju
do restarta), zaglavljene poruke u processing listi bez DLQ-a, ACK-as-duplicate
na non-text enqueue failu, idempotency kolizija na delayed retryju. Svi fixovi
u `test_worker_edge_fixes.py` (AUD-3 sekcija). Garancija vrijedi OD tog commita.

### 17.3 Kako se garancija ČUVA (regression osiguranje)

Svaki novi izlazni put u kodu MORA dodati redak u ovu tablicu + test. CI gate:
suite zelena = tablica važi. (Za /actions svijet: redci 10-12 dobivaju nove
testove nad `/actions` contractom — test_e2e_actions.py u §2 stablu.)

---

### Reference u repou (za implementaciju)
`services/v2/engine.py` (dispatch) · `services/v2/executor.py` (async gateway) ·
`services/v2/type_resolver.py` (šifrarnik) · `services/v2/pending_params.py` +
`param_ui.py` (missing-param) · `services/v2/conversation_history.py` (kontekst) ·
`services/router/llm_router.py` (LLM poziv) · `services/api_gateway.py` (HTTP) ·
`config/tool_data.json` (današnji shape → uzor za `actions.json`).
