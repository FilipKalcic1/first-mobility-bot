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
│   ├── api_gateway.py               # (postoji) HTTP + circuit breaker + SSRF + idempotency
│   ├── token_manager.py             # (postoji) OAuth client_credentials
│   └── admin_auth.py                # (postoji)
├── webhook_simple.py                # ⟵ MIJENJA: +channel tag, +/webhook/viber
├── worker.py                        # ⟵ MIJENJA: outbound grananje po channel tagu
├── k8s/                             # (postoji) produkcijski deploy (NE briši)
├── Dockerfile · docker-compose.yml  # (postoji)
└── tests/                           # svaki novi modul + svoj test
```

> Migracija je **fazna** (vidi `PLAN_KONVERGENCIJA_10_OD_10`): `actions.json` živi
> PORED `tool_data.json` iza feature-flaga dok se ne dokaže, pa se staro povlači.

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
> nose `codebook` marker i razrješuju se po §7.4.

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
    resp = await self._gateway.call(
        method=spec["execution"]["method"],
        service="",                                   # /actions je na root Business API
        path=spec["execution"]["action"],            # "/actions/report-incident"
        body=body if spec["execution"]["method"] == "POST" else None,
        tenant_id=identity["tenant_id"],
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

**Ugovor koji tražimo od backenda po akciji:** ruta, input DTO (čista poslovna
polja), strukturiran error (`error_code`+`message`), i tko mapira šifrarnike
(preporuka: backend — §7.4 opcija C).

---

## 9. Otvorene odluke (sažetak — puni kontekst u `DIZAJN_ACTIONS_OTVORENA_PITANJA`)

| # | Odluka | Preporuka |
|---|---|---|
| 1 | Granica AI-puni vs backend-puni parametre | `ai.parameters` = samo poslovni; interno u backendu |
| 2 | Gdje šifrarnici (`CaseType` per-tenant) | Business API mapira semantiku (opcija C); `type_resolver` fallback |
| 3 | Registracija → VehicleId | backend resolve (World A) ili bot pre-resolve (rana validacija) |
| 4 | Tko gradi `/actions` | **pitanje za Damira** — World A (backend) vs World B (bot BFF) |
| 5 | RAG/knowledge | Faza 2, zasebna sposobnost `answer_from_policy` |

---

### Reference u repou (za implementaciju)
`services/v2/engine.py` (dispatch) · `services/v2/executor.py` (async gateway) ·
`services/v2/type_resolver.py` (šifrarnik) · `services/v2/pending_params.py` +
`param_ui.py` (missing-param) · `services/v2/conversation_history.py` (kontekst) ·
`services/router/llm_router.py` (LLM poziv) · `services/api_gateway.py` (HTTP) ·
`config/tool_data.json` (današnji shape → uzor za `actions.json`).
