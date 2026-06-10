# 13 — CONFIG & SIGURNOST

**Svrha**: Centralna konfiguracija (Pydantic Settings iz env varijabli) + sigurnosni sloj: admin token verifikacija, HTTP security headeri, HMAC cache-invalidation webhook, strukturirana hijerarhija grešaka, Azure OpenAI klijent factory.

## Datoteke

| Datoteka | LOC | Status | Uloga |
|---|---|---|---|
| `config.py` | 339 | LIVE | Pydantic `Settings` (sve env var) + validatori. `get_settings()` @lru_cache singleton, FAILA ako fale obavezni env |
| `services/admin_auth.py` | 78 | LIVE | Admin token (ADMIN_TOKEN_1..4) constant-time HMAC |
| `services/security_headers.py` | 64 | LIVE | Starlette middleware: CSP, HSTS, X-Frame-Options, no-store |
| `services/v2/cache_invalidation.py` | 216 | LIVE | HMAC webhook `/admin/cache-invalidate` (MobilityOne busta identity/cache state) |
| `services/errors.py` | 281 | LIVE | `ErrorCode` (str enum ~70 kodova), `BotError` baza + podklase + HTTP_STATUS_TO_ERROR_CODE |
| `services/openai_client.py` | 188 | LIVE | Azure OpenAI factory: get_openai_client (rate-guarded chat), get_embedding_client |

## Ulazne točke

| Simbol | Lokacija | Što radi |
|---|---|---|
| `get_settings` | config.py:331 | @lru_cache singleton; raise+CRITICAL ako fale obavezni env (config importan u ~30 fileova) |
| `verify_admin_token` | admin_auth.py:43 | constant-time hmac.compare_digest preko svih slotova → user label ili None (deny-all). Pozvan na admin rutama u `webhook_simple.py:719/801/859/945` |
| `extract_bearer_or_query_token` | admin_auth.py:67 | iz `?token=` ili `Authorization: Bearer` |
| `SecurityHeadersMiddleware.dispatch` | security_headers.py:19 | main.py:227. CSP olabavljen za /docs,/redoc,/openapi.json |
| `process_request` | cache_invalidation.py:163 | `/admin/cache-invalidate`: 429/413/503/401/400/200. main.py:285 |
| `verify_signature` | cache_invalidation.py:83 | HMAC-SHA256 (compare_digest) |
| `get_openai_client` | openai_client.py:125 | Shared GuardedAzureClient (chat, rate-guarded, max_retries=0, timeout=15s). engine.py:2439 |
| `get_embedding_client` | openai_client.py:159 | Shared AsyncAzureOpenAI embedding (max_retries=1, timeout=10s). engine.py:2440 |

## Settings (config.py)

- **Obavezni** (Field bez defaulta): DATABASE_URL (:32), REDIS_URL (:48), MOBILITY_API_URL/AUTH_URL/CLIENT_ID/CLIENT_SECRET/TENANT_ID (:81-85), AZURE_OPENAI_ENDPOINT/API_KEY (:91-92).
- **Opcionalni s defaultom**: APP_ENV='development' (:24), AZURE_OPENAI_DEPLOYMENT_NAME='gpt-4o-mini' (:94), AZURE_OPENAI_EMBEDDING_DEPLOYMENT='text-embedding-ada-002' (:95), INFOBIP_* (:66-76), CACHE_INVALIDATION_SECRET (:222), AZURE_LLM_MAX_CONCURRENT=20 (:206).
- **Validatori**:
  - `_forbid_disabled_signature_in_production` (config.py:309): ako APP_ENV=='production' i VERIFY_WHATSAPP_SIGNATURE=False → raise (sprječava webhook spoofing).
  - `_require_infobip_secret_when_active` (config.py:294): INFOBIP_API_KEY bez INFOBIP_SECRET_KEY → raise.
  - `validate_url` (config.py:287): MOBILITY/AZURE URL mora http(s), rstrip('/').
- **Property**: is_production (==production), DEBUG (==development), swagger_sources, tenant_id.

## Config data fileovi (tko ih učitava)

| File | Učitan | Status |
|---|---|---|
| `config/tool_data.json` | engine.py:2552 (path), :2567 fail-fast load (`if not exists: raise RuntimeError`) | LIVE |
| `config/processed_tool_registry.json` | registry/__init__.py:94 + engine.py:2553 | LIVE |
| `config/risky_tools.json` | engine.py:2529 (missing=prazan set) | LIVE (optional) |
| `config/context_param_schemas.json` | services/registry/swagger_parser.py:56 (SwaggerParser init) | LIVE |
| `config/domain/path_entity_map.json` | entity_mappings (DEV_ONLY chain) | DEV_ONLY |
| `config/linguistic/typo_synonyms.json` | text_normalizer (lazy) | LIVE (optional) |
| `config/entity_translations_hr.json` | samo `scripts/dedupe_intent_summary.py` | **DEV_ONLY** |
| `config/tenants/_default/tool_subset.json` | catalog_scoper | LIVE |

## Sigurnosni mehanizmi

- `GuardedAzureClient` (openai_client.py:106): proxy nad AsyncAzureOpenAI; svaki `chat.completions.create` omotan u AzureRateGuard.acquire (timeout 30s). Embeddings NE rate-guard.
- `get_azure_rate_guard` (openai_client.py:59): semafor; čita AZURE_LLM_MAX_CONCURRENT iz raw os.environ (runtime override), fallback Settings (20), floor 1.
- `handle_invalidation_event` (cache_invalidation.py:100): identity_cache uvijek clear; conversation_history samo na role_change/termination; pending_mutation/clarify uvijek.
- `_check_ip` (cache_invalidation.py:49): in-memory per-IP rate limit (10/60s).

## Što NE radi

- config NE radi HTTP routing (to je main.py).
- admin_auth NE radi role-based authz — binarni token match + label.
- cache_invalidation NE perzistira eventove i rate-limit je in-memory (po procesu).
- openai_client NE rate-guarda embeddinge; **NEMA reranker klijent** (docstring laže).

## Caveati

- **openai_client docstring (:5-7) laže**: tvrdi "chat/embedding/reranker" ali `get_reranker_client` NE postoji.
- **errors.py:278-280**: dangling header "Convenience factory…" BEZ implementacije.
- **AZURE_LLM_MAX_CONCURRENT** namjerno iz raw os.environ (ne cached Settings) jer Settings singleton ne reflektira runtime mutacije.
- **config/entity_translations_hr.json DEV_ONLY**: nijedan runtime servis ga ne učitava (samo scripts).
- cache_invalidation je LIVE preko main.py FastAPI rute, NE preko worker→V2Engine glavne petlje.
