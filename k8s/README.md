# k8s/ — ARCHIVED

**Status (2026-05-10):** Production target is **Azure VM with Docker** (see [docs/AZURE_VM_DEPLOY_PLAYBOOK.md](../docs/AZURE_VM_DEPLOY_PLAYBOOK.md)).

These k8s manifests in [_archive/](_archive/) are **not production-ready**:

- Never tested against a live cluster
- Image registry path was stale (`mobilityone/bot` → fixed to `filipkalcic1/bot` 2026-05-10, but uncommitted)
- Sealed-secrets controller setup not documented for any specific cluster
- Resource limits assume a specific Pod profile that hasn't been validated

## When to migrate to k8s

- Need >1 replica for redundancy / zero-downtime deploys
- Multi-tenant: separate clusters per tenant
- Damir hires DevOps with k8s experience

## Recommended migration path

**DO NOT** restart from `_archive/`. Either:

1. **Use Azure Container Apps (ACA)** — sits between Docker and k8s, less ops overhead
2. **Use a Helm chart** generated fresh from `Dockerfile` + `docker-compose.production.yml` (per playbook), reviewed against actual cluster setup
3. **Use Bitnami / Cloud-native PostgreSQL Operator + cert-manager + ingress-nginx** stack assembled fresh

`_archive/` contents reference only — kept for git history value, not for direct application.
