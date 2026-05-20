# Config Guide — kompletan walkthrough svih JSON-ova u `config/`

Verziju: 2026-05-16. Post-Phase-E + production safety fixes.

---

## Vizualna mapa

```
config/
├── tool_data.json                       3.8 MB   ★ ROUTING HEART
├── processed_tool_registry.json         3.5 MB   ★ EXECUTOR HEART
├── context_param_schemas.json             3 KB     parameter classifier
│
├── domain/
│   └── path_entity_map.json              21 KB     build-time only
│
├── linguistic/
│   └── typo_synonyms.json                 1 KB     hot-path slang
│
└── tenants/
    └── _default/
        └── tool_subset.json             ~10 KB     Phase E catalog scope
```

**6 file-ova ukupno.** 2 velika (binarno-velika ali JSON), 4 mala. Tri direktorija. Sve ostalo u `config/` koje vidiš drugdje je junk — obrisan u prethodnom cleanup-u.

---

## Hot-path message flow — koji file kad?

Kad korisnik pošalje WhatsApp poruku, redoslijed:

```
   ┌─────────────────────────────────────────────────────────────────┐
   │  WhatsApp webhook → Redis stream → worker.py picks up message   │
   └──────────────────────────┬──────────────────────────────────────┘
                              │
                              ▼
   ┌─────────────────────────────────────────────────────────────────┐
   │  L0  identity.resolve(phone)                                    │
   │  ────────────────────────────                                   │
   │  Redis cache (30s TTL) — če miss, call /Persons + /MasterData   │
   │  Resolve persona:                                               │
   │    READS tenants/{tenant_id}/personas.json  [optional, lazy]    │
   │    fallback: "driver"                                           │
   └──────────────────────────┬──────────────────────────────────────┘
                              │
                              ▼
   ┌─────────────────────────────────────────────────────────────────┐
   │  L0.5  text_normalizer.normalize_query(message)                 │
   │  ────────────────────────────────────────────                   │
   │  USES linguistic/typo_synonyms.json (lazy, in-memory)           │
   │  "auta" → "vozila", "rezerviraj" → kanonski oblik                │
   └──────────────────────────┬──────────────────────────────────────┘
                              │
                              ▼
   ┌─────────────────────────────────────────────────────────────────┐
   │  L1/L2  special_intents + intent_type                            │
   │  ────────────────────────────────────                            │
   │  Greetings, GDPR, help → short-circuit                          │
   │  No config files involved.                                      │
   └──────────────────────────┬──────────────────────────────────────┘
                              │
                              ▼
   ┌─────────────────────────────────────────────────────────────────┐
   │  L3  router.route(query, tool_filter=scope)                      │
   │  ──────────────────────────────────────────                      │
   │  scope = catalog_scoper.scope(tenant_id, persona)               │
   │    READS tenants/_default/tool_subset.json [lazy + mtime invl]  │
   │    INTERSECT s personas_strict iz tool_data.json [in-memory]    │
   │  anchor_index.top_k(query, allowed_tool_ids=scope)              │
   │    Embedding pretraga + cosine ranking iz tool_data.json[anchors]│
   │  LLM tool-calling s ToolSchemaBuilder schemas                   │
   │    Schema build iz tool_data.json [intent_summary, use_when]    │
   └──────────────────────────┬──────────────────────────────────────┘
                              │
                              ▼
   ┌─────────────────────────────────────────────────────────────────┐
   │  L5  confidence_gate.decide()                                   │
   │  L6  mutation_gate.decide_mutation()                            │
   │  ──────────────────────────────────                              │
   │  Pure logic, no config files.                                   │
   └──────────────────────────┬──────────────────────────────────────┘
                              │
                              ▼
   ┌─────────────────────────────────────────────────────────────────┐
   │  L7  executor.execute(tool_id, params)                          │
   │  ───────────────────────────────────                            │
   │  USES ToolRegistry [in-memory from processed_tool_registry.json] │
   │  Pozove pravi MobilityOne API endpoint                          │
   └──────────────────────────┬──────────────────────────────────────┘
                              │
                              ▼
   ┌─────────────────────────────────────────────────────────────────┐
   │  L8  formatter_llm.format(response)                             │
   │  ─────────────────────────────────                              │
   │  USES registry_dict [in-memory from tool_data.json]              │
   │  Croatian odgovor                                               │
   └──────────────────────────┬──────────────────────────────────────┘
                              │
                              ▼
                   WhatsApp poruka korisniku
```

