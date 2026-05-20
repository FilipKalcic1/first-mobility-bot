# Azure VM Deploy Playbook

**Audience:** Filip (or Damir if he runs deploy himself)
**Trigger:** Damir says "stavi na Azure VM"
**Target:** Single Ubuntu VM with Docker, no Kubernetes (yet).

---

## Prerequisites Damir provisions

| Item | Spec |
|---|---|
| OS | Ubuntu 22.04 LTS or newer |
| Compute | Min 2 vCPU / 4 GiB RAM (bot + Postgres + Redis local Docker = ~1.5 GiB) |
| Disk | 20 GiB SSD |
| Public IP | Yes, with DNS A record (e.g. `bot.damir.com`) |
| SSH access for Filip | port 22, key auth |
| Firewall inbound | 22 (SSH, your IP only), 443 (Infobip webhooks) |
| Firewall outbound | 443 unrestricted (Azure OpenAI + MobilityOne API) |

If Damir uses Azure Managed Postgres / Redis, those replace local Docker containers — saves ~500 MiB RAM but adds network hop.

---

## One-time VM setup (~30-45 min)

### 1. SSH to VM and update

```bash
ssh azureuser@<VM_PUBLIC_IP>
sudo apt update && sudo apt upgrade -y
```

### 2. Install Docker

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
newgrp docker
docker --version
```

### 3. Install Nginx + Certbot (TLS termination → bot:8000)

```bash
sudo apt install -y nginx certbot python3-certbot-nginx
```

Create `/etc/nginx/sites-available/mobility-bot`:

```nginx
server {
    server_name bot.damir.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        client_max_body_size 1m;
        proxy_read_timeout 30s;
    }

    listen 80;
}
```

```bash
sudo ln -s /etc/nginx/sites-available/mobility-bot /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d bot.damir.com  # generates LetsEncrypt cert
```

### 4. Login to GHCR (so VM can pull image)

Need a separate **read-only** PAT for the VM (NOT the same one used for push):

1. On https://github.com/settings/tokens/new
2. Scope: only `read:packages`
3. Save token

```bash
echo "<read-only-PAT>" | docker login ghcr.io -u FilipKalcic1 --password-stdin
```

This stores creds in `~/.docker/config.json`. Future `docker pull` works without re-auth.

### 5. Setup deploy directory

```bash
sudo mkdir -p /opt/mobility-bot
sudo chown $USER:$USER /opt/mobility-bot
cd /opt/mobility-bot
```

### 6. Write production `.env`

```bash
nano .env
```

Copy from `.env.example` and fill in:

```bash
APP_ENV=production
LOG_LEVEL=WARNING

# Database
DATABASE_URL=postgresql+asyncpg://bot_user:<PASS>@postgres:5432/mobility_db
ADMIN_DATABASE_URL=postgresql+asyncpg://admin_user:<PASS>@postgres:5432/mobility_db

# Redis
REDIS_URL=redis://redis:6379/0

# Azure OpenAI (production)
AZURE_OPENAI_ENDPOINT=https://<production>.openai.azure.com/
AZURE_OPENAI_API_KEY=<key>
AZURE_OPENAI_API_VERSION=2024-08-01-preview
AZURE_OPENAI_DEPLOYMENT_NAME=gpt-4o-mini
AZURE_OPENAI_EMBEDDING_DEPLOYMENT=text-embedding-ada-002

# MobilityOne API (production)
MOBILITY_API_URL=https://<production>.mobilityone.io/
MOBILITY_AUTH_URL=https://<production>.mobilityone.io/sso/connect/token
MOBILITY_CLIENT_ID=<id>
MOBILITY_CLIENT_SECRET=<secret>
MOBILITY_TENANT_ID=<id>

# Infobip
INFOBIP_BASE_URL=https://<your>.api.infobip.com
INFOBIP_API_KEY=<key>
INFOBIP_SECRET_KEY=<key>
INFOBIP_SENDER_NUMBER=<number>
VERIFY_WHATSAPP_SIGNATURE=true

