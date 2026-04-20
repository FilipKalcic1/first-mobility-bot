# Deployment

Single-pod Kubernetes target: 1 replica, 1 CPU, 1 GiB RAM. No horizontal autoscaling.

Manifests live in [k8s/](k8s/) and are applied via Kustomize.

## Apply

```bash
# 1. Seal secrets for the target cluster (requires kubeseal + sealed-secrets controller):
./k8s/create-sealed-secret.sh

# 2. Apply manifests:
kubectl apply -k k8s/

# 3. Wait for the idempotent migration Job:
kubectl wait --for=condition=complete --timeout=5m job/mobility-db-migrate

# 4. Watch rollout:
kubectl rollout status deployment/mobility-bot
kubectl logs -f deployment/mobility-bot -c api
kubectl logs -f deployment/mobility-bot -c worker
```

## Resource budget

| Container | CPU req | CPU lim | Mem req | Mem lim |
| --- | --- | --- | --- | --- |
| `api` | 50m | 400m | 350Mi | 400Mi |
| `worker` | 50m | 500m | 350Mi | 500Mi |
| **Pod total** | **100m** | **900m** | **700Mi** | **900Mi** |

~100 MiB headroom under the 1 GiB ceiling absorbs kubelet/log-rotation overhead.

## Deployment strategy

`Recreate`. The embeddings cache PVC is `ReadWriteOnce`; a rolling update would deadlock on the volume. Downtime window per deploy: ~30–60 s.

To lift the single-replica constraint later: switch PVC to `ReadWriteMany`, flip `strategy` to `RollingUpdate`, add a `PodDisruptionBudget`, split API and worker into separate Deployments, and (optionally) add KEDA on Redis queue depth for the worker.

## Environment variables

Full catalog: [.env.example](.env.example). Non-exhaustive highlights:

| Var | Purpose |
| --- | --- |
| `APP_ENV` | `development` / `staging` / `production` |
| `DATABASE_URL` | SQLAlchemy asyncpg URL — **bot_user role** |
| `ADMIN_DATABASE_URL` | Admin role URL, used only by admin tooling |
| `REDIS_URL` | Redis 6+. Sentinel supported via `REDIS_SENTINEL_*` |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | Total cap must stay under Postgres `max_connections` |
| `INFOBIP_BASE_URL`, `INFOBIP_API_KEY`, `INFOBIP_SECRET_KEY` | WhatsApp provider |
| `VERIFY_WHATSAPP_SIGNATURE` | HMAC-SHA256 webhook verification — **must be true in prod** |
| `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_API_VERSION` | Azure OpenAI |
| `AZURE_OPENAI_DEPLOYMENT_NAME` | Chat deployment (gpt-4o-mini) |
| `AZURE_EMBEDDING_DEPLOYMENT_NAME` | Embedding deployment (text-embedding-ada-002) |
| `GDPR_HASH_SALT` | Salt for phone-number hashing in logs and analytics |
| `MOBILITYONE_*` | OAuth2 client credentials and API base |
| `ADMIN_API_TOKEN` | Bearer token guarding admin endpoints |
| `ADMIN_IP_WHITELIST` | Comma-separated CIDRs for admin traffic |

Settings schema with defaults: [config.py](config.py).

## Secrets

Sealed via [k8s/sealed-secrets.yaml](k8s/sealed-secrets.yaml). Regenerate with:

```bash
./k8s/create-sealed-secret.sh   # reads a local .env, produces sealed-secrets.yaml
```

Never commit raw `.env`. The helper asserts no plaintext secret is emitted into manifests.

## Database migrations

Alembic. Three revisions in [alembic/versions/](alembic/versions/):

1. `001_initial_schema` — baseline tables
2. `002_add_gdpr_consent_fields` — consent timestamps + hashed identifiers
3. `003_align_orm_models` — keeps ORM in sync with Postgres

Run locally: `alembic upgrade head`. In-cluster: the manifest includes a Kubernetes `Job` (`mobility-db-migrate`) that runs `alembic upgrade head` before the Deployment rolls.

## Pre-flight verification

Before tagging a pod ready for traffic:

```bash
kubectl exec deploy/mobility-bot -c worker -- python scripts/verify_production_readiness.py
```

Checks the Redis Lua lock script is cached and atomic, FAISS index IDs align with metadata, idle memory < 200 MiB, and no unmasked phone numbers in key log call sites.

## Monitoring (production)

Metrics are scraped by **kube-prometheus-stack**. Install once per cluster:

```bash
kubectl label ns monitoring kubernetes.io/metadata.name=monitoring --overwrite
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm install kube-prom prometheus-community/kube-prometheus-stack \
  --namespace monitoring --create-namespace \
  --set prometheus.prometheusSpec.serviceMonitorSelectorNilUsesHelmValues=false \
  --set prometheus.prometheusSpec.ruleSelectorNilUsesHelmValues=false
```

The app's `/metrics` endpoint requires a Bearer token in production. Create a
matching Secret in the `monitoring` namespace with the same value as
`ADMIN_TOKEN_1` in the app's sealed secret:

```bash
kubectl -n monitoring create secret generic prometheus-metrics-token \
  --from-literal=token="$ADMIN_TOKEN_1"
```

[k8s/monitoring.yaml](k8s/monitoring.yaml) wires the rest:

- **ServiceMonitor** `mobility-bot` — scrapes `mobility-api:metrics` with the Bearer token
- **PrometheusRule** `mobility-bot-alerts` — 10 alerts on exported metrics (latency, LLM error rate, circuit breaker, cost, clarify rate)
- **ConfigMap** `mobility-bot-grafana-dashboard` — labelled `grafana_dashboard: "1"` so the kube-prometheus-stack Grafana sidecar auto-imports it

Access Grafana:

```bash
kubectl -n monitoring port-forward svc/kube-prom-grafana 3000:80
# admin password: `kubectl -n monitoring get secret kube-prom-grafana -o jsonpath='{.data.admin-password}' | base64 -d`
```

The bundled dashboard ("MobilityOne Bot") has panels for HTTP p95, LLM p95, LLM error rate, daily cost, circuit breaker state, routing decision rate, and FAISS search p95. Datasource UID `prometheus` matches the default kube-prometheus-stack provisioning.

## Managed Redis

[k8s/redis.yaml](k8s/redis.yaml) provisions an in-cluster Redis StatefulSet. To switch to managed Redis:

1. Delete `redis.yaml` and its entry from [k8s/kustomization.yaml](k8s/kustomization.yaml).
2. Replace `mobility-redis` Service endpoints with the managed host (or rely entirely on the `REDIS_URL` secret).
3. Update the sealed secret accordingly.
