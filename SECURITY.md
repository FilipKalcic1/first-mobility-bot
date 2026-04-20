# Security

Defensive posture for the MobilityOne WhatsApp bot. Assumes an internet-exposed webhook endpoint fronted by an ingress controller.

## Trust boundaries

| Boundary | Control |
| --- | --- |
| Infobip → `/webhook` | HMAC-SHA256 signature verification ([webhook_simple.py](webhook_simple.py)), `VERIFY_WHATSAPP_SIGNATURE=true` in prod |
| Bot → Postgres | Two-role model; the app runs as `bot_user` with a narrow INSERT/SELECT allowlist |
| Bot → MobilityOne API | OAuth2 client credentials, per-request bearer, SSRF guard in [services/api_gateway.py](services/api_gateway.py) |
| Bot → Azure OpenAI | Deployment-scoped API key via sealed secret |
| Admin endpoints | Bearer token (`ADMIN_API_TOKEN`) + IP allowlist (`ADMIN_IP_WHITELIST`) |
| Pod → Pod | Kubernetes `NetworkPolicy` default-deny in [k8s/service.yaml](k8s/service.yaml) |

## Dual-user database model

- **bot_user** — used by api + worker. Can SELECT/INSERT on `user_mappings`, `conversations`, `messages`, `tool_executions`; can INSERT (only) into `hallucination_reports`. Cannot read `audit_logs` or update/delete hallucination rows.
- **admin_user** — full access. Only used by offline admin tooling.

Migrations running as the Postgres superuser grant these roles explicitly. See [alembic/versions/](alembic/versions/).

## PII handling

- Phone numbers never appear unmasked in logs. Helpers live in [services/gdpr_masking.py](services/gdpr_masking.py) and [services/pii_filter.py](services/pii_filter.py).
- `scripts/verify_production_readiness.py` scans [services/user_service.py](services/user_service.py), [webhook_simple.py](webhook_simple.py), and [worker.py](worker.py) for unmasked `{phone}` / `{sender}` interpolations in logger calls — fails the pre-flight if any are found.
- GDPR consent timestamps stored server-side (migration `002`). A salted SHA-256 (`GDPR_HASH_SALT`) pseudonymizes phone numbers in analytics and error tables.
- Hallucination capture ([services/hallucination_repository.py](services/hallucination_repository.py)) masks PII before the row is written.

## Webhook hardening

- HMAC-SHA256 over the raw body. Constant-time comparison. Missing signature → 401.
- Body size cap enforced before JSON parse.
- Infobip retries are idempotent: dedupe key is the `messageId`.

## SSRF and outbound calls

[services/api_gateway.py](services/api_gateway.py) resolves target hosts and rejects private/loopback/link-local ranges unless the target matches the configured MobilityOne base URL. Redirects are not followed across hosts.

## Rate limiting and backpressure

- `fastapi-limiter` on the webhook endpoint (Redis-backed).
- Circuit breakers ([services/circuit_breaker.py](services/circuit_breaker.py)) per upstream dependency with a `HALF_OPEN_PROBING` state to avoid thundering herds on recovery.
- Worker pulls from Redis with a bounded concurrency to keep memory under the 500 MiB container limit.

## Distributed locks

Critical sections (FAISS warmup, hallucination report writes) use a SET NX with a cached Lua release script. `scripts/verify_production_readiness.py` runs a live atomicity test (release-only-own-lock) on each deploy.

## etcd and cluster-level

[k8s/encryption-config.yaml](k8s/encryption-config.yaml) is provided as a reference for etcd encryption-at-rest — cluster admins apply it out-of-band.

## Audit items checked each release

- [ ] `VERIFY_WHATSAPP_SIGNATURE=true` in prod sealed secret
- [ ] `DEBUG=false` in production ConfigMap
- [ ] No new logger call sites interpolate `phone`/`sender` unmasked
- [ ] Alembic migrations applied before API/worker start
- [ ] NetworkPolicy still default-deny after any k8s edit
- [ ] Sealed-secrets re-sealed whenever any key rotates
