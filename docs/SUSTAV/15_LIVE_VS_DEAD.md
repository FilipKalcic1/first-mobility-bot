# 15 — LIVE vs DEAD inventar (što se stvarno izvršava)

> Ovaj dokument odgovara na "ima li nešto viška". Status je određen **ručnim grep-om import-grafa** od ulaznih točaka (main.py, worker.py, webhook_simple.py, engine.py factory), NE oslanjajući se na agente. Konflikti agenata (npr. confidence_gate, atomic_io) riješeni su stvarnim importima.

## Definicije

- **LIVE** — dosegnuto iz živog puta WhatsApp→worker→V2Engine (ili FastAPI app rute).
- **PARTIAL** — datoteka je LIVE ali samo dio simbola; ostatak mrtav/dev.
- **DEV_ONLY** — importaju ga samo `scripts/` i/ili `tests/`, nikad živi runtime.
- **DEAD** — realan kod, ali ga nitko (ni živi ni dev) ne importa, ili je tombstone.

---

## Ulazne točke (entry points)

| Simbol | Lokacija |
|---|---|
| FastAPI app + lifespan | main.py:201 / main.py:99 |
| webhook router | webhook_simple.py (montiran main.py:253) |
| worker | worker.py:250 (Worker.start) |
| V2Engine factory | engine.py:2422 (make_v2_engine_for_production), pozvan worker.py:481 |

---

## DEAD — realan kod koji se NE izvršava (3)

| Datoteka | LOC | Dokaz |
|---|---|---|
| `services/v2/confidence_gate.py` | 132 | Nema `import confidence_gate` u živom kodu; engine.py:519 ga spominje SAMO u komentaru ("replace the … confidence_gate path entirely"). Stari L5 gate, zamijenjen Model A + pending_clarify |
| `tool_routing.py` (root) | 30 | Tombstone — hardkodiran routing UKLONJEN u 11.0.4 |
| `analyze_orphans.py` (root) | 137 | Dev skripta za pronalaženje orphana; nije dio koda (DEV_ONLY/scratch) |