# Admin tokens
ADMIN_TOKEN_1=<64-char-hex from `openssl rand -hex 32`>
ADMIN_TOKEN_1_USER=filip.kalcic
ADMIN_TOKEN_2=<64-char-hex>
ADMIN_TOKEN_2_USER=damir.skrtic

# Cache invalidation
CACHE_INVALIDATION_SECRET=<64-char-hex>

# Sentry
SENTRY_DSN=<your DSN>

# GDPR
GDPR_HASH_SALT=<32-char-hex>
```

`chmod 600 .env` (read/write only by owner).

### 7. Write production `docker-compose.production.yml`

```bash
nano docker-compose.production.yml
```

```yaml
services:
  postgres:
    image: postgres:15-alpine
    restart: unless-stopped
    environment:
      POSTGRES_USER: bot_user
      POSTGRES_PASSWORD: ${BOT_DB_PASSWORD}
      POSTGRES_DB: mobility_db
    volumes:
      - postgres_data:/var/lib/postgresql/data
    networks: [internal]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U bot_user -d mobility_db"]

  redis:
    image: redis:7-alpine
    restart: unless-stopped
    command: ["redis-server", "--appendonly", "yes"]
    volumes:
      - redis_data:/data
    networks: [internal]

  api:
    image: ghcr.io/filipkalcic1/bot:latest
    restart: unless-stopped
    pull_policy: always
    env_file: .env
    ports:
      - "127.0.0.1:8000:8000"  # Nginx proxies to this
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_started
    networks: [internal]
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/ready"]
      interval: 30s
      timeout: 10s
      retries: 3

  worker:
    image: ghcr.io/filipkalcic1/bot:latest
    restart: unless-stopped
    pull_policy: always
    env_file: .env
    command: ["python", "worker.py"]
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_started
    networks: [internal]

  migrate:
    image: ghcr.io/filipkalcic1/bot:latest
    pull_policy: always
    env_file: .env
    command: ["python", "-m", "alembic", "upgrade", "head"]
    depends_on:
      postgres:
        condition: service_healthy
    networks: [internal]
    restart: "no"  # one-shot

volumes:
  postgres_data:
  redis_data:

networks:
  internal:
    driver: bridge
```

### 8. First boot

```bash
cd /opt/mobility-bot
docker compose -f docker-compose.production.yml pull
docker compose -f docker-compose.production.yml run --rm migrate  # one-shot DB init
docker compose -f docker-compose.production.yml up -d api worker
docker compose -f docker-compose.production.yml ps
docker compose -f docker-compose.production.yml logs -f api
```

Wait until `api` shows `Application startup complete`. Then:

```bash
curl https://bot.damir.com/health
# Expect: {"status":"healthy",...}

curl https://bot.damir.com/ready
# Expect: HTTP 200 with checks
```

### 9. Configure Infobip webhook

In Infobip console:
- WhatsApp Business → Webhooks → Set URL: `https://bot.damir.com/webhook/whatsapp`
- Save webhook secret to `.env` `INFOBIP_SECRET_KEY` if not done

### 10. Send a test WhatsApp message

From an enrolled phone number, send `kolika km` to bot. Expect Croatian response with mileage.

If response missing, check:
```bash
docker compose logs -f api
docker compose logs -f worker
```

---

## Iterative deploy (every code update)

### On dev machine (Filip):

```bash
cd c:/Users/filip/Desktop/damir/nova-verzija

# 1. Run tests
pytest tests/ -q --ignore=tests/benchmarks

# 2. Build
docker build -t ghcr.io/filipkalcic1/bot:latest -t ghcr.io/filipkalcic1/bot:$(git rev-parse --short HEAD) .

# 3. Login to GHCR (PAT with write:packages, ROTATE regularly)
echo "$GHCR_TOKEN" | docker login ghcr.io -u FilipKalcic1 --password-stdin

# 4. Push both tags
docker push ghcr.io/filipkalcic1/bot:latest
docker push ghcr.io/filipkalcic1/bot:$(git rev-parse --short HEAD)
```

