# 11 — PODACI & STANJE (Postgres + Redis)

**Svrha**: Stalno (Postgres) i privremeno (Redis) stanje korisnika, razgovora, alata i auditiranja. Dva PostgreSQL korisnika (bot_user/admin_user) + sloj keširanja.

## Datoteke

| Datoteka | LOC | Status | Uloga |
|---|---|---|---|
| `database.py` | 137 | LIVE | Async SQLAlchemy engine s dual-user URL-ovima + connection pooling; init_db/close_db |
| `models.py` | 173 | LIVE | ORM za 6 tablica |
| `base.py` | 10 | LIVE | Shared `declarative_base` (izbjegava cikluse database↔models) |
| `alembic/versions/001_initial_schema.py` | 154 | LIVE | Inicijalna migracija: 6 tablica + indexi + dual-user GRANT |
| `alembic/versions/002_add_gdpr_consent_fields.py` | 47 | LIVE | +4 GDPR kolone na user_mappings |
| `alembic/versions/003_align_orm_models.py` | 215 | LIVE | Alignment migracija (timezone-aware, drop conversation_id FK, rename/drop kolone) — **DATA-DESTRUCTIVE** |
| `services/cache_service.py` | 282 | LIVE | Redis cache wrapper sa SafeJSONEncoder + stampede-zaštita + Lua increment |
| `services/v2/conversation_history.py` | 98 | LIVE | Redis FIFO 5 zadnjih turnova/telefon, TTL 30 min |
| `services/v2/atomic_io.py` | 42 | **DEV_ONLY** | Atomski JSON zapis (tmp + os.replace). Importan samo iz `scripts/` + testova, NE iz živog runtime-a |

> ⚠️ **atomic_io.py je DEV_ONLY**: koriste ga config-regeneracijske skripte (sync_tools.py itd.) i testovi. Nijedan živi modul (engine/worker/main) ga ne importa (potvrđeno grep-om). Prvi agent ga je krivo označio LIVE.

## Ulazne točke

| Simbol | Lokacija | Što radi |
|---|---|---|
| `get_database_url` | database.py:49 | BOT_ ili ADMIN_DATABASE_URL po SERVICE_ROLE |
| `create_engine_with_pool` | database.py:64 | Async engine, AsyncAdaptedQueuePool (prod), NullPool (test) |
| `init_db` | database.py:129 | Base.metadata.create_all (main.py lifespan) |
| `CacheService` | cache_service.py:48 | get/set/set_json/delete/invalidate/get_or_compute/increment |
| `ConversationHistoryStore` | conversation_history.py:37 | append/load/clear (max 5 turnova, TTL 30 min) |
| `atomic_write_json` | atomic_io.py:20 | tmp write → os.replace (atomic) |

## 6 PostgreSQL tablica (models.py)

| Tablica | Model @ linija | Ključne kolone |
|---|---|---|
| `user_mappings` | models.py:31 | id UUID, phone_number UNIQUE, api_identity (person_id, nullable=True u ORM), display_name, tenant_id, is_active, GDPR consent kolone, timestamps |
| `conversations` | models.py:56 | user_id FK, started_at, ended_at, status, flow_type, metadata JSON |
| `messages` | models.py:74 | conversation_id FK, role, content, timestamp, tool_name, tool_call_id, tool_result JSON |
| `tool_executions` | models.py:93 | tool_name, parameters, result, success, error_message, execution_time_ms (bez conversation_id FK) |
| `audit_logs` | models.py:108 | **admin-only** (bot nema pristup): user_id, action, entity_type/id, details JSON |
| `hallucination_reports` | models.py:133 | user_query, bot_response, user_feedback, reviewed, correction, category |

- **Dual-user GRANT** (001_initial:131-145): bot_user SELECT/INSERT/UPDATE/DELETE na user/conv/msg/tool + INSERT-only na hallucination_reports; REVOKE ALL na audit_logs. admin_user ALL.
- **GDPR** (002): gdpr_consent_given, gdpr_consent_at, gdpr_data_retention_days (default 365), gdpr_anonymized_at.

## Redis (samo state ovog podsustava)

- `v2_conv_history:{phone}` — FIFO lista, max 5 turnova, **TTL 30 min** (rpush→ltrim→expire). ConversationTurn(user, bot, bot_action).
- `lock:{cache_key}` — distributed lock za get_or_compute.

> Ostali Redis ključevi (stream, dlq, pending_*, rate_limit) pripadaju drugim podsustavima — vidi [00_PREGLED](00_PREGLED.md).

## Što NE radi

- Ne kreira Postgres korisnike (bot_user/admin_user pretpostavljaju se već postoje; migracije samo GRANT).
- Ne šifrira podatke u mirovanju (TLS-in-transit preko conn stringa).
- Conversation history nije source-of-truth (best-effort cache, 30 min); za long-term koristiti `messages` tablicu.
- Nema distribuiranih commitova Postgres↔Redis.

## Caveati

- **api_identity nesklad**: ORM nullable=True (models.py:38) vs migration 001 nullable=False (001:37). Migration 003 NE ispravlja → trebala bi dodatna migracija.
- **Migration 003 DATA-DESTRUCTIVE**: DROPS last_activity/message_count/state iz conversations + kolone iz messages. Prije pokretanja: pg_dump backup + maintenance window.
- **PII u conversation_history**: scrub je nakon dispatcha (engine) — bot vidi originalni PII u LLM kontekstu, samo Redis persistencija je scrubana (po dizajnu — LLM treba kontekst).
- SafeJSONEncoder: datetime→ISO, UUID→str, fallback str().
