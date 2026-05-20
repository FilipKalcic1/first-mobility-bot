#!/bin/bash
# Live smoke test — drives a real WhatsApp message through the full stack.
#
# WHAT THIS PROVES (vs. what unit tests prove):
#   - Tests prove each segment works against fakes.
#   - This proves the docker-compose stack actually boots and routes one
#     real message through webhook → Redis stream → worker → V2Engine →
#     Azure OpenAI → outbound queue. Real Redis, real Postgres, real LLM.
#
# PREREQUISITES:
#   1. .env populated (AZURE_OPENAI_*, INFOBIP_*, DATABASE_URL pointing at
#      a writable Postgres, REDIS_URL).
#   2. docker-compose accessible from this directory.
#   3. Your phone number registered in MobilityOne's dev backend
#      (/Persons API returns a row with your phone).
#   4. `jq` and `curl` installed on the host.
#
# WHAT IT DOES NOT TEST:
#   - The real Infobip → real WhatsApp delivery (would need a real Meta
#     subscription + a real phone you control). We watch the outbound
#     queue in Redis instead — if the bot's reply lands there, the only
#     remaining link is Infobip's HTTP POST which is covered by
#     tests/test_whatsapp_service.py.
#
# USAGE:
#   bash scripts/smoke_live.sh +385955087196 "koja je moja kilometraža"
#
# Returns 0 on success, non-zero with a clear error message on failure.

set -euo pipefail

PHONE="${1:-}"
MESSAGE="${2:-koja je moja kilometraža}"

if [[ -z "$PHONE" ]]; then
  echo "ERROR: phone number required as first argument" >&2
  echo "usage: bash scripts/smoke_live.sh +385955087196 \"koja je moja kilometraža\"" >&2
  exit 2
fi

API_BASE="${API_BASE:-http://localhost:8000}"
TENANT_ID="${TENANT_ID:-}"  # if empty, we'll discover via Persons API at runtime
STREAM_KEY="whatsapp_stream_inbound"
OUTBOUND_KEY="whatsapp_outbound"
MSG_ID="smoke_$(date +%s)_$$"

echo "============================================================"
echo " LIVE SMOKE — Croatian WhatsApp bot E2E"
echo " phone:   $PHONE"
echo " message: $MESSAGE"
echo " msg_id:  $MSG_ID"
echo " api:     $API_BASE"
echo "============================================================"

# --- Step 1: docker-compose up + wait ----------------------------------
echo
echo "[1/6] Ensuring docker-compose stack is up..."
if ! docker-compose ps 2>/dev/null | grep -qE 'mobility_(api|worker|postgres|redis).*Up'; then
  echo "  stack not running — bringing it up (may take 60-90s)..."
  docker-compose up -d
fi

echo "  waiting for /ready..."
for i in {1..60}; do
  if curl -fsS "$API_BASE/ready" >/dev/null 2>&1; then
    echo "  /ready OK after ${i}s"
    break
  fi
  if [[ $i -eq 60 ]]; then
    echo "ERROR: api never became ready. Check: docker-compose logs api" >&2
    exit 3
  fi
  sleep 1
done

# --- Step 2: ensure user_mappings has this phone -----------------------
echo
echo "[2/6] Checking user_mappings for phone $PHONE..."
EXISTING=$(docker-compose exec -T postgres psql -U bot_user -d mobility_db -tA -c \
  "SELECT tenant_id FROM user_mappings WHERE phone_number = '$PHONE' AND is_active = 1 LIMIT 1;" \
  2>/dev/null || echo "")

if [[ -z "$EXISTING" ]]; then
  if [[ -z "$TENANT_ID" ]]; then
    echo "ERROR: phone $PHONE not in user_mappings and TENANT_ID env not set." >&2
    echo "Either seed manually:" >&2
    echo "  docker-compose exec postgres psql -U bot_user -d mobility_db -c \\" >&2
    echo "    \"INSERT INTO user_mappings (id, phone_number, tenant_id, is_active) \\" >&2
    echo "     VALUES ('smoke-$$', '$PHONE', '<your-tenant-uuid>', 1);\"" >&2
    echo "Or set TENANT_ID=<uuid> and re-run this script." >&2
    exit 4
  fi
  echo "  seeding user_mappings: phone=$PHONE, tenant=$TENANT_ID"
  docker-compose exec -T postgres psql -U bot_user -d mobility_db -c \
    "INSERT INTO user_mappings (id, phone_number, tenant_id, is_active) \
     VALUES ('smoke-$$', '$PHONE', '$TENANT_ID', 1) \
     ON CONFLICT (phone_number) DO UPDATE SET tenant_id = EXCLUDED.tenant_id, is_active = 1;" \
    >/dev/null
  EXISTING="$TENANT_ID"
fi
echo "  tenant_id resolved: $EXISTING"

# --- Step 3: snapshot outbound queue length BEFORE ---------------------
BEFORE_LEN=$(docker-compose exec -T redis redis-cli LLEN "$OUTBOUND_KEY" | tr -d '\r')
BEFORE_LEN=${BEFORE_LEN:-0}
echo
echo "[3/6] Outbound queue length before: $BEFORE_LEN"

# --- Step 4: POST a fake Infobip webhook ------------------------------
echo
echo "[4/6] POSTing Infobip-style webhook to $API_BASE/whatsapp..."
PAYLOAD=$(cat <<EOF
{
  "results": [{
    "from": "$PHONE",
    "to": "12172817448",
    "integrationType": "WHATSAPP",
    "receivedAt": "$(date -u +%Y-%m-%dT%H:%M:%S.000+0000)",
    "messageId": "$MSG_ID",
    "message": { "type": "TEXT", "text": "$MESSAGE" }
  }]
}
EOF
)

HTTP_CODE=$(curl -sS -o /tmp/smoke_resp.json -w '%{http_code}' \
  -X POST "$API_BASE/whatsapp" \
  -H "Content-Type: application/json" \
  -d "$PAYLOAD")

if [[ "$HTTP_CODE" != "200" ]]; then
  echo "ERROR: webhook returned HTTP $HTTP_CODE" >&2
  cat /tmp/smoke_resp.json >&2
  exit 5
fi
echo "  HTTP 200 OK"

# --- Step 5: wait for reply in outbound queue --------------------------
echo
echo "[5/6] Waiting up to 30s for bot reply in $OUTBOUND_KEY..."
for i in {1..30}; do
  NOW_LEN=$(docker-compose exec -T redis redis-cli LLEN "$OUTBOUND_KEY" | tr -d '\r')
  NOW_LEN=${NOW_LEN:-0}
  if [[ "$NOW_LEN" -gt "$BEFORE_LEN" ]]; then
    echo "  outbound grew from $BEFORE_LEN to $NOW_LEN after ${i}s"
    break
  fi
  if [[ $i -eq 30 ]]; then
    echo "ERROR: no reply in outbound after 30s." >&2
    echo "Last 30 worker log lines:" >&2
    docker-compose logs --tail=30 worker >&2
    exit 6
  fi
  sleep 1
done

# --- Step 6: read reply ------------------------------------------------
echo
echo "[6/6] Reply payload (newest entry):"
docker-compose exec -T redis redis-cli LRANGE "$OUTBOUND_KEY" -1 -1 | tee /tmp/smoke_reply.txt
echo

REPLY_TEXT=$(jq -r '.text' /tmp/smoke_reply.txt 2>/dev/null || cat /tmp/smoke_reply.txt)
echo "============================================================"
echo " SMOKE OK"
echo " Croatian reply: $REPLY_TEXT"
echo "============================================================"