**Što se čita za svaku poruku:** SAMO `personas.json` (lazy, mtime) + `tool_subset.json` (lazy, mtime). Sve ostalo je in-memory kopija učitana JEDNOM na worker startup.

---

## File #1 — `tool_data.json` ★ ROUTING HEART

```
┌──────────────────────────────────────────────────────────────────┐
│  ŠTO JE TO                                                       │
│  Single source of truth za sve metapodatke 950 alata.            │
│  Sadrži: operation_id, method, path, parameters, anchors,        │
│          intent_summary, use_when, do_not_use_when,              │
│          personas_strict, persona_reason                         │
└──────────────────────────────────────────────────────────────────┘
```

**Veličina:** 3.8 MB, 950 alata × ~12 KB svaki.

**Tipičan zapis:**
```json
"get_MasterData": {
  "operation_id": "get_MasterData",
  "method": "GET",
  "path": "/MasterData",
  "parameters": {"personId": {...}, "vehicleId": {...}},
  "intent_summary": "Trenutni snapshot vozila vozača",
  "use_when": ["korisnik pita o svom autu sada", ...],
  "do_not_use_when": [{"alt_tool": "get_MileageReports", "razlog": "..."}],
  "anchors": ["Koliko ima km moj auto?", ...12 fraza...],
  "personas": ["driver"],              // loose, iz arhiva
  "personas_strict": ["driver"],       // strict LLM klasifikacija
  "persona_reason": "driver pita o svom autu, marka, model..."
}
```

**Tko ga čita:**

