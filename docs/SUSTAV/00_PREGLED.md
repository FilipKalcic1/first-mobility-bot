# 00 — PREGLED SUSTAVA (MobilityOne WhatsApp bot)

> Generirano 2026-06-09 iz živog koda. Svaka tvrdnja je provjerena protiv stvarnih datoteka (file:line). Ako neki dio ne odgovara kodu — to je greška opisa, prijavi je.
>
> **Metodologija**: 15 paralelnih agenata pročitalo je svaki podsustav + nezavisni verifikator re-čitao kod i lovio krive/missing/izmišljene tvrdnje. Liveness (LIVE/DEAD) dodatno provjeren ručnim grep-om import-grafa (ne oslanja se na agente).

---

## Što je ovo

Hrvatski WhatsApp bot za upravljanje voznim parkom (MobilityOne). Vozač/manager pošalje poruku na WhatsApp ("kolika mi je km", "rezerviraj auto sutra 9-15", "prijavi kvar"), bot razumije namjeru, pronađe pravi API alat (od ~950), prikupi parametre, potvrdi mutaciju i pozove MobilityOne API te vrati odgovor na hrvatskom.

- **LLM**: Azure OpenAI `gpt-4o-mini` (chat) + `text-embedding-ada-002` (embeddings)
- **Backend**: MobilityOne REST API (OAuth2 client_credentials, multi-tenant preko `x-tenant`)
- **Kanal**: Infobip WhatsApp Business API (webhook in, REST out)
- **Infra**: FastAPI (API proces) + zaseban worker proces + Redis (stream/cache/state) + PostgreSQL (user mappings, audit)

---

## Put poruke kroz sustav (high-level)

```
WhatsApp korisnik
   │  (Infobip šalje webhook)
   ▼
[01 EDGE] webhook_simple.py  ── HMAC verify → dedup → xadd u Redis stream
   │
   ▼  whatsapp_stream_inbound (Redis stream, consumer grupa "workers")
[01 EDGE] worker.py  ── XREADGROUP petlja → idempotency lock → _process_message
   │
   ▼  V2Engine.process_message(phone, text)
[02 ORKESTRATOR] services/v2/engine.py :: _dispatch_message — sekvencijalni slojevi:
   │
   ├─ L-1  [04] rate_limiter          (previše poruka → cooldown)
   ├─ L0.5 [04] pii_scrubber          (OIB/IBAN/telefon → [REDACTED])
   ├─ L0.6 [04] input_sanitizer       (prompt-injection blok)
   ├─ L0   [03] identity.resolve      (telefon → person/tenant/vozilo)
   ├─ PENDING nastavci (po redu): params → mutation → "nije točno" reoffer → clarify → flow
   ├─ L0.7 [04] crisis_detector       (suicidalni signal → Plavi telefon)
   ├─ L0.75[04] negation_handler      ("nemoj/odustani")
   ├─ L0.8 [04] multi_intent_detector ("X i Y" → pitaj što prvo)
   ├─ L0.85[04] meta_intents          ("tko si ti")
   ├─ L1   [04] special_intents       (welcome / GDPR / handover / help)
   ├─ L1.5      unknown phone gate     (nepoznat broj → enrollment poruka)
   ├─ L2a  [05] intent_type.classify  (4-way: pitanje-o-sebi / akcija / flow / drugo)
   ├─ L2b  [05] driver_basics.match   (km/registracija/vozilo → cached snapshot, BEZ routinga)
   ├─ L4   [06] flow                  ("rezerviraj/upiši km/prijavi kvar" → state machine)
   └─ Model A [05/07] action picker → L3 router → top-3 picker → param-ask → mutation gate → executor → formatter
   │
   ▼
[08 EXECUTOR] executor.py → api_gateway.py → MobilityOne API (Bearer + x-tenant + Idempotency-Key)
   │
   ▼
[09 FORMATTER] llm_formatter (JSON → HR tekst) ILI api_error_translator (4xx → HR)
   │
   ▼  enqueue_outbound
[01 EDGE] worker.py outbound petlja → whatsapp_service.send → Infobip → WhatsApp korisnik
```

**Ključno za razumijevanje**: slojevi **L-1 do L2b + L4** su diskretne provjere u `_dispatch_message`. Ali **L3 (router), L6 (mutation gate), L7 (executor), L8 (formatter) NISU top-level slojevi** — pozivaju se INTERNO unutar "Model A" 3-turn cascade-a (action picker → tool picker → execute), ne kao dispatch koraci. (Ovo je verifikator ispravio kao čestu zabludu.)

---

## Dvije/tri grane obrade (gdje poruka završi)

| Grana | Kad | Routing? | Pouzdanost |
|---|---|---|---|
| **[A] Vozačke prečice (L2b)** | "kolika km", "registracija", "moje vozilo" | NE — cached snapshot | ~visoka (deterministički) |
| **[B] Flow (L4)** | "rezerviraj", "upiši km", "prijavi kvar" | NE — hardkodiran tool | ~visoka (deterministički) |
| **[C] Model A cascade** | sve ostalo | DA — L3 LLM router (1 od ~950) | ~slabija (mjereno ~35% na nasumičnom uzorku, ~90% na driver-rutini) |

[A] i [B] namjerno zaobilaze routing. [C] je generički put za cijeli katalog.

---

## Indeks dokumenata

