# Per-tenant configuration

Each tenant subdirectory may contain:

| File | Purpose | Required? | Loaded by |
|---|---|---|---|
| `tool_subset.json` | Whitelist of `operation_id`s this tenant is allowed to route to. Falls back to `_default/tool_subset.json` if missing. | Optional | [services/router/catalog_scoper.py](../../services/router/catalog_scoper.py) `_tenant_subset()` |

## How it works

At request time, [catalog_scoper.scope()](../../services/router/catalog_scoper.py) looks up the user's `tenant_id` and resolves the candidate set:

1. **Per-tenant override** at `config/tenants/{tenant_id}/tool_subset.json` if present (use its `allowed_tool_ids` list verbatim).
2. **Default subset** at `config/tenants/_default/tool_subset.json` otherwise (594 user-facing tools curated 2026-05-16).
3. **No file at all** → fallback to ALL tools in the registry (only `drop_internal` regex narrows the set).

The result is then narrowed further by the per-request `methods` filter (chosen by Model A action picker) and the `drop_internal` flag (removes UI helpers, aggregations, schema endpoints).

## Creating a tenant-specific subset

Use this when a tenant should see a different (usually smaller) set of tools than `_default`. Typical case: post-launch telemetry shows the tenant only ever exercises ~30 daily-use tools — narrowing to that subset improves routing accuracy (smaller cosine pool, less LLM disambiguation).

```bash
# 1. Find tenant UUID
grep MOBILITY_TENANT_ID .env

# 2. Create directory + file
mkdir -p config/tenants/{uuid}
cat > config/tenants/{uuid}/tool_subset.json <<'JSON'
{
  "tenant_id": "{uuid}",
  "allowed_tool_ids": [
    "get_MasterData",
    "post_AddMileage",
    "post_VehicleCalendar"
  ]
}
JSON

# 3. Deploy — mtime-cached, picks up on next request, no restart needed
git add config/tenants/{uuid}/tool_subset.json
git commit -m "feat: tenant {uuid} tool subset"
```

## Security note

Tool whitelisting here is a **routing-accuracy aid**, not a security boundary. The MobilityOne backend enforces ACL via OAuth scope (HTTP 403 for unauthorized calls). Removing a tool from `tool_subset.json` only hides it from the router's candidate set — it does NOT grant or revoke any permission.

## History

Per-tenant `personas.json` (phone → role mapping) used to live here. It was removed 2026-05-28 (Filip) — backend OAuth scope is the real ACL, we had no reliable role source, and the persona filter had been a no-op since 2026-05-22. The `personas_strict` field stays in the tool registry as harmless metadata for audit/benchmark scripts.
