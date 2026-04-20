# MobilityOne WhatsApp Bot

Croatian-language conversational agent over the MobilityOne fleet API. Receives Infobip WhatsApp webhooks, routes natural-language requests to ~950 backend tools via a hybrid FAISS + BM25 + LLM reranker pipeline, resolves identifier dependencies automatically (Graph Discovery), and replies in Croatian.

- **Version:** `11.0.2` (see [config.py](config.py))
- **Runtime:** Python 3.14, FastAPI, asyncio worker
- **Stores:** PostgreSQL (asyncpg + SQLAlchemy 2.0), Redis (queue + cache + distributed locks)
- **AI:** Azure OpenAI — `gpt-4o-mini` for routing/reranking, `text-embedding-ada-002` for FAISS
- **Deployment:** single-pod Kubernetes, 1 CPU / 1 GiB RAM (see [k8s/](k8s/))

## Repository layout

| Path | Purpose |
| --- | --- |
| [main.py](main.py) | FastAPI app (webhook receiver, health, metrics) |
| [webhook_simple.py](webhook_simple.py) | Infobip HMAC verification + enqueue |
| [worker.py](worker.py) | Async queue consumer, runs the full routing pipeline |
| [config.py](config.py) | Pydantic `Settings` — all env vars |
| [database.py](database.py), [models.py](models.py), [base.py](base.py) | SQLAlchemy engine + ORM |
| [tool_routing.py](tool_routing.py) | Entry into unified router |
| [services/](services/) | Routing, retrieval, execution, GDPR, cost tracking, circuit breakers |
| [services/engine/](services/engine/) | Per-intent handlers (tool, confirmation, hallucination, user, flow) |
| [services/registry/](services/registry/) | Swagger parsing, tool registry, embedding engine |
| [services/dependency_resolver/](services/dependency_resolver/) | Graph Discovery over `processed_tool_registry.json` |
| [config/](config/) | Processed tool registry, documentation, categories, context schemas |
| [alembic/versions/](alembic/versions/) | DB migrations (001 initial, 002 GDPR consent, 003 ORM alignment) |
| [k8s/](k8s/) | Kustomize deployment manifests |
| [tests/](tests/) | pytest suite + benchmarks |
| [scripts/](scripts/) | Tool sync, embedding generation, documentation helpers |

## Quickstart (local)

```bash
python -m venv .venv && . .venv/Scripts/activate   # Windows: Scripts; Linux/mac: bin
pip install -r requirements.txt -r requirements-dev.txt

cp .env.example .env          # fill in DATABASE_URL, REDIS_URL, Azure + Infobip keys
alembic upgrade head

# API (webhook receiver)
uvicorn main:app --host 0.0.0.0 --port 8000

# Worker (in a second shell)
python worker.py
```

Docker alternative: `docker compose up --build` (see [docker-compose.yml](docker-compose.yml)).

## First-time setup of the tool registry

Swagger → processed registry → FAISS embeddings:

```bash
python scripts/sync_tools.py                   # regenerate config/processed_tool_registry.json
python scripts/generate_tool_embeddings.py     # populate .cache/tool_embeddings.json
```

Re-run `scripts/generate_tool_embeddings.py` whenever tool metadata changes; the worker loads `.cache/tool_embeddings.json` on startup.

## Tests

```bash
pytest                                          # full suite
pytest tests/benchmarks/test_tool_recognition.py  # retrieval benchmark (seed=42)
PARAPHRASES_PATH=tests/benchmarks/tool_recognition_paraphrases.seed1337.json \
  pytest tests/benchmarks/test_tool_recognition.py   # held-out seed
```

Pre-flight for production pods:

```bash
python scripts/verify_production_readiness.py
```

## Related documents

- [ARCHITECTURE.md](ARCHITECTURE.md) — component map and request lifecycle
- [DEPLOYMENT.md](DEPLOYMENT.md) — k8s manifests, env vars, sealed secrets
- [SECURITY.md](SECURITY.md) — threat model, GDPR, hardening
- [HANDOFF.md](HANDOFF.md) — ownership orientation for new maintainers
- [CHANGELOG.md](CHANGELOG.md) — notable changes
- [k8s/README.md](k8s/README.md) — manifest-level details
