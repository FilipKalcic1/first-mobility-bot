# Architecture

Single Docker container running FastAPI **api** for Infobip webhooks + asyncio **worker** that drains the Redis queue and runs the routing pipeline. Postgres + Redis are shared dependencies. No direct RPC between api and worker.

## Request lifecycle

```
WhatsApp → Infobip → POST /webhook (FastAPI api)
    → HMAC verify (webhook_simple.py)
    → enqueue job on Redis stream
api returns 200 immediately.

worker.py (consumer loop)
    → V2Engine.process_message
    → response → Infobip outbound → WhatsApp
```

## Routing pipeline

`services/v2/engine.py` orchestrates 27 ordered layers. Only L2a + L3 + L8 use LLM; everything else is deterministic Python + Redis.

| Layer | File | Purpose |
|---|---|---|
| L-1 Rate limiter | `services/v2/rate_limiter.py` | Per-phone Redis token bucket (atomic Lua) |
| L0.5 PII Scrubber | `services/v2/pii_scrubber.py` | OIB/IBAN/email/phone redaction before LLM |
| L0.6 Input sanitizer | `services/v2/input_sanitizer.py` | Prompt-injection guard |
| L0 Identity | `services/v2/identity.py` | Phone → personId via MobilityOne API, cached 30s |
| L1 State continuations | `pending_mutation.py`, `pending_clarify.py`, `flow_engine.py` | Resume mid-conversation flows |
| L0.7 Crisis detector | `services/v2/crisis_detector.py` | Ethical hotline redirect (runs FIRST among inline detectors) |
| L0.75 Negation | `services/v2/negation_handler.py` | Standalone "ne / nemoj / odustani" |
| L0.8 Multi-intent | `services/v2/multi_intent_detector.py` | Split detection (2+ intents in one msg) |
| L0.85 Meta-intents | `services/v2/meta_intents.py` | Self-reference, bug report, undo |
| L1 Special intents | `services/v2/special_intents.py` | GDPR / welcome / handover (with side-effect queue) |
| L1.5 Unknown-phone gate | `services/v2/engine.py` inline | Reject unregistered phones |
| L2a Intent type | `services/v2/intent_type.py` | 4-way classifier (1 LLM call) |
| L2b Driver basics | `services/v2/driver_basics.py` | Embedding anchor match → serve from cached MasterData |
| L4 Flows | `services/v2/flow_engine.py` | booking / mileage / case state machines (keyword-gated) |
| Model A cascade | `services/v2/engine.py` + `services/v2/pending_clarify.py` | Turn 1 action picker → Turn 2 scoped L3 router → Turn 3 tool pick (no auto-execute) |
| L3 LLM Router | `services/router/llm_router.py` | anchor top-50 → gpt-4o-mini tool-call (runs inside Turn 2) |
| Param collection | `services/v2/pending_params.py` + `services/v2/param_ui.py` | Ask missing required params, offer optionals, HR date/number parsing |
| L6 Mutation gate | `services/v2/mutation_gate.py` | Confirm dialog for POST/PUT/PATCH/DELETE + anti-replay exec lock |
| L7 Executor | `services/v2/executor.py` + `services/api_gateway.py` | HTTP call to MobilityOne with circuit breaker, OAuth, tenant header |
| L8 LLM Formatter | `services/formatter/llm_formatter.py` | Backend JSON → Croatian reply |

## L3 router internals (services/router/)

```
LLMRouter.route(query, identity_summary, conversation_history)
  Stage A — AnchorIndex.top_k(query, k=50)
            → cosine over 950 tools × ~12 anchor phrases each
            → returns [(tool_id, score), ...] sorted desc
  Stage B — ToolSchemaBuilder.build_for_tools(top50, tkb)
            → OpenAI tools=[] schema with parameter descriptions
            → 3 oversized tool names aliased via SHA1
  Stage C — LLM tool-call
            → gpt-4o-mini, tool_choice="required", temp 0
            → retries with backoff on 429/5xx (5s/15s/25s + jitter)
  Stage D — Validate
            → operation_id ∈ top-50 (hallucination guard)
            → required user_input params → missing list
            → heuristic confidence from anchor_score + missing
  → RouterResult{tool_id, params, confidence, anchor_score, alternatives, missing_required}
```

## L8 formatter internals (services/formatter/)

