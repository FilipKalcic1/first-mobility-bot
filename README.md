# MobilityOne WhatsApp Bot

Croatian-language conversational agent over the MobilityOne fleet API. Receives Infobip WhatsApp webhooks, routes natural-language requests to ~950 backend tools, executes via the MobilityOne HTTP API, replies in Croatian.

- **Runtime:** Python 3.12 + FastAPI + asyncio worker ([Dockerfile](Dockerfile))
- **Stores:** PostgreSQL (asyncpg + SQLAlchemy 2.0), Redis (queue + cache + distributed locks)
- **AI:** Azure OpenAI — `gpt-4o-mini` (chat + tool-call routing), `text-embedding-ada-002` (anchor retrieval)
- **Deploy target:** Azure VM with Docker — see [docs/AZURE_VM_DEPLOY_PLAYBOOK.md](docs/AZURE_VM_DEPLOY_PLAYBOOK.md)

## Routing architecture (post-rewrite 2026-05-12)

Single pipeline. Old V2 recognition + V3 hierarchical router experiments deleted.

```
Infobip POST → webhook → Redis stream → worker → V2Engine.process_message
   L-1 rate limiter / L0.5 PII / L0.6 sanitizer / L0 identity (cache 30s)
   L0.7 crisis / L0.75 negation / L0.8 multi-intent / L0.85 meta
   L1 special intents (GDPR/welcome/handover) / L1.5 unknown-phone gate
   L2 driver quick-path (regex, 0 LLM) / L2a intent type / L2b basics anchor
   L3 LLM router [services/router/]:
     anchor_index.top_k(query) → 50 candidates
     tool_schema_builder → OpenAI tools=[]
     gpt-4o-mini chat.completions.create(tools=..., tool_choice="auto")
     → RouterResult{tool_id, params, confidence, anchor_score}
   L5 confidence_gate → execute / clarify / fallback
   L6 mutation gate → confirm dialog for POST/PUT/PATCH/DELETE
   L7 executor → services/api_gateway (OAuth, circuit breaker, x-tenant)
   L8 LLM formatter [services/formatter/]:
     output_sanitize → prune → gpt-4o-mini Croatian response → PII scrub
   → Redis outbound list → Infobip POST
```

## Known limits (honest status, 2026-05-25)

- **Routing accuracy is the real cap.** Common/driver tools p@1 ~70–83%; the ~920 long-tail tools ~20% p@1 (+ gpt-4o-mini run-to-run `no_tool_call` variance). If the wrong tool is picked, nothing downstream matters. A real-data bench re-run is the highest-value next step — see [docs/ACCURACY_HONEST_2026-05-24.md](docs/ACCURACY_HONEST_2026-05-24.md).
- **Filter is disabled (reset to zero).** The bot builds no `Filter` query param; the redesign is data-driven and waits on a MobilityOne filter-schema — see [docs/FILTER_REDESIGN_2026-05-25.md](docs/FILTER_REDESIGN_2026-05-25.md) + [docs/M1_ZAHTJEV_filter_2026-05-25.md](docs/M1_ZAHTJEV_filter_2026-05-25.md).
- **~half the tool base isn't chat-drivable yet** due to backend Swagger gaps: 0 enums, 196 mutations without a named body-schema, only 28% of required params have descriptions. The fix is backend enrichment ([docs/M1_ZAHTJEV_params_2026-05-25.md](docs/M1_ZAHTJEV_params_2026-05-25.md)), not bot code.
- **Output is bounded by what the tool returns.** L8 LLM formatter selects the relevant field(s) per the user's question, values verbatim from the JSON (grounded, no hallucination), with a deterministic template fallback — but it can only surface fields the response actually contains.

## Repository layout

| Path | Purpose |
|---|---|
| [main.py](main.py) | FastAPI app (webhook receiver, health checks) |
| [webhook_simple.py](webhook_simple.py) | Infobip HMAC verification + enqueue + admin diagnostic endpoints |
| [worker.py](worker.py) | Async queue consumer; runs V2Engine routing |
| [config.py](config.py) | Pydantic `Settings` — all env vars |
| [database.py](database.py), [models.py](models.py) | SQLAlchemy engine + ORM |
| [services/v2/](services/v2/) | Engine orchestrator + L0-L2/L4-L8 guards, flows, gates, executor |
| [services/router/](services/router/) | L3 anchor retrieval + OpenAI tool-call routing |
| [services/formatter/](services/formatter/) | L8 LLM-driven Croatian response generation |
| [services/api_gateway.py](services/api_gateway.py) | MobilityOne HTTP client (OAuth, retries, circuit breaker, tenant headers) |
| [services/registry/](services/registry/) | Offline registry build (sync_tools, embedding helper for scripts) |
| [config/](config/) | Tool registry (950 tools), TKB, anchor enrichments, quick-path patterns, typo synonyms |
| [tests/](tests/) | pytest suite (~1180 passing) |
| [scripts/](scripts/) | Tool sync, anchor + TKB regeneration, router benchmark, param enrichment |
| [k8s/_archive/](k8s/_archive/) | Old k8s manifests (never production-tested) |

## Quickstart (local dev)

```bash
python -m venv .venv && . .venv/Scripts/activate   # Windows: Scripts; Linux/mac: bin
pip install -r requirements.txt -r requirements-dev.txt

cp .env.example .env          # fill in DATABASE_URL, REDIS_URL, Azure + Infobip keys
alembic upgrade head

# API (webhook receiver)
uvicorn main:app --host 0.0.0.0 --port 8000

# Worker (separate shell)
python worker.py
```

Docker: `docker compose up --build` (see [docker-compose.yml](docker-compose.yml)).

## Testing

```bash
pytest                                          # full suite
python scripts/bench_router.py                  # end-to-end router benchmark
```

## Production deploy

Single Docker image, Azure VM target. See [docs/AZURE_VM_DEPLOY_PLAYBOOK.md](docs/AZURE_VM_DEPLOY_PLAYBOOK.md) for step-by-step.

Rollback: pull previous sha tag, restart container. ~30-60s downtime per deploy.

## Related documents

- [ARCHITECTURE.md](ARCHITECTURE.md) — component map + request lifecycle
- [DEPLOYMENT.md](DEPLOYMENT.md) — env vars, secrets, deploy steps
- [docs/REWRITE_2026-05-12.md](docs/REWRITE_2026-05-12.md) — routing rewrite report + numbers
- [docs/AZURE_VM_DEPLOY_PLAYBOOK.md](docs/AZURE_VM_DEPLOY_PLAYBOOK.md) — full VM runbook
- [SECURITY.md](SECURITY.md) — threat model, GDPR, hardening
- [CLAUDE.md](CLAUDE.md) — engineering doctrine
- [CHANGELOG.md](CHANGELOG.md) — notable changes
