# 08 — EXECUTOR + API GATEWAY + TOKEN

**Svrha**: L7 izvršavanje alata kroz MobilityOne API — ubrizgavanje konteksta iz identiteta, rutiranje parametara po lokaciji, supstitucija `{id}`, completeness guard, dvije sklopke zaustavljanja (per-service + globalna).

## Datoteke

| Datoteka | LOC | Status | Uloga |
|---|---|---|---|
| `services/v2/executor.py` | 266 | LIVE | L7 izvršitelj s per-servisnom sklopkom (automation/tenantmgt/vehiclemgt) |
| `services/api_gateway.py` | 726 | LIVE | HTTP klijent: token, retry+backoff, x-tenant, Idempotency-Key, SSRF zaštita, globalna sklopka, HTML firewall |
| `services/token_manager.py` | 273 | LIVE | OAuth2 client_credentials + Redis cache + lock + cooldown + auto-refresh |
| `services/retry_utils.py` | 30 | LIVE | Backoff s jitterom + Lua za atomično otpuštanje locka |
| `services/v2/azure_rate_guard.py` | 85 | LIVE | Semafor za konkurentne LLM pozive (default 20) |

## Ulazne točke

| Simbol | Lokacija | Što radi |
|---|---|---|
| `ToolExecutor.execute` | executor.py:84 | Glavni ulaz: tool_id, params, identity_summary → ExecutionResult (success, data, status_code, error, error_body, circuit_open) |
| `APIGateway.call` | api_gateway.py:665 | Wrapper: service, path, query_params, body, tenant_id → APIResponse. URL = `/{service}{path}` |
| `APIGateway.execute` | api_gateway.py:121 | Niski nivo: retry+backoff, 401 refresh, 4xx/5xx, globalna sklopka |
| `TokenManager.get_token` | token_manager.py:73 | OAuth2 token (mem cache 60s buffer → Redis → refresh s lockom) |
| `AzureRateGuard.acquire` | azure_rate_guard.py:52 | Async context manager: čeka semaphor slot |

## Ključni mehanizmi (verificirano)

| Komponenta | Lokacija | Detalj |
|---|---|---|
| Context injection | executor.py:113 | Iz identity_summary: tenant_id, company_id, orgunit_id, vehicle_id, person_id → parametri po `spec.context_params` |
| Param routing po lokaciji | executor.py:143 | path (zamjena `{id}`), query, body, header. Prije: sve u query/body, `{id}` nikad zamijenjen → 404 |
| Completeness guard | executor.py:134 | Obavezni context param prazan nakon injekcije → `missing_required` error (ne tihi 422) |
| Path supstitucija | executor.py | PRVO zamijeni `{id}` (quote safe=''), TEK ONDA provjeri ostale `{...}` → `missing_path_param` |
| Per-service sklopka | executor.py:248 (`_record_failure`); FAIL_THRESHOLD konst. :30 | FAIL_THRESHOLD=3 (5xx ILI timeout) → OPEN 30s → HALF_OPEN → re-fail OPEN 60s. Izolacija po servisu |
| SSRF hardening | api_gateway.py:407 | http(s) path → block userinfo/scheme-mismatch/netloc-mismatch |
| HTML firewall | api_gateway.py:480 | Content-Type text/html ili `<!DOCTYPE` → čista greška (nikad HTML korisniku) |
| Tenant isolation | api_gateway.py:185 | x-tenant OBAVEZAN; bez → MISSING_TENANT_CONTEXT |
| Idempotency-Key | api_gateway.py:229 | POST/PUT/PATCH/DELETE: UUID jednom izvan retry loopa |
| 401 refresh + retry | api_gateway.py:270 | 401 → invalidate token + retry. 408/429/5xx → backoff+jitter. **Globalna sklopka broji samo 5xx+network, 4xx RESETIRA** |
| OAuth2 grant | token_manager.py:157 | POST grant_type=client_credentials; Redis cache TTL=(expires_in - 120s), min 60s |

## Redis ključevi

- `mobility:access_token` — JSON `{token, expires_at}`, SETEX TTL=(expires_in-120s)
- `lock:{message_id}:{phone}` / Lua owner-safe release (retry_utils)
- `api_err_translate:{hash}` — vidi [09_FORMATTER](09_FORMATTER.md)

## Status kodovi (KLJUČNO za interpretaciju)

- **403 FORBIDDEN** = korisnik/tenant nema scope/permission (bot poziv je strukturno OK, M1 odbija ovlast). U 50-tool testu sve greške bile 403/5xx, **0× 422** → bot strukturno gradi poziv ispravno.
- **422 VALIDATION** = parametar fali/krivi tip (strukturna greška poziva).

## Što NE radi

- Ne validira param schemu prije slanja (validator hook obrisan 2026-05-09; pokrivaju llm_router + param_ui).
- Ne transformira M1 odgovor (raw data; formatter dalje).
- Ne šalje WhatsApp (to je whatsapp_service).
- Ne radi rate limiting (L-1) ni PII scrubbing (L0.5).

## Caveati

- **DVIJE sklopke**: (1) executor per-service (automation/tenantmgt/vehiclemgt izolacija, threshold 3, 30s); (2) api_gateway GLOBALNA (threshold 3, base cooldown 15s + exponential). Različite svrhe.
- **OAuth token je plaintext u Redisu** → za multi-pod prod OBAVEZNO Redis AUTH + TLS (`rediss://`).
- **Obje sklopke broje SAMO 5xx + timeout/network kao failure, NE 4xx**. Executor `_record_failure` se zove samo na TimeoutError (executor.py:192-193), exception (:198) i `status_code >= 500` (:204-205). Razlika: api_gateway dodatno eksplicitno RESETIRA counter na 4xx (user error).
- **Tenant ID iz 2 izvora po prioritetu**: explicit `tenant_id` > `context["TenantId"]` (api_gateway `_execute_inner` ~:193: `effective_tenant = tenant_id or ctx_tenant`). **.env `MOBILITY_TENANT_ID` je NAMJERNO odbijen kao fallback** (komentar: cross-tenant write rizik) → nijedan izvor → MISSING_TENANT_CONTEXT.
