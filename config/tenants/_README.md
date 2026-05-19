# Per-tenant configuration

Each tenant subdirectory may contain:

| File | Purpose | Required? | Loaded by |
|---|---|---|---|
| `tool_subset.json` | Whitelist of `operation_id`s this tenant is allowed to call (additional layer on top of registry `personas_strict`). Falls back to `_default/tool_subset.json` if missing. | Optional | [services/router/catalog_scoper.py](../../services/router/catalog_scoper.py) `_tenant_subset()` |
| `personas.json` | Phone → role map. Phone numbers in this file get a non-default persona. Phones not listed default to `"driver"` (safest minimum scope). | Optional but **strongly recommended** for production | [services/v2/identity.py](../../services/v2/identity.py) `_resolve_persona()` |

## How to create `personas.json` for a new tenant (FAZA 14 setup)

### Step 1 — Find tenant UUID

```bash
# Option A: from .env
grep MOBILITY_TENANT_ID .env

# Option B: from Python REPL (project root)
python -c "from config import settings; print(settings.MOBILITY_TENANT_ID)"
```

Result is a UUID like `12345678-abcd-ef01-2345-67890abcdef0`.

### Step 2 — Create directory

```bash
mkdir -p config/tenants/{uuid}
```

(Replace `{uuid}` with the real value.)

### Step 3 — Create `personas.json`

File format: simple JSON object mapping E.164 phone → role.

```json
{
  "+385951234567": "admin",
  "+385952345678": "manager",
  "+385953456789": "driver"
}
```

**Valid persona values** (Faza 14 hierarchy):
- `"driver"` — sees 18 tools (driver scope only)
- `"manager"` — sees 245 tools (driver + manager scope, FAZA 14 hierarchy)
- `"admin"` — sees 481 tools (all user-facing — driver + manager + admin)

**Default for unlisted phones**: `"driver"` (most restrictive — safest, see [services/v2/identity.py:55](../../services/v2/identity.py#L55)).

### Step 4 — Validate format

```bash
python -c "import json; json.load(open('config/tenants/{uuid}/personas.json'))"
# No output = valid JSON
```

### Step 5 — Commit + deploy

```bash
git add config/tenants/{uuid}/personas.json
git commit -m "feat: add personas.json for tenant {uuid} (Faza 14)"
git push origin <branch>

# On bot.damir.com:
ssh azureuser@bot.damir.com "cd /path/to/nova-verzija && git pull && docker compose restart api worker"
```

The personas.json is mtime-cached, so the first request after restart picks up the new mapping; no rebuild needed.

## Phone number formatting

E.164 format with leading `+`:
- ✅ `+385951234567` (international, with `+`)
- ❌ `00385951234567` (with `00` prefix)
- ❌ `0951234567` (national, no country code)
- ❌ `385951234567` (no `+`)

[services/v2/identity.py](../../services/v2/identity.py) normalizes incoming phones, but the lookup key in `personas.json` must match the normalized form (with leading `+`).

## Why this file is not auto-generated

MobilityOne `/Persons` endpoint doesn't yet expose role data per phone number, so the bot can't infer persona from upstream. This file is a manual override until backend adds the field.

## Security note

The bot's MobilityOne ACL is enforced **server-side** (returns 403 for unauthorized calls). This file only affects **routing accuracy** — narrower candidate set means better cosine + LLM disambiguation. Setting someone to `"driver"` does NOT grant elevated privileges if backend rejects them.
