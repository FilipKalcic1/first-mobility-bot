# Deployment

**Current production target (2026-05-11): Azure VM with Docker.** Full runbook: [docs/AZURE_VM_DEPLOY_PLAYBOOK.md](docs/AZURE_VM_DEPLOY_PLAYBOOK.md).

## Quick deploy

```bash
# On dev machine
docker build -t ghcr.io/filipkalcic1/bot:latest -t ghcr.io/filipkalcic1/bot:$(git rev-parse --short HEAD) .
echo $GHCR_TOKEN | docker login ghcr.io -u FilipKalcic1 --password-stdin
docker push ghcr.io/filipkalcic1/bot:latest
docker push ghcr.io/filipkalcic1/bot:$(git rev-parse --short HEAD)

# On Azure VM (SSH)
cd /opt/mobility-bot
docker compose -f docker-compose.production.yml pull api worker
docker compose -f docker-compose.production.yml run --rm migrate  # if migrations
docker compose -f docker-compose.production.yml up -d --no-deps api worker
```

Downtime per deploy: **30-60s** (api/worker container restart). Postgres + Redis stay up.

## Rollback

```bash
docker pull ghcr.io/filipkalcic1/bot:<previous-short-sha>
docker tag ghcr.io/filipkalcic1/bot:<sha> ghcr.io/filipkalcic1/bot:latest
docker compose up -d --no-deps api worker
```

## Routing engine selection (V2 vs V3)

`V2_USE_V3_ROUTING=1` in `.env` activates V3 routing. Default 0 uses V2 Recognition. Hot toggle — restart container to apply. Rollback safe: set to 0, restart.

**Per Phase 6 benchmark, V3 routing hits 90.2% strict accuracy on 200 driver-realistic Croatian queries (target ≥85%).** Cost $0.24/1000 queries, latency p50 1.3s.

## Environment variables

Full catalog: [.env.example](.env.example). Critical ones:

| Var | Purpose |
|---|---|
| `APP_ENV` | `development` / `staging` / `production` |
| `DATABASE_URL` | SQLAlchemy asyncpg URL — `bot_user` role |
| `ADMIN_DATABASE_URL` | Admin role URL, used only by admin tooling + migrations |
| `REDIS_URL` | Redis 6+ |
| `INFOBIP_BASE_URL`, `INFOBIP_API_KEY`, `INFOBIP_SECRET_KEY` | WhatsApp provider |
| `VERIFY_WHATSAPP_SIGNATURE` | HMAC-SHA256 webhook verification — **must be true in prod** |
| `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_API_VERSION` | Azure OpenAI |
| `AZURE_OPENAI_DEPLOYMENT_NAME` | Chat deployment (gpt-4o-mini default) |
| `AZURE_OPENAI_EMBEDDING_DEPLOYMENT` | Embedding deployment (text-embedding-3-large default) |
| `MOBILITY_API_URL`, `MOBILITY_CLIENT_ID`, `MOBILITY_CLIENT_SECRET`, `MOBILITY_TENANT_ID` | MobilityOne backend OAuth2 |
| `ADMIN_TOKEN_1..4` | Admin endpoint auth (64-char hex each); `ADMIN_TOKEN_<N>_USER` for audit labels |
| `V2_USE_V3_ROUTING` | `0` (V2) or `1` (V3) — production routing engine flag |
| `CACHE_INVALIDATION_SECRET` | HMAC secret for backend → bot cache-bust webhook |
| `SENTRY_DSN` | Error tracking |
| `GDPR_HASH_SALT` | Salt for phone-number hashing in logs |

## Secrets management

`.env` is gitignored. For Azure VM: file lives at `/opt/mobility-bot/.env` with `chmod 600`. For container: mounted via `env_file: .env` in docker-compose.production.yml.

Never commit raw `.env`. PAT for GHCR push lives only on Filip's dev machine; VM uses a separate `read:packages`-scoped PAT.

## Database migrations

Alembic. Three revisions in [alembic/versions/](alembic/versions/):

1. `001_initial_schema` — baseline tables
2. `002_add_gdpr_consent_fields` — consent timestamps + hashed identifiers
3. `003_align_orm_models` — keeps ORM in sync with Postgres

Run via one-shot Docker service:

```bash
docker compose -f docker-compose.production.yml run --rm migrate
```

The `migrate` service runs `alembic upgrade head` with `ADMIN_DATABASE_URL` and exits.

## Pre-flight verification

After deploy:

```bash
# Health endpoint
curl https://bot.damir.com/health
# Expected: {"status":"healthy", ...}

# Readiness (deep dependency check)
curl https://bot.damir.com/ready
# Expected: HTTP 200

# Smoke test routing log
docker compose -f docker-compose.production.yml exec redis redis-cli LRANGE routing:accuracy_log 0 5

# Admin endpoint (with token)
curl "https://bot.damir.com/webhook/whatsapp/debug?token=$ADMIN_TOKEN_1"
```

## Observability

Telemetry layer is [services/v2/telemetry.py](services/v2/telemetry.py). See [ARCHITECTURE.md](ARCHITECTURE.md#observability) for the event shape and sink stack.

Live tap: `LRANGE routing:accuracy_log 0 -1` on Redis. Long-term: stdout JSON → Docker log driver → Azure Container App Diagnostic Settings → Log Analytics workspace.

## Kubernetes (archived)

`k8s/` manifests were never production-tested. They live under [k8s/_archive/](k8s/_archive/) for reference. If/when k8s migration is needed, **rewrite from scratch** using a fresh Helm chart based on `docker-compose.production.yml`, OR use Azure Container Apps (ACA) which sits between Docker and full k8s. Do not restart from `_archive/` content — see [k8s/README.md](k8s/README.md).