### On VM (Filip via SSH, or Damir):

```bash
ssh azureuser@<VM_PUBLIC_IP>
cd /opt/mobility-bot

# 5. Pull new image
docker compose -f docker-compose.production.yml pull api worker

# 6. Run migration if any
docker compose -f docker-compose.production.yml run --rm migrate

# 7. Restart
docker compose -f docker-compose.production.yml up -d --no-deps api worker

# 8. Watch
docker compose -f docker-compose.production.yml logs -f --tail=50 api
```

**Downtime per deploy: ~30-60s** (api/worker container restart). Postgres + Redis stay up.

---

## Rollback

If new version breaks:

```bash
# On VM
cd /opt/mobility-bot

# Pin to specific previous tag (saved during build)
docker pull ghcr.io/filipkalcic1/bot:phase2-cleanup-2026-05-10
docker tag ghcr.io/filipkalcic1/bot:phase2-cleanup-2026-05-10 ghcr.io/filipkalcic1/bot:latest

# Restart with rolled-back image
docker compose -f docker-compose.production.yml up -d --no-deps api worker
```

Build pipeline tags every push with `:latest` AND `:<short-git-sha>`. Rollback target = previous SHA.

---

## Monitoring (lightweight, no Prometheus)

### Health check loop (runs from a separate machine)

```bash
while true; do
    curl -s https://bot.damir.com/health | jq -r '.status'
    sleep 60
done
```

### Check telemetry stream

```bash
# On VM
docker compose -f docker-compose.production.yml exec redis redis-cli
> LRANGE routing:accuracy_log 0 9
```

Returns last 10 routing decisions. Each is JSON with `tool_picked`, `confidence`, `latency_ms`, `error`.

### Admin endpoints (browser or curl)

```bash
# Webhook diagnostics
curl "https://bot.damir.com/webhook/whatsapp/debug?token=$ADMIN_TOKEN_1"

# Last 100 routing decisions
curl "https://bot.damir.com/webhook/whatsapp/routing-log?token=$ADMIN_TOKEN_1&limit=100"
```

Both return JSON. Use for live debugging during a Damir support session.

---

## Common failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| 502 Bad Gateway | api container not running | `docker compose ps`, then `logs api` |
| Webhook returns 401 | INFOBIP_SECRET_KEY mismatch | Re-check Infobip console secret matches `.env` |
| Bot silent on message | Worker not consuming Redis stream | `docker compose logs worker`, check Redis connection |
| `/ready` 503 | DB or Redis disconnected | `docker compose ps`, restart unhealthy container |
| Slow first response (~30s) | Cold-start LLM connection | Normal for first request after restart; warms up |

---

## Security checklist

- [ ] `.env` has `chmod 600`
- [ ] PAT for GHCR is scoped `read:packages` only on VM (no `write:packages`)
- [ ] Push PAT (write:packages) lives only on Filip's dev machine, rotated quarterly
- [ ] `VERIFY_WHATSAPP_SIGNATURE=true`
- [ ] LetsEncrypt certificate auto-renews (`certbot renew --dry-run`)
- [ ] SSH access restricted to Filip's IP via Azure NSG
- [ ] No public Postgres/Redis ports (only internal Docker network)
- [ ] Sentry DSN configured for error tracking
- [ ] Postgres backup cron set up (NOT covered here — use Azure Managed Postgres for built-in backups)

---

## When to migrate to Kubernetes

Stay on Docker until:
- Need >1 replica for redundancy (e.g. zero-downtime deploys)
- Multiple bots / multi-tenant setup
- Damir hires DevOps engineer

Current `k8s/*.yaml` files are **not production-ready** — they were written aspirationally and not tested. When migrating to k8s, either:
- Rewrite from scratch using a fresh Helm chart, or
- Use Azure Container Apps (ACA) which sits between Docker and k8s.

Treat current k8s files as `_archive/` reference only.
