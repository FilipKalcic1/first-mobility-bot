# k8s/ — production deployment

Fresh manifests (2026-06-11), replacing the archived never-tested set. This
replaces the dev setup of `docker run` + `ngrok http 8000`: the ingress is
the public HTTPS webhook URL for Infobip, with HA on the receive path.

## Topology

```
Infobip ──HTTPS──▶ ingress-nginx ──▶ bot-api ×2 (webhook: HMAC → XADD)   [HPA 2-4]
                                        │
                                   Redis stream  (AOF persisted, noeviction)
                                        │
                                  bot-worker ×1 (V2Engine brain)         [Recreate]
                                        │
                              Postgres (user_mappings, audit)
```

Performance/correctness decisions baked in (each documented in the manifest):

| Decision | Where | Why |
|---|---|---|
| api ×2, RollingUpdate `maxUnavailable: 0`, preStop sleep | api.yaml | zero webhook downtime during deploys — Infobip won't wait |
| worker ×1, Recreate | worker.yaml | per-sender ordering is an in-process lock; 2 workers would interleave a user's pending confirm/param state. Stream buffers during the ~30s gap |
| Redis AOF + `noeviction` | redis.yaml | queues/DLQ/pending state live here — eviction = silent message loss; full Redis must fail LOUD |
| anchor-cache PVC + `ANCHOR_CACHE_PATH` | worker.yaml, configmap | without it every deploy re-embeds ~11k anchor phrases (minutes of cold start, Azure cost, 429 risk) |
| worker heartbeat-file liveness | worker.yaml | worker has no HTTP port; a hung event loop stops touching the file → restart |
| migrate as a Job, not init-container | migrate-job.yaml | one migration per rollout, not one per pod restart |
| NetworkPolicy zero-trust | networkpolicy.yaml | only nginx→api; only api/worker→redis/pg; egress 443+DNS only |

## Deploy runbook

```bash
# 0) prereqs on the cluster: ingress-nginx, cert-manager (ClusterIssuer letsencrypt-prod)

# 1) build + push the image (git-lfs MUST be pulled — tool_data.json is LFS!)
git lfs pull
docker build -t ghcr.io/filipkalcic1/nova-verzija:$(git rev-parse --short HEAD) .
docker push ghcr.io/filipkalcic1/nova-verzija:$(git rev-parse --short HEAD)
# update the image tag in api.yaml / worker.yaml / migrate-job.yaml (or kustomize images:)

# 2) secrets (once per cluster; values from your password manager)
#    see secret.example.yaml for the exact kubectl create secret command

# 3) edit ingress.yaml host (bot.example.com → your DNS name)

# 4) apply everything
kubectl apply -k k8s/

# 5) migrate DB schema
kubectl -n mobility-bot apply -f k8s/migrate-job.yaml
kubectl -n mobility-bot wait --for=condition=complete job/bot-migrate --timeout=120s

# 6) verify
kubectl -n mobility-bot get pods
kubectl -n mobility-bot logs deploy/bot-worker | tail   # expect anchor_index + health lines
curl https://<your-host>/webhook/whatsapp                # → "ok"

# 7) point the Infobip webhook at https://<your-host>/webhook/whatsapp
```

## Rollout / rollback

```bash
# new version
kubectl -n mobility-bot set image deploy/bot-api    api=ghcr.io/...:<sha>
kubectl -n mobility-bot set image deploy/bot-worker worker=ghcr.io/...:<sha>
# rollback
kubectl -n mobility-bot rollout undo deploy/bot-api
kubectl -n mobility-bot rollout undo deploy/bot-worker
```

Worker rollout = up to ~60s of buffered (not lost) messages: api keeps
ACKing webhooks into the persisted stream; the new worker drains it.

## Scaling the worker beyond 1 (future)

The Redis consumer group already supports N workers. Before raising
`replicas`, move the per-sender serialization from the in-process dict
(worker.py `_processing_locks`) to a Redis lock (e.g. `SET NX PX` per
sender with renewal) — otherwise two pods can interleave one user's
pending-confirm state. Until ~thousands of users, 1 worker is not the
bottleneck (turn latency is LLM+M1 bound; different senders already run
concurrently inside the pod).

## Monitoring

Everything logs structured JSON to stdout → your cluster log stack.
Grep keys: `health` (worker stats incl. DLQ depth + pg pool), `dlq_growing`,
`circuit OPEN`, `config_freshness`, `routing-log` admin endpoint for live
routing decisions.