| # | Dokument | Podsustav | Glavne datoteke |
|---|---|---|---|
| 01 | [EDGE_IO](01_EDGE_IO.md) | Ulaz/izlaz WhatsApp ↔ bot | webhook_simple.py, worker.py, main.py, whatsapp_service.py |
| 02 | [ORKESTRATOR](02_ORKESTRATOR.md) | V2Engine dispatch pipeline | services/v2/engine.py |
| 03 | [IDENTITET](03_IDENTITET.md) | telefon → osoba/tenant/vozilo | identity.py, tenant_resolver.py |
| 04 | [GUARDS](04_GUARDS.md) | pred-routing zaštite | rate_limiter, pii_scrubber, crisis_detector, … |
| 05 | [ROUTING](05_ROUTING.md) | NL → 1 od 950 alata | intent_type, driver_basics, anchor_index, llm_router |
| 06 | [FLOWS](06_FLOWS.md) | booking/mileage/case automati | flow_engine.py |
| 07 | [INTERAKCIJA](07_INTERAKCIJA.md) | clarify + params + mutation gate | clarify_ui, param_ui, mutation_gate, pending_* |
| 08 | [EXECUTOR_GATEWAY](08_EXECUTOR_GATEWAY.md) | API izvršavanje | executor.py, api_gateway.py, token_manager.py |
| 09 | [FORMATTER](09_FORMATTER.md) | JSON → HR poruka | llm_formatter.py, formatter.py, api_error_translator.py |
| 10 | [REGISTRY](10_REGISTRY.md) | baza 950 alata | registry/, tool_contracts.py |
| 11 | [PODACI_STANJE](11_PODACI_STANJE.md) | Postgres + Redis state | database.py, models.py, alembic/, conversation_history.py |
| 12 | [OBSERVABILITY](12_OBSERVABILITY.md) | telemetrija, GDPR audit, tracing | telemetry.py, gdpr_audit.py, tracing.py |
| 13 | [CONFIG_SIGURNOST](13_CONFIG_SIGURNOST.md) | konfiguracija + sigurnost | config.py, admin_auth.py, openai_client.py |
| 14 | [REOFFER](14_REOFFER.md) | "nije točno" feedback petlja | engine.py (_handle_reoffer), pending_clarify.py |
| 15 | [LIVE_VS_DEAD](15_LIVE_VS_DEAD.md) | inventar živih i mrtvih datoteka | (cijeli services/ + root) |

---

## Dva procesa (bitno)

1. **API proces** (`main.py`) — FastAPI. Prima webhook, montira ga pod `/webhook`, izlaže `/health`, `/ready`, `/admin/cache-invalidate`. **NE pokreće V2Engine.**
2. **Worker proces** (`worker.py`) — čita Redis stream, gradi V2Engine (`make_v2_engine_for_production`), obrađuje svaku poruku, šalje odgovor. **Ovdje živi sav bot mozak.**

Komunikacija: API proces gura poruke u Redis stream `whatsapp_stream_inbound`; worker ih čita preko consumer grupe `workers`.

---

## Glavni Redis ključevi (pregled — detalji u pojedinim dokumentima)

| Ključ | Vlasnik | TTL | Svrha |
|---|---|---|---|
| `whatsapp_stream_inbound` | edge | maxlen 100k | inbound stream (webhook→worker) |
| `whatsapp_outbound` | edge | — | outbound queue (worker→Infobip) |
| `wh_dedup:{message_id}` | edge | 60s | webhook dedup |
| `msg_lock:{sender}:{message_id}` | edge | 300s | idempotency lock |
| `v2:identity:{phone}` | identity | 30s | cached snapshot |
| `tenant_phone:{e164}` | identity | 300s | tenant lookup cache |
| `v2:rl:m:{phone}` / `v2:rl:h:{phone}` | guards | 60s / 3600s | rate-limit buckets |
| `v2:pending_mut:{phone}` | interakcija | 300s | confirm dialog |
| `v2:pending_mut_exec:{phone}` | interakcija | 30s | anti-replay exec lock |
| `v2_pending_clarify:{phone}` | interakcija | 300s | top-3 picker + reoffer state |
| `v2_pending_params:{phone}` | interakcija | 300s | param collection state |
| `v2:flow:{phone}` | flows | 600s | flow state machine |
| `v2_conv_history:{phone}` | data | 30min | zadnjih ~5 turnova |
| `routing:accuracy_log:{tenant}` | observability | 30d | telemetrija (1000 zadnjih) |
| `gdpr:requests:{tenant}` / `handover:requests:{tenant}` | observability | 90d / 30d | audit |
| `mobility:access_token` | executor | ~token expiry | OAuth token cache |
| `api_err_translate:{hash}` | formatter | 3600s | 4xx prijevod cache |

> **Napomena o nekonzistentnom imenovanju** (verificirano): neki ključevi koriste dvotočku (`v2:pending_mut:`, `v2:flow:`, `v2:rl:`), a neki podvlaku (`v2_pending_clarify:`, `v2_pending_params:`, `v2_conv_history:`). To je povijesna nedosljednost, ne bug.

---

## Honest caveati o cijelom sustavu

- **Routing accuracy je strop** ~35% na nasumičnom uzorku 950 alata (mjereno cascade testom 2026-05-30); driver-rutina (~10 čestih alata) ~90%. Glavni izvor greške: sibling collisions (`delete_X` vs `delete_X_id`). Detalji u `docs/REAL_ACCURACY_TEST_2026-05-30.md`.
- **Multi-tenant bottleneck**: identity uvijek queira `/Persons` pod jednim `MOBILITY_TENANT_ID`. Za Damir pilot (1 tenant) OK; za 1000+ tenanta treba re-arhitektura.
- **Mrtav kod postoji**: nekoliko datoteka (confidence_gate, active_learning, anchor_audit, …) su realan kod koji NIJE u živom putu — vidi [15_LIVE_VS_DEAD](15_LIVE_VS_DEAD.md). To NIJE greška opisa nego stvarno stanje repozitorija.