> Napomena: prvi liveness agent je `active_learning`, `anchor_audit`, `entity_mappings`, `atomic_io` označio DEAD; ručna provjera pokazuje da ih **scripts/** importaju → točniji status je **DEV_ONLY** (vidi dolje).

---

## DEV_ONLY — samo skripte/testovi (6)

| Datoteka | LOC | Importer |
|---|---|---|
| `services/v2/active_learning.py` | 348 | scripts/run_active_learning.py |
| `services/v2/anchor_audit.py` | 418 | scripts/audit_anchor_quality.py |
| `services/v2/atomic_io.py` | 42 | scripts/sync_tools.py i dr. (config regeneracija) + testovi |
| `services/registry/embedding_engine.py` | 463 | scripts/sync_tools.py |
| `services/registry/entity_mappings.py` | 548 | embedding_engine.py (sam DEV_ONLY) |
| `services/router/anchor_vocab.py` | 341 | scripts (generiranje anchora) |

---

## PARTIAL — LIVE ali samo dio (3)

| Datoteka | LOC | Što je LIVE / što nije |
|---|---|---|
| `services/queue_service.py` | 218 | LIVE: `read_stream` (worker), `create_consumer_group` (main). MRTVO: enqueue_inbound/outbound, dequeue_outbound, store_dlq, ack_message |
| `services/registry/swagger_parser.py` | 661 | Instanciran u ToolRegistry.__init__ (LIVE), ali parse_spec/_classify_context_parameter samo offline (scripts/sync_tools.py) |
| `services/v2/latency_ux.py` | 163 | LIVE: `chunk_for_whatsapp` (engine.py:183). MRTVO: hint_for_query, typing_watchdog, LatencyHint (samo testovi) |

---

## LIVE — u živom putu (sve ostalo)

### Root
| Datoteka | LOC |
|---|---|
| `main.py` | 427 |
| `worker.py` | 1479 |
| `webhook_simple.py` | 1044 |
| `middleware.py` | 84 |
| `config.py` | 339 |
| `database.py` | 137 |
| `models.py` | 173 |
| `base.py` | 11 (import database.py/models.py/alembic) |

### services/ (ne-v2)
| Datoteka | LOC | Importer |
|---|---|---|
| `services/admin_auth.py` | 78 | main.py admin rute |
| `services/api_capabilities.py` | 419 | worker.py:467 (initialize_capability_registry) |
| `services/api_gateway.py` | 726 | executor + worker + main |
| `services/cache_service.py` | 282 | main/worker lifespan |
| `services/config_loader.py` | 45 | text_normalizer (live chain) |
| `services/context_service.py` | 445 | main.py:134, worker.py:442 |
| `services/errors.py` | 281 | gateway/context/whatsapp/token |
| `services/openai_client.py` | 188 | engine factory (:2409-2410) |
| `services/pii_filter.py` | 79 | main.py:38-40, worker.py:27-29 |
| `services/rag_scheduler.py` | 717 | worker.py:58 |
| `services/redis_factory.py` | 22 | Redis klijent factory |
| `services/retry_utils.py` | 31 | api_gateway, token_manager |
| `services/security_headers.py` | 64 | main.py:227 |
| `services/tenant_resolver.py` | 365 | identity.py, engine factory |
| `services/text_normalizer.py` | 337 | formatter.py:365 |
| `services/token_manager.py` | 274 | api_gateway |
| `services/tool_contracts.py` | 183 | registry |
| `services/tracing.py` | 182 | webhook/worker/gateway/context/whatsapp |
| `services/whatsapp_service.py` | 662 | worker outbound |

### services/utils/
| Datoteka | LOC | Importer |
|---|---|---|
| `services/utils/pattern_registry.py` | 245 | context_service, whatsapp_service |
| `services/utils/vector.py` | 25 | anchor_index, driver_basics |

### services/router/ (LIVE osim anchor_vocab=DEV_ONLY, swagger nije ovdje)
anchor_index.py (200), catalog_scoper.py (211), llm_router.py (376), tool_schema_builder.py (267).

### services/registry/ (LIVE)
`__init__.py` (226), `tool_store.py` (107). (swagger_parser=PARTIAL, embedding_engine/entity_mappings=DEV_ONLY.)

### services/formatter/ (LIVE)
`llm_formatter.py` (205).

### services/v2/ (LIVE — sve osim confidence_gate=DEAD, active_learning/anchor_audit/atomic_io=DEV_ONLY, latency_ux=PARTIAL)
engine, identity, executor, flow_engine, formatter, intent_type, driver_basics, rate_limiter, pii_scrubber, input_sanitizer, crisis_detector, negation_handler, multi_intent_detector, meta_intents, special_intents, output_sanitizer, mutation_gate, pending_mutation, pending_clarify, pending_params, param_ui, param_labeler, optional_extractor, type_resolver, clarify_ui, conversation_history, telemetry, gdpr_audit, api_error_translator, azure_rate_guard, cache_invalidation.

### alembic/
001_initial_schema.py, 002_add_gdpr_consent_fields.py, 003_align_orm_models.py — LIVE (migracije).

---

## Sažetak

| Status | Broj datoteka (približno) |
|---|---|
| LIVE | ~55 |
| PARTIAL | 3 (queue_service, swagger_parser, latency_ux) |
| DEV_ONLY | 6 (active_learning, anchor_audit, atomic_io, embedding_engine, entity_mappings, anchor_vocab) |
| DEAD | 3 (confidence_gate, tool_routing, analyze_orphans) |

**Što ovo znači za Damira**: jezgra koja obrađuje svaku WhatsApp poruku je LIVE i čvrsto povezana. "Mrtve" datoteke su (a) zamijenjene komponente (confidence_gate — stari routing), (b) offline build-alati (embedding/anchor regeneracija), (c) tombstones. Nijedna DEAD/DEV_ONLY datoteka se ne izvršava pri obradi poruke — njihovo postojanje ne utječe na ponašanje bota, ali ih treba znati da se ne dokumentiraju kao da rade.
