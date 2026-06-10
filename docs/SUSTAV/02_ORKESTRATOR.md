# 02 — ORKESTRATOR / V2Engine (mozak)

**Svrha**: Centralni orkestrator koji usmjerava svaku poruku kroz diskretne provjere (L-1 do L1.5, L2a/L2b, Model A cascade), te unutar Model A cascade-a poziva L3 router, mutation gate, executor i formatter.

## Datoteka

| Datoteka | LOC | Status | Uloga |
|---|---|---|---|
| `services/v2/engine.py` | 2733 | LIVE | V2Engine klasa, `_dispatch_message` pipeline, svi pending-state handleri, `make_v2_engine_for_production` factory |

## Ulazne točke

| Simbol | Lokacija | Što radi |
|---|---|---|
| `V2Engine.process_message` | services/v2/engine.py:187 | Javni API: postavlja telemetrijski kontekst (correlation_id, turn_number), poziva `_dispatch_message`, dodaje obrt u conversation_history (PII-scrubban) |
| `V2Engine._dispatch_message` | services/v2/engine.py:237 | Srce orkestratora — sekvencijalna obrada kroz sve slojeve s prekidima |
| `make_v2_engine_for_production` | services/v2/engine.py:2422 | Factory: gradi potpuno povezan V2Engine iz infrastrukture (Redis, gateway, registry). Poziva ga worker.py:481 |

## Slojevi `_dispatch_message` (točan redoslijed + linije)

| Sloj | Lokacija | Što radi |
|---|---|---|
| L-1 Rate Limiter | engine.py:244 | Previše poruka (`v2:rl:*`) → cooldown poruka, prekid |
| L0.5 PII Scrubber | engine.py:250 | OIB/IBAN/telefon → placeholderi, redakcije u telemetriju |
| L0.6 Input Sanitizer | engine.py:272 | Prompt-injection blok (role-tagovi, "ignore previous", token-flood) |
| L0 Identity | engine.py:290 | telefon → person_id/tenant_id/vehicle_id (`v2:identity:{phone}`, 30s) |
| PENDING params | engine.py:293 | Ako se prikupljaju parametri, nastavi |
| PENDING mutation | engine.py:313 | Ako čeka Da/Ne potvrda, parsira i izvršava/odbija (anti-replay lock) |
| **"Nije točno" reoffer** | engine.py:328 | Ako korisnik kaže "nije točno", ponudi sljedeća 3 kandidata (vidi [14_REOFFER](14_REOFFER.md)) |
| PENDING clarify | engine.py:346 | Ako su top-3 kartice prikazane, parsira "1/2/3/ne" |
| L4 Flow nastavak | engine.py:366 | Aktivni flow (booking/mileage/case) → nastavi; detektira flow-switch i fresh-action |
| L0.7 Crisis | engine.py:417 (verifikator: ~425) | Suicidalni signal → Plavi telefon 116 123, prekid |
| L0.75 Negation | engine.py:435 (verif: ~441) | Samostalni "ne/nemoj" → ljubazno priznanje |
| L0.8 Multi-intent | engine.py:450 (verif: ~454) | "X i Y" → pitaj što prvo |
| L0.85 Meta-intents | engine.py:464 (verif: ~468) | "tko si ti" → inline odgovor |
| L1 Special intents | engine.py:477 (verif: ~478) | welcome / GDPR / handover / help (+ side_effects) |
| L1.5 Unknown phone gate | engine.py:499 | Nepoznat broj → enrollment poruka |
| L2a Intent type | engine.py:521 | LLM 4-way klasifikacija |
| Orphan confirm guard | engine.py:524 | Samo "Da/Ne" bez pendinga → "nemam aktivnu potvrdu" |
| L2b Driver basics | engine.py:549 | km/vozilo/registracija → cached snapshot |
| Model A action picker | engine.py:595 | Fallback: 4 akcije (POGLEDATI/UNIJETI/IZMIJENITI/IZBRISATI) — Turn 1 |

> **Točan redoslijed pending provjera je load-bearing**: params → mutation → reoffer → clarify → flow. Bilo koje preuređenje razbija state machine.

## Što je INTERNO (ne top-level sloj)

L3 (router), L6 (mutation gate), L7 (executor), L8 (formatter) **nisu** koraci u `_dispatch_message`. Pozivaju se unutar Model A cascade-a:
- `_resolve_pending_clarify` → L3 `router.route` → top-3 picker
- `_run_gate_and_execute` → mutation_gate → executor → formatter

## Ovisi o (depends_on)

Gotovo svi v2 moduli: rate_limiter, pii_scrubber, identity, intent_type, driver_basics, input_sanitizer, crisis_detector, negation_handler, multi_intent_detector, meta_intents, special_intents, flow_engine, executor, mutation_gate, formatter, pending_mutation, pending_clarify, pending_params, conversation_history, gdpr_audit, clarify_ui, param_ui, type_resolver, optional_extractor, api_error_translator, param_labeler, telemetry + router/* + formatter/llm_formatter.

## Config datoteke koje factory učitava

- `config/tool_data.json` — **single source of truth** (union-merge registry + intent_summaries + anchors; fail-fast RuntimeError ako missing/corrupt), engine.py:2552
- `config/processed_tool_registry.json` — fallback za `dependency_graph` (CRIT-2 fix, engine.py:2553)
- `config/param_labels_hr.json` — preloaded HR labele (optional)
- `config/risky_tools.json` — set alata s nepotpunom schemom (warn prefix), engine.py:2529
- `config/tenants/*` — per-tenant tool subsets (scoper)

## Što NE radi

- Ne gradi LLM router kandidate sam → `services/router/llm_router`.
- Ne izvršava API pozive direktno → `services/v2/executor`.
- Ne implementira flow DSL → `services/v2/flow_engine`.
- Ne radi mutation permissioning (HTTP 403 je u executoru/backendu, ne ovdje).
- Ne čuva raw user tekst u Redis (PII-scrub prije conversation_history.append).

## Caveati

- **Anti-replay lock** (`try_acquire_execution`) MORA biti atomičan — Infobip može retry "Da" ili korisnik double-tap → bez toga double-write/double-booking.
- **Crisis detection MORA biti prije negation_handlera** (inače "ne želim živjeti" → negation umjesto hotline). NAPOMENA: u stvarnom kodu crisis trči nakon identity + pending koraka, ne "odmah nakon PII scruba" kako tvrdi docstring crisis_detector.py:14.
- Param coercion (HR "12,5"→12.5) se primjenjuje samo na LLM putanji (`_coerce_llm_params`); param-ask putanja koristi `param_ui.parse_param_value`.
- Telemetrija + identity invalidate su best-effort (ne blokiraju korisnika ako Redis padne).