```
LLMFormatter.format(query, tool_id, api_data, identity_summary)
  1. output_sanitizer.sanitize(api_data)
     → defang [SYSTEM:...] / "ignore previous" in attacker-controlled fields
     → truncate strings > 1000 chars, max recursion 6 levels
  2. Prune for token budget (≤6000 chars)
     → list → first 15 rows + explicit `ukupno_stavki` count (LLM never
       reports a truncated count as the total)
     → enveloped list ({"Data": [...]}) → inner list pruned in place
     → dict + registry.output_keys → project to subset
  3. gpt-4o-mini call (temp 0, max 500 tokens, strict grounding prompt)
  4. PIIScrubber on output (defense in depth — catches LLM-echoed OIBs)
  → FormatResult{text, error, truncated}
```

## Data stores

- **Postgres** — conversations, user profiles, GDPR consent, hallucination reports, cost ledger.
- **Redis** — Infobip job queue (stream `whatsapp_stream_inbound`), outbound queue (`whatsapp_outbound`), session context, identity cache, pending mutation/clarify state, flow state, rate-limit counters, telemetry tap (`routing:accuracy_log`).
- **Anchor cache** — `tests/benchmarks/router_anchor_cache.json` — content-fingerprinted vector cache, rebuilt on anchor data change.

## Config files (production-active)

| File | Purpose |
|---|---|
| `config/tool_data.json` | **Single source of truth** (Git LFS, 3.8 MB) — 950 tools × {method, path, parameters, intent_summary, use_when, do_not_use_when, anchors} |
| `config/processed_tool_registry.json` | Swagger-derived registry — parameters, locations, dependency_source, output_keys, dependency_graph |
| `config/context_param_schemas.json` | Param classification rules (person_id / tenant_id auto-injected) |
| `config/risky_tools.json` | Tools with incomplete body schemas → confirm-time warning |
| `config/entity_translations_hr.json` | English entity → Croatian name |
| `config/linguistic/` | Croatian typo / slang mapping for text_normalizer |
| `config/domain/` | English path → Croatian entity name (used by sync_tools script) |
| `config/tenants/` | Per-tenant catalog scoping (CatalogScoper) |

## Observability

Each routing decision emits one `TelemetryEvent` (`services/v2/telemetry.py`) with 12 fields:
`tenant_id`, `correlation_id`, `turn_number`, `query_scrubbed`, `is_negation`, `tool_picked`, `confidence`, `competitors`, `clarify`, `error`, `latency_ms`, `redactions`.

`correlation_id` + `turn_number` + `is_negation` auto-injected via ContextVar (`set_request_context()` at engine entry). Sinks (`V2_TELEMETRY_BACKEND`):

- **`StdoutJsonSink`** — primary. Docker captures stdout → Azure Container Apps / Log Analytics.
- **`RedisSink`** — live tap. `LPUSH routing:accuracy_log` (LTRIM 0..999, 30d TTL). Admin endpoint `/webhook/whatsapp/routing-log?token=…` reads this for live debugging.
- **`BufferedAsyncFileSink`** — dev only.

User feedback signal: bot appends `"Ako nije točno, napiši 'nije točno'."` to read/mutate execute responses. Exact-match `"nije točno"` next turn flips `is_negation=True` — explicit ground-truth signal.

## Admin endpoints

Two token-protected diagnostic endpoints (`webhook_simple.py`):

| Path | Purpose |
|---|---|
| `GET /webhook/whatsapp/debug?token=...` | Webhook stats, last events ring buffer, Redis health, stream lag |
| `GET /webhook/whatsapp/routing-log?token=...&limit=N` | Last N (max 500) routing decisions from `routing:accuracy_log` |

Token check via `services/admin_auth.py` (constant-time, returns user label). Up to 4 tokens via `ADMIN_TOKEN_1..4`; `ADMIN_TOKEN_<N>_USER` env var gives the operator label for per-call audit logging.

## Deployment

**Target: Azure VM with Docker.**

| Component | Mechanism |
|---|---|
| Docker image | tagged with git short-sha for rollback |
| Reverse proxy | nginx + LetsEncrypt for `bot.damir.com` |
| Local Postgres + Redis | Docker Compose services |
| Deploy iteration | `git push` → `docker build` → SSH `docker pull` + restart |
| Rollback | `docker pull <dated-tag>` + restart |

Full runbook: [docs/AZURE_VM_DEPLOY_PLAYBOOK.md](docs/AZURE_VM_DEPLOY_PLAYBOOK.md).

`k8s/` manifests archived to `k8s/_archive/` — never production-tested.
