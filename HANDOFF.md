# HANDOFF

A single-page orientation for a new owner inheriting this codebase. Pairs with `README.md` (quick-start) and `ARCHITECTURE.md` / `DEPLOYMENT.md` / `SECURITY.md` (deeper reference).

## What this project is

Croatian-language WhatsApp tool-retrieval bot for MobilityOne. Users send natural-language messages via Infobip; the bot retrieves the correct API tool out of ~950 candidates (FAISS + BM25 + LLM reranker + TFI fast-path) and invokes it with user-scoped parameters, then returns a rendered response in Croatian.

Stack:
- **API layer**: FastAPI (`main.py`, `webhook_simple.py`)
- **Worker**: asyncio `worker.py` consuming `whatsapp_stream_inbound` Redis stream; one message per consumer at a time
- **Storage**: Postgres (SQLAlchemy + Alembic migrations in `alembic/versions/`), Redis (async `redis.asyncio`)
- **Retrieval**: FAISS + BM25 hybrid in `services/unified_search.py`, populated from `config/processed_tool_registry.json`
- **LLM**: Azure OpenAI via `services/openai_client.py` (shared pool + rate limiter + circuit breaker)

## Running it

```
# local dev
cp .env.example .env                      # fill Azure OpenAI + Mobility creds
docker compose up -d redis postgres
alembic upgrade head
python main.py                            # API on :8000
python worker.py                          # worker consumes stream
```

Tests: `pytest tests/ --ignore=tests/benchmarks` — **2134 passing, 15 skipped, 0 failed, 0 errors**. Run has been validated both against mocks AND against a real Postgres (at head) + real Redis stack. The 15 skipped are live-API smoke tests gated behind `RUN_INTEGRATION_TESTS=1` (booking/mileage/case flows + webhook signature probes + graceful-shutdown) which require network reach to the MobilityOne dev API; they were not executed here because that host is not reachable from this sandbox.

Benchmark (against Azure OpenAI `m1-ai-dev`, both paraphrase seeds):

| Seed | Recalib Top-5 | Top-20 |
| ---- | ------------- | ------ |
| 42   | 71.3%         | 82.3%  |
| 1337 | 76.8%         | 82.6%  |

Both seeds pass baseline (≥69%); fixes did not regress retrieval.

## Operating playbook

- **Tenant onboarding**: Insert into `tenants` + `tenant_phones`; `services/tenant_resolver.py` caches phone→tenant mappings in Redis. Unknown phones are refused at the webhook edge — no worker load.
- **Tool registry rebuild**: `python scripts/sync_tools.py` — pulls Swagger from MobilityOne API, regenerates `config/processed_tool_registry.json` + `.cache/tool_embeddings.json`. Bump `CACHE_VERSION` in `services/registry/cache_manager.py` when upstream schema changes so stale caches are invalidated on startup.
- **Benchmarks**: `tests/benchmarks/test_tool_recognition.py` against two seeds (42 + 1337). Recalib Top-5 target 69-72% with the current ada-002 HR stack. See `MEMORY` context for why higher requires multilingual embedding swap.
- **Circuit breaker state**: Per-endpoint metrics at `GET /admin/circuits` (if admin router exposed). States: `closed`, `open`, `half_open`, `half_open_probing` (probe in flight).

## Security posture (what's hardened, what's a known limit)

Hardened in recent audit commits (see `git log --grep=security --grep=fix` on this branch):
- Infobip webhook HMAC-SHA256 verification — fails closed if `INFOBIP_SECRET_KEY` unset (commit `deb9a40` + earlier).
- SSRF hardening in `services/api_gateway.py`: netloc comparison, userinfo rejection, scheme enforcement.
- Circuit breaker half-open probe is serialized via `CircuitState.HALF_OPEN_PROBING` — no parallel dict that can drift.
- GDPR consent Redis keys sanitized via `_safe_redis_sender()` in `services/engine/__init__.py` — defends against key injection from webhook-controlled `from` field.
- CORS `allow_origins=['*']` gated on BOTH `DEBUG=true` AND non-production env.
- PII: webhook no longer logs raw message body (commit `deb9a40`); masking is done at log-site (`sender[-4:]`).

Known limits (document to buyer, not bugs):
- `tool_categories.json` missing → ranking degrades silently → now logs ERROR at startup (commit `c2b385c`) but still boots.
- `except Exception` in several fallback paths is intentional (Redis best-effort for consent cache, rag_scheduler pubsub reconnect). Wrapped with `exc_info=True` so stacks reach logs.

## Test suite

All unit and component tests pass on Python 3.14. Integration smoke tests (live API + Redis + DB) are opt-in:

```
RUN_INTEGRATION_TESTS=1 pytest tests/test_booking_flow.py tests/test_case_flow.py tests/test_mileage_flow.py
```

Regression guards for post-audit fixes live in `tests/test_audit_regressions.py`.

## Architectural debts resolved in this handoff sweep

| Debt | Commit | Note |
| --- | --- | --- |
| RouterAction enum replaces string literals | `025fee5` | Type-safe routing actions; `str`-mixin preserves comparisons |
| `CircuitState.HALF_OPEN_PROBING` collapses parallel flag dict | `7e9b7c3` | Single source of truth for probe serialization |
| Router clarify Redis → `redis.asyncio` | `0ca9bdc` | Drops `asyncio.to_thread` wrapper + `threading.Lock` |
| O(1) case-insensitive `resolve_tool_id` | `97bc38d` | 950-tool scan on router validation was O(N) |
| Dead one-shot scripts purged | `02267e0` | 15 files, 3265 LOC removed |
| CRITICAL security findings closed | `deb9a40` | PII leak, key injection, CORS wildcard |
| MAJOR observability findings | `c2b385c` | Silent-failure paths now log |
| Croatian date parsed as UTC | `1ad0189` | `danas`/`sutra` use `Europe/Zagreb` (fallback `+01` on tzdata-less hosts) |
| Phone-variation nondeterminism | `8dc3e4f` | Ambiguous lookups refuse; closes cross-account takeover vector |
| LLM JSON extractor greediness | `9967743` | Brace-depth scanner replaces `\{[\s\S]*\}` regex |
| Token refresh KeyError | `4a2d726` | Missing `access_token` → `GatewayError` + cooldown |
| Test suite cleanup | `4093fa2` | All 50 pre-existing failures/errors resolved; 12 new regression tests |
| SAFE_HEADERS hot-path | `df60d24` | Hoisted per-request set-build to module-level frozenset |

## Files the buyer should read first (in order)

1. `README.md` — setup + quick-start
2. `ARCHITECTURE.md` — component map
3. `config.py` — all tunables and required env vars
4. `main.py` — app wiring (middleware, routers, startup)
5. `webhook_simple.py` — inbound message edge
6. `worker.py` — worker loop + burst mode
7. `services/unified_router.py` — routing decisions (entry point for most debugging)
8. `services/engine/__init__.py` — message processing pipeline
9. `services/unified_search.py` — FAISS + BM25 hybrid
10. `SECURITY.md` + this file — operating context

## Sacred file

`config/tool_documentation.json` — do not hand-edit. Regenerate via `scripts/sync_tools.py` + manual synonym injection workflow documented in `MEMORY` context. Embedding cache is keyed on the hash of this file; silent edits will cause ranker drift.