| File | Linija | Kad |
|---|---|---|
| `services/v2/engine.py` | [1162](../services/v2/engine.py#L1162) | Startup worker-a (jednom) |

**Što sustav dobije:**
- **registry-shape** view → `LLMRouter._tools_by_id`
- **TKB-shape** view (intent_summary/use_when) → `ToolSchemaBuilder`
- **anchors-shape** ({op_id: [phrases]}) → `AnchorIndex`
- **personas_strict** lookup → `CatalogScoper`
- staleness check vs registry mtime

**Što se događa ako fali:**
```
🔴 CRASH at engine.py:1162 (FileNotFoundError).
   Worker se ne pokrene. Pod marked unhealthy.
   Bot kompletno down.
```
Fail-fast acceptable jer ova datoteka **mora postojati** — bot je nemoguće raditi bez nje.

**Kako se update-a:** Hand-edit datoteke. `tests/test_config_parity.py` provjeri invariante (svaki alat ima 4 content polja, ≥5 anchors, no duplicates, parity s registry-jem).

---

## File #2 — `processed_tool_registry.json` ★ EXECUTOR HEART

```
┌──────────────────────────────────────────────────────────────────┐
│  ŠTO JE TO                                                       │
│  Swagger-derived metadata za 950 alata u executor-shape-u.       │
│  Sadrži: operation_id, method, path, parameters (s tipovima),   │
│          required_params, dependency_graph, response_schema     │
└──────────────────────────────────────────────────────────────────┘
```

**Veličina:** 3.5 MB.

**Tipičan zapis:**
```json
{
  "tools": [
    {
      "operation_id": "get_MasterData",
      "method": "GET",
      "path": "/MasterData",
      "parameters": [{"name": "personId", "type": "string", "required": true}, ...],
      "service": "automation",
      ...
    },
    ...
  ],
  "dependency_graph": [...]
}
```

**Tko ga čita:**

| File | Linija | Kad |
|---|---|---|
| `services/registry/__init__.py` | [94](../services/registry/__init__.py#L94) | `ToolRegistry.initialize()` na worker startup |
| `services/v2/engine.py` | [1153](../services/v2/engine.py#L1153) | Samo mtime za staleness check |

**Što sustav dobije:**
- `ToolRegistry` zna kako pozvati svaki endpoint (method, path template, expected params)
- Executor (L7) koristi ovo za HTTP call

**Što se događa ako fali:**
```
🟡 GRACEFUL FAIL — logged error, ToolRegistry.is_ready = False.
   /ready endpoint vraća 503 dok se ne dignе.
   Bot pokrene se ali odbija sve poruke dok registry nije ready.
```

**Kako se update-a:**
```
1. python scripts/sync_tools.py   # pulls live Swagger from MobilityOne
   → writes processed_tool_registry.json
2. pytest tests/test_config_parity.py   # MORA prolaziti
3. Ako fails: hand-edit tool_data.json da doda nove alate
4. pytest opet
```

---

## File #3 — `context_param_schemas.json`

```
┌──────────────────────────────────────────────────────────────────┐
│  ŠTO JE TO                                                       │
│  Klasifikacijska pravila za API parametre.                       │
│  "Je li ovo person_id, tenant_id, vehicle_id, entity_id, ili    │
│   business param koji LLM treba?"                                │
└──────────────────────────────────────────────────────────────────┘
```

**Veličina:** 3 KB.

**Što sadrži:** klasifikacijska pravila (regex/exact match) za parametre. Korišteno tijekom **sync_tools.py** build-a — odlučuje hoće li `personId` biti automatski popunjen iz session-a ili pitano LLM.

**Tko ga čita:**

| File | Linija | Kad |
|---|---|---|
| `services/registry/swagger_parser.py` | [29](../services/registry/swagger_parser.py#L29) (CONFIG_PATH) | Module import (lazy via parser instantiation) |

Pri runtime-u sustav **ne čita** ovaj file direktno — parser ga koristi samo pri `sync_tools.py` regen-u processed_tool_registry-ja.

**Što se događa ako fali:**
```
🟢 GRACEFUL FALLBACK — parser uses hardcoded defaults.
   Log warning, sync_tools nastavlja s degraded klasifikacijom.
   Production nije pogođen (file se ne čita pri normal traffic-u).
```

**Kako se update-a:** Hand-edit. Reload sync_tools.py kad MobilityOne doda novi tip parametra.

---

## File #4 — `domain/path_entity_map.json`

```
┌──────────────────────────────────────────────────────────────────┐
│  ŠTO JE TO                                                       │
│  Mapping API path segmenata na hrvatske entitete.                │
│  "vehicles" → ("vozilo", "vozila")                               │
│  "expenses" → ("trošak", "troškova")                             │
│  ~340 mapiranja                                                  │
└──────────────────────────────────────────────────────────────────┘
```

**Veličina:** 21 KB.

**Tipičan zapis:**
```json
{
  "path_entity_map": {
    "vehicles": ["vozilo", "vozila"],
    "expenses": ["trošak", "troškova"],
    "cases": ["slučaj", "slučaja"],
    ...
  }
}
```

**Tko ga čita:**

| File | Linija | Kad |
|---|---|---|
| `services/registry/entity_mappings.py` | [14-25](../services/registry/entity_mappings.py#L14) (PATH_ENTITY_MAP) | Lazy via getter (post Fix #1b) |
| `services/registry/embedding_engine.py` | [147](../services/registry/embedding_engine.py#L147) | Build-time samo (sync_tools) |

**Što sustav dobije:**
- Pri **sync_tools.py** generaciji embedding teksta — pretvara engleski API path u hrvatske entitete da embedding hvata semantiku
- Production runtime: imported ali ne UPITUJE se (samo build-time)

**Što se događa ako fali (post Fix #1b):**
```
🟢 GRACEFUL DEGRADE — empty dict + logged error.
   Worker pokrene se. Production routing ne pogođen (build-time only).
   sync_tools.py regen bi proizveo slabije embedding tekstove.
```

**Kako se update-a:** Hand-edit kad MobilityOne doda novi entity path.

---

## File #5 — `linguistic/typo_synonyms.json` (HOT PATH!)

```
┌──────────────────────────────────────────────────────────────────┐
│  ŠTO JE TO                                                       │
│  Hrvatski slang/typo → kanonski oblik mapping.                  │
│  "auta" → "vozila"                                              │
│  "kvarova" → "kvar"                                             │
│  Koristi se ZA SVAKU poruku korisnika (L0 normalizacija).        │
└──────────────────────────────────────────────────────────────────┘
```

**Veličina:** 1 KB, ~27 mapiranja.

**Tipičan zapis:**
```json
{
  "synonym_map": {
    "auta": "vozila",
    "auto": "vozilo",
    "kvarova": "kvar",
    "rezerviraj": "rezervacija",
    ...
  }
}
```

**Tko ga čita:**

| File | Linija | Kad |
|---|---|---|
| `services/text_normalizer.py` | [40-69](../services/text_normalizer.py#L40) (SYNONYM_MAP) | Lazy via getter (post Fix #1a) |

**Što sustav dobije:**
- Pre-normaliziran upit ide u L3 router. "prikazi mi sva auta" → "prikazi mi sva vozila"
- Bez ovog file-a, router treba pogađati "auto"="vozilo" iz embedding similarity-ja

**Što se događa ako fali (post Fix #1a):**
```
🟢 GRACEFUL DEGRADE — empty dict, log error.
   Bot pokrene se. Slang normalizacija isključena.
   "Prikazi mi auta" više se ne normalizira u "vozila".
   Accuracy djelomično degradira ali bot odgovara.
```

**Kako se update-a:** Hand-edit kad uoči novi slang pattern. Promjena vidljiva tek nakon worker restart-a (module-level, ali post Fix #1a samo lru_cache treba čistiti).

---

## File #6 — `tenants/_default/tool_subset.json` ★ Phase E

```
┌──────────────────────────────────────────────────────────────────┐
│  ŠTO JE TO                                                       │
│  Lista alata dostupnih DEFAULT tenantu (svima koji nemaju       │
│  vlastiti override). 594 user-facing alata (od 950 ukupno).      │
└──────────────────────────────────────────────────────────────────┘
```

**Veličina:** ~10 KB.

**Tipičan zapis:**
```json
{
  "tenant_id": "_default",
  "description": "Default tool subset...",
  "source": "personas_strict in tool_data.json",
  "generated_at": "2026-05-16T05:24:00Z",
  "allowed_tool_ids": [
    "get_MasterData",
    "post_AddMileage",
    "get_MileageReports",
    ...594 ukupno
  ]
}
```

**Tko ga čita:**

| File | Linija | Kad |
|---|---|---|
| `services/router/catalog_scoper.py` | [124-155](../services/router/catalog_scoper.py#L124) (_tenant_subset) | Lazy + mtime invalidation (post Fix #2) |
| `services/v2/engine.py` | [1230](../services/v2/engine.py#L1230) | Constructor pass-a tenants_dir |

**Što sustav dobije:**
- Pri svakoj poruci, `scope(tenant_id, persona)` vraća ~50-150 alata umjesto 950
- Sibling rivalry strukturalno nestaje za driver short queries

**Što se događa ako fali:**
```
🟢 GRACEFUL FALLBACK — sve 950 alata postaje dostupno.
   Pre-Phase-E ponašanje. Routing accuracy pada nazad na 68.7%.
   Multi-tenant izoliranost izgubljena. Bot radi.
```

**Multi-tenant story:**
```
config/tenants/
├── _default/              ← fallback za svakog tenanta bez overridea
│   └── tool_subset.json
├── damir-uuid/            ← per-tenant override (opcionalno)
│   ├── tool_subset.json   ← drugačiji set alata
│   └── personas.json      ← phone → persona mapping
└── novi-klijent-uuid/     ← nový tenant = novi direktorij, no code change
    ├── tool_subset.json
    └── personas.json
```

**Kako se update-a:**
```bash
python scripts/generate_default_tenant_subset.py
# regen iz tool_data.json personas_strict tagova
```
ILI hand-edit `allowed_tool_ids` listu. Promjena se pikne **na sljedeću poruku** (mtime invalidation).

---

## File #7 — `tenants/{tenant_id}/personas.json` (opcionalno, ne postoji default)

```
┌──────────────────────────────────────────────────────────────────┐
│  ŠTO JE TO                                                       │
│  Phone → persona override mapping za specifični tenant.          │
│  Bez ovog file-a, svi su "driver" (najsigurniji default).        │
└──────────────────────────────────────────────────────────────────┘
```

**Veličina:** ~1-5 KB (ovisi koliko admin/manager-a tenant ima).

**Tipičan zapis:**
```json
{
  "385955087196": "admin",
  "385951234567": "manager",
  "385999999999": "driver"
}
```

**Tko ga čita:**

| File | Linija | Kad |
|---|---|---|
| `services/v2/identity.py` | [270](../services/v2/identity.py#L270) (_load_persona_overrides) | Lazy s mtime invalidation |

**Što sustav dobije:**
- `IdentitySnapshot.persona` postavljen na "manager" / "admin" za tagged phone-ove
- Inače "driver" (default)
- Persona ulazi u `CatalogScoper.scope()` → drugačiji set alata

**Što se događa ako fali:**
```
🟢 GRACEFUL — svi default na "driver".
   Manager/admin tagovi izgubljeni. Tenant radi ali svi su drivers.
```

**Kako se update-a:** Hand-edit. Promjena vidljiva **na sljedeću poruku** (mtime invalidation).

---

## Sažetak: failure modes svih 6 file-ova

| File | Missing | Malformed JSON | Schema-wrong |
|---|---|---|---|
| tool_data.json | 🔴 CRASH | 🔴 CRASH | 🟡 0 tools loaded |
| processed_tool_registry.json | 🟡 logged, /ready 503 | 🟡 logged | 🟡 logged |
| context_param_schemas.json | 🟢 fallback defaults | 🟢 fallback | 🟢 fallback |
| domain/path_entity_map.json | 🟢 empty + log *(post Fix #1b)* | 🟢 | 🟢 |
| linguistic/typo_synonyms.json | 🟢 empty + log *(post Fix #1a)* | 🟢 | 🟢 |
| tenants/_default/tool_subset.json | 🟢 all tools allowed | 🟢 fallback | 🟢 |
| tenants/{X}/personas.json | 🟢 all "driver" | 🟢 fallback | 🟢 |

**Jedini hard-fail je tool_data.json** — i to acceptable: bot je nemoguće raditi bez liste alata. CI gate (Fix #4) hvata malformed JSON prije merge-a.

---

## Sažetak: kako se ažurira što

| Scenarij | Akcija |
|---|---|
| MobilityOne dodaje novi API endpoint | `python scripts/sync_tools.py` → hand-edit tool_data.json za nove alate → pytest |
| Damir kaže "vozač treba još jedan alat" | Hand-edit tool_data.json (`personas_strict` add "driver") → regen default tool_subset |
| Novi tenant dolazi | `mkdir config/tenants/{uuid}/` → opcionalno custom tool_subset.json + personas.json |
| Admin u Damirovom tenantu mijenja se | Edit `config/tenants/{damir-uuid}/personas.json` → mtime invalidation pikne |
| Driver query "kvarova" ne pogađa | Hand-edit `config/linguistic/typo_synonyms.json` → restart worker |
| Sustav stao raditi nakon deploy-a | Provjeri `/ready` health endpoint, log za "tool_data.json crash" |

---

## Production safety status (post 4 fix-eva, 2026-05-16)

| Fix | Status | Verifikacija |
|---|---|---|
| #1a Lazy load typo_synonyms.json | ✅ Implementirano | Manual missing-file test prošao |
| #1b Lazy load path_entity_map.json | ✅ Implementirano | Manual missing-file test prošao |
| #2 mtime invalidation za tool_subset.json | ✅ Implementirano | 2 nova pytest-a zelena |
| #3 Path.cwd() → Path(__file__).resolve() | ✅ Implementirano | Resolve test iz /tmp prošao |
| #4 CI JSON validation gate | ✅ Implementirano | Local bash gate prošao |

**Total pytest: 1249 zelen / 0 fail / 5 skipped.**

Sve 3 production risk-a koje sam identificirao u audit-u sada su **mitigated**:
- 🔴 Import-time crash → 🟢 graceful degrade
- 🔴 Silent stale cache → 🟢 mtime invalidation
- 🟡 cwd dependency → 🟢 anchored paths

Sustav je sad **production-safe za Damir pilot**.
