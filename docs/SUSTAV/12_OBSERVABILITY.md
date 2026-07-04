# 12 — OBSERVABILITY (telemetrija, GDPR audit, tracing)

**Svrha**: Bilježi jednu strukturiranu telemetrijsku event po routing-odluci, vodi GDPR/handover audit trag i opcionalni OpenTelemetry tracing. Sve best-effort, nikad ne blokira korisnika. (Offline analize — active learning + anchor QA — OBRISANE u Fazi 0; sirova telemetrija ostaje.)

## Datoteke

| Datoteka | LOC | Status | Uloga |
|---|---|---|---|
| `services/v2/telemetry.py` | 528 | LIVE | TelemetryEvent + 5 sinkova + TelemetryLogger.from_env. Ključ `routing:accuracy_log:{tenant}` |
| `services/v2/gdpr_audit.py` | 131 | LIVE | GdprAuditStore: append-only GDPR delete/export + handover u Redis |
| `services/tracing.py` | 181 | LIVE | OpenTelemetry setup. No-op kad OTEL_ENABLED=false (default) |
| `services/v2/latency_ux.py` | 162 | PARTIAL | Samo `chunk_for_whatsapp` je LIVE; ostalo (hint_for_query, typing_watchdog) samo testovi |
| ~~`services/v2/active_learning.py`~~ | 347 | **OBRISANO (Faza 0)** | Offline-analitika ugašena; obrisan sa skriptom `run_active_learning.py` i testom |
| ~~`services/v2/anchor_audit.py`~~ | 417 | **OBRISANO (Faza 0)** | Statički QA anchora ugašen; obrisan sa skriptom `audit_anchor_quality.py` i testom |

## Ulazne točke

| Simbol | Lokacija | Što radi |
|---|---|---|
| `TelemetryLogger.from_env` | telemetry.py:449 | Tvornica logera iz env (V2_TELEMETRY, V2_TELEMETRY_BACKEND). Poziva engine.py:2470 |
| `TelemetryLogger.log` | telemetry.py:485 | Fan-out 1 eventa na sve sinkove (izolacija grešaka). Kroz `engine._log_telemetry`, ~19 poziva |
| `set_request_context` | telemetry.py:106 | Per-request contextvars (correlation_id, turn_number). engine.py:209 |
| `GdprAuditStore.record_gdpr_request` | gdpr_audit.py:47 | LPUSH GDPR zahtjeva. engine._handle_special_side_effects (engine.py:737) |
| `GdprAuditStore.record_handover_request` | gdpr_audit.py:73 | LPUSH handover. engine.py:745 |
| `get_tracer` / `trace_span` | tracing.py:85 / :105 | Tracing ulaz (webhook/worker/api_gateway/…). No-op kad OTEL off |
| `chunk_for_whatsapp` | latency_ux.py:69 | Dijeli dugi tekst na WhatsApp komade. Poziva se iz `engine.process_message_chunked` (funkcija @ engine.py:172, lazy import + poziv chunk_for_whatsapp @ :183-185) |

## Ključne komponente

| Simbol | Lokacija | Što radi |
|---|---|---|
| `TelemetryEvent` | telemetry.py:148-186 | 13 polja: tenant_id, correlation_id, turn_number, query_scrubbed, is_negation, tool_picked, confidence, competitors, clarify, error, latency_ms, redactions, **correction** (measure-first 2026-06-10: `{wrong_tool, correct_tool}` golden-set labela, set samo na "nije točno" reoffer korekciju). `to_record()` izbacuje None/''/[]/{}; zadržava 0/False |
| Sinkovi | telemetry.py:205-424 | StdoutJsonSink (prod primarni→Log Analytics), RedisSink (bounded lista), BufferedAsyncFileSink (dev), FileSink (legacy, nije u from_env), NullSink (disabled) |
| from_env selekcija | telemetry.py:448-479 | V2_TELEMETRY=0→Null. Default `stdout+redis`: Stdout+Redis (Redis samo ako klijent proslijeđen). U prod aktivni: **StdoutJsonSink + RedisSink** |
| Redis key + LTRIM/TTL | telemetry.py:63-80, :374-384 | `routing:accuracy_log:{tenant_id}` (ili legacy unscoped). Pipeline: LPUSH + LTRIM 0..999 (1000 zadnjih) + EXPIRE 30 dana |
| ~~4 extract_* analize~~ | ~~active_learning.py~~ | **OBRISANO (Faza 0)** — telemetrija (`routing:accuracy_log*`) ostaje; analize po potrebi ručno nad Redis podacima |
| ~~5 flag detektora~~ | ~~anchor_audit.py~~ | **OBRISANO (Faza 0)** |
| `_handle_special_side_effects` | engine.py:712-755 | Most special_intents → GdprAuditStore (record_gdpr/handover) |

## Redis ključevi

- `routing:accuracy_log:{tenant_id}` — telemetrija (LPUSH+LTRIM 0..999+EXPIRE 30d)
- `routing:accuracy_log` — legacy unscoped (evente bez tenanta)
- `gdpr:requests:{tenant_id}` — GDPR delete/export audit (LPUSH+LTRIM 0..499+EXPIRE 90d)
- `handover:requests:{tenant_id}` — handover audit (EXPIRE 30d)

## Admin čitači (webhook_simple.py)

- `/whatsapp/routing-log?tenant=…` (webhook_simple.py:784) — LRANGE routing log (admin token, 404 bez tokena, 400 bez tenanta)
- `/admin/gdpr-requests` (webhook_simple.py:847) — čita gdpr/handover liste

## Što NE radi

- Ne radi PII scrubbing — pretpostavlja da je pozivatelj već redigirao query (telemetrija dobiva `query_scrubbed`).
- **Ne pohranjuje phone/phone_hash** u TelemetryEvent (privatnost) → offline analize ne mogu precizno povezati Turn N→N+1 za istog korisnika (tenant-scoped aproksimacija).
- gdpr_audit NE izvršava brisanje/export — samo bilježi zahtjev; operator djeluje ručno (GDPR čl. 17/20).
- tracing ne emitira spanove osim ako OTEL_ENABLED=true.
- Telemetrija nikad ne blokira/ruši zahtjev (greške sinka izolirane).

## Caveati

- **active_learning + anchor_audit OBRISANI u Fazi 0** (bili DEV_ONLY, nisu bili na webhook→worker→V2Engine putu); sirova telemetrija u Redisu netaknuta.
- **latency_ux PARTIAL**: samo `chunk_for_whatsapp` LIVE; hint_for_query/typing_watchdog/LatencyHint samo testovi. Stvarna funkcija je `typing_watchdog` (latency_ux.py:119), ali **modul docstring (latency_ux.py:16) zastario** — referencira nepostojeću `make_typing_watchdog`.
- tracing LIVE po importu ali funkcionalno no-op u defaultnoj prod (OTEL_ENABLED=false).
- TelemetryEvent ima 13 polja (12 + `correction` dodan 2026-06-10); docstring (telemetry.py:9) zastario ("11 fields"). NAPOMENA: schema-primjer u docstringu (telemetry.py:13) ipak prikazuje `tenant_id`; `redactions` i `correction` nisu u tom primjeru.
- **Golden-set harvester** (`scripts/build_golden_set.py`, measure-first): čita `routing:accuracy_log:{tenant}` + `correction` evente → `tests/benchmarks/golden_set.json` (corrected=HIGH iz reoffer korekcija, accepted_weak=LOW iz neporeknutih pickova). Dormant dok nema live prometa.
- FileSink postoji ali nije izložen kroz from_env (samo test).
