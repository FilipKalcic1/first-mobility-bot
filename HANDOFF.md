# Handoff

Orientation for a new maintainer. Pair with [ARCHITECTURE.md](ARCHITECTURE.md), [DEPLOYMENT.md](DEPLOYMENT.md) and the per-subsystem deep-dives in [docs/SUSTAV/](docs/SUSTAV/00_PREGLED.md).

## First hour

1. Read [docs/SUSTAV/00_PREGLED.md](docs/SUSTAV/00_PREGLED.md) — the verified map of the live system (generated from code, each claim has file:line).
2. Skim [config.py](config.py). Every env var that matters is there with defaults.
3. `git lfs pull` — `config/tool_data.json` (3.8 MB, the routing source of truth) is in Git LFS. Without it the worker refuses to start.
4. Run the suite: `pytest` (env vars from `.github/workflows/ci.yml`). Baseline must be green.

## First day

- Trace a message end-to-end: `webhook_simple.py` (HMAC → dedup → Redis stream) → `worker.py` (XREADGROUP → idempotency lock → engine → outbound queue) → `services/v2/engine.py::_dispatch_message` (the layered brain) → `services/v2/executor.py` → `services/api_gateway.py` (OAuth + x-tenant + Idempotency-Key).
- Read `services/v2/engine.py` top docstring — the layer order is the contract. Pending-state continuations (params → mutation → reoffer → clarify → flow) run BEFORE fresh routing.
- Routing is the **Model A 3-turn cascade**: Turn 1 saves the query + shows the universal action picker (POGLEDATI/UNIJETI/IZMIJENITI/IZBRISATI); Turn 2 runs the scoped L3 router (anchor top-50 → gpt-4o-mini tool-call) and shows a top-3 tool picker; Turn 3 collects params → mutation gate → execute. There is **no LLM auto-execute**.

## Working on the tool registry

1. Edit upstream Swagger, then `python scripts/sync_tools.py` — regenerates `config/processed_tool_registry.json`.
2. `pytest tests/test_config_parity.py` — fails loudly if `tool_data.json` drifted from the registry (new tools missing anchors/intent summaries).
3. Hand-edit `config/tool_data.json` for new tools (see the runbook at the top of `scripts/sync_tools.py` — LLM regen of anchors was tried and verified worse).
4. Anchor embeddings cache (`tests/benchmarks/router_anchor_cache.json`) is content-fingerprinted — it rebuilds itself when anchors change.

## What lives where (cheat sheet)

| Question | Look here |
| --- | --- |
| "Why is the bot routing this query wrong?" | `services/router/llm_router.py` + `services/router/anchor_index.py`; live decisions: `GET /webhook/whatsapp/routing-log?token=…` |
| "Why is a required field missing / wrongly asked?" | `services/v2/engine.py` (`_compute_missing_required`, `_resolve_pending_params`) + registry `dependency_source` |
| "Why did the API call 400?" | `services/api_gateway.py` (request build) + `services/v2/api_error_translator.py` (what the user saw) |
| "Why did the user get silence?" | `worker.py` outbound loop + `dlq:inbound` / `dlq:outbound` Redis lists |
| "Where is consent stored?" | `user_mappings` table, migration `002` |
| "Which files are live vs dead?" | [docs/SUSTAV/15_LIVE_VS_DEAD.md](docs/SUSTAV/15_LIVE_VS_DEAD.md) |

## People and process

- Architecture directives and standards: **Damir (Principal Architect)**.
- Implementation and maintenance: **Filip**.
- All architectural changes require sign-off before merging.

## Non-negotiables

1. PII never leaves a masking boundary (see [SECURITY.md](SECURITY.md)) — scrub before any LLM prompt, before conversation history, before logs.
2. Tenant isolation is strict: no env-default tenant fallback for user-scoped calls; `TenantId` comes from the user's resolved identity or the call is refused.
3. Mutations always pass the confirm gate (Da/Ne) and the anti-replay execution lock. A digit or an ambiguous reply must never blind-execute.
4. Never commit raw `.env` or unsealed secrets.
5. The benchmark scripts under `scripts/bench_*.py` are the authority on routing accuracy — measure before and after any retrieval change.
