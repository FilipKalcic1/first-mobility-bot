# k8s/

Single-pod deployment for the MobilityOne WhatsApp bot.

## Constraint

**1 replica, 1 CPU, 1 GiB RAM** for the whole Pod. No horizontal autoscaling.

## Layout

| File | Purpose |
| --- | --- |
| `deployment.yaml` | Single `Deployment` (api + worker sidecars) + migration `Job` |
| `service.yaml` | `mobility-api` Service + Ingress + NetworkPolicy + Redis Service |
| `configmap.yaml` | Non-secret env vars (pool sizes, AI tuning, Azure deployment names) |
| `sealed-secrets.yaml` | Bitnami SealedSecrets for `DATABASE_URL`, `REDIS_URL`, Azure keys, Infobip secret |
| `pvc.yaml` | ReadWriteOnce cache PVC for embeddings (~40 MiB, loaded at startup) |
| `redis.yaml` | In-cluster Redis StatefulSet — **delete for managed Redis**, update `service.yaml` + secret accordingly |
| `create-sealed-secret.sh` | Helper to regenerate `sealed-secrets.yaml` from a local `.env` |
| `chaos-cpu-burn.yaml` | Optional chaos experiment, not applied by default |
| `encryption-config.yaml` | etcd encryption-at-rest config (cluster-level) |
| `kustomization.yaml` | `kubectl apply -k k8s/` entrypoint |

## Apply

```bash
# 1. Seal secrets once per environment (requires kubeseal + sealed-secrets controller):
./k8s/create-sealed-secret.sh

# 2. Apply manifests:
kubectl apply -k k8s/

# 3. Run DB migrations (Job is idempotent):
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

Headroom of ~100 MiB under the 1 GiB ceiling absorbs kubelet/log-rotation overhead.

## Why `Recreate` strategy?

Single replica + `ReadWriteOnce` PVC. A rolling update would try to schedule the new pod before the old releases the volume, which deadlocks on most CSI drivers. `Recreate` terminates the old pod first. Downtime window: ~30–60 s per deploy. If zero-downtime is required, move to an `ReadWriteMany` volume and switch back to rolling.

## Scaling out later

This manifest is intentionally single-replica. If the boss lifts the constraint:

1. Bump `spec.replicas` in `deployment.yaml`.
2. Switch the PVC to `ReadWriteMany` (e.g. `azurefile`, `efs-sc`).
3. Change `strategy` back to `RollingUpdate`.
4. Add a `PodDisruptionBudget` with `minAvailable: 1`.
5. Split API and worker into separate Deployments if independent scaling is needed — KEDA on Redis queue depth for the worker side.
