"""
Simple webhook endpoint for WhatsApp messages (Infobip).
Receives messages and pushes to Redis queue for worker processing.

- Handles ALL known Infobip payload formats
- Case-insensitive type matching
- Non-text message forwarding (image, location, voice → user gets response)
- Webhook-level DLQ for Redis failures
- Diagnostic endpoint for remote debugging
- Request ID tracking for log correlation

Security:
- Webhook signature validation (HMAC-SHA256) when VERIFY_WHATSAPP_SIGNATURE=True
- Verify token from environment variable WHATSAPP_VERIFY_TOKEN
"""

import asyncio
import hmac
import hashlib
import json
import os
import sys
from collections import deque
from datetime import datetime, timezone
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import PlainTextResponse
import redis.asyncio as aioredis
try:
    from redis.exceptions import (
        ConnectionError as RedisConnectionError,
        RedisError,
        ResponseError,
    )
    # Guard: test stubs may inject MagicMocks that aren't real exception classes
    if not (isinstance(RedisConnectionError, type) and issubclass(RedisConnectionError, BaseException)):
        raise TypeError("redis.exceptions returned stub types")
except Exception:
    RedisConnectionError = OSError  # type: ignore[assignment,misc]
    RedisError = OSError  # type: ignore[assignment,misc]
    class ResponseError(Exception):  # type: ignore[assignment]
        """Sentinel — never raised; safe fallback when redis.exceptions unavailable."""
import logging

from config import get_settings
from services.tracing import get_tracer, trace_span
from services.tenant_resolver import resolve_tenant_for_phone

# Refusal sent to unknown phone numbers. Single source of truth so the e2e
# test can assert on it without duplicating the string.
TENANT_REFUSAL_TEXT_HR = (
    "Vaš broj nije registriran u MobilityOne sustavu. "
    "Kontaktirajte vašeg administratora za pristup."
)

settings = get_settings()
logger = logging.getLogger(__name__)
_tracer = get_tracer(__name__)

# ---
# DIAGNOSTIC RING BUFFER - Last N webhook events for remote debugging
# ---
_MAX_DIAG_ENTRIES = 50
_diag_buffer: deque = deque(maxlen=_MAX_DIAG_ENTRIES)
# Stats lock: writes are atomic in asyncio's cooperative model (no await between
# read-modify-write), but the lock ensures a consistent snapshot on read.
_stats_lock = asyncio.Lock()
_stats = {
    "total_received": 0,
    "total_pushed": 0,
    "total_no_text": 0,
    "total_no_sender": 0,
    "total_no_results": 0,
    "total_redis_errors": 0,
    "total_parse_errors": 0,
    "last_success_at": None,
    "last_error_at": None,
    "last_error": None,
    "started_at": datetime.now(timezone.utc).isoformat(),
}


def _diag_log(event: str, data: dict = None):
    """Log to diagnostic ring buffer for remote debugging."""
    entry = {
        "ts": datetime.now(timezone.utc).strftime("%H:%M:%S"),
        "event": event,
        **(data or {})
    }
    _diag_buffer.append(entry)


# ---
# SIGNATURE VALIDATION
# ---

def verify_webhook_signature(payload: bytes, signature: str, secret: str) -> bool:
    """
    Verify Infobip webhook signature using HMAC-SHA256.

    Fails closed: any missing input (secret, signature) returns False.
    Never bypasses verification — INFOBIP_SECRET_KEY must be configured
    whenever the webhook endpoint is reachable.
    """
    if not secret:
        logger.error(
            "INFOBIP_SECRET_KEY missing — rejecting webhook. "
            "Set the secret in environment to enable WhatsApp webhook processing."
        )
        return False

    if not signature:
        logger.warning("No signature header in webhook request — rejecting")
        return False

    if signature.startswith("sha256="):
        signature = signature[7:]

    expected = hmac.new(
        secret.encode('utf-8'),
        payload,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(expected.lower(), signature.lower())


# ---
# TEXT EXTRACTION - handles ALL known Infobip formats
# ---

def extract_text_and_type(result: dict) -> tuple:
    """
    Extract text and message type from Infobip webhook result.

    Handles ALL known formats:
    1. {"message": {"type": "TEXT", "text": "..."}}          - Standard Infobip
    2. {"message": {"type": "text", "text": "..."}}          - Lowercase variant
    3. {"content": [{"type": "TEXT", "text": "..."}]}        - Content as list
    4. {"content": {"type": "TEXT", "text": "..."}}          - Content as dict
    5. {"text": "..."}                                        - Direct text field
    6. {"message": {"text": "..."}}                           - Message without type
    7. {"body": "..."}                                        - Body field variant

    Returns:
        (text: str, msg_type: str) - text is empty string if no text found
    """
    text = ""
    msg_type = "UNKNOWN"

    # Format 1 & 2: message object (most common Infobip format)
    message_obj = result.get("message")
    if message_obj and isinstance(message_obj, dict):
        msg_type = message_obj.get("type") or "UNKNOWN"

        # Case-insensitive type check
        if str(msg_type).upper() == "TEXT":
            text = message_obj.get("text", "")
            if isinstance(text, str) and text.strip():
                return text.strip(), "TEXT"

        # Format 6: message object without type field but with text
        if not text and "text" in message_obj:
            text = message_obj.get("text", "")
            if isinstance(text, str) and text.strip():
                return text.strip(), msg_type or "TEXT"

        # Non-text message - return type for handling
        return "", str(msg_type)

    # Format 3 & 4: content field
    content = result.get("content")
    if content:
        if isinstance(content, dict):
            content_type = content.get("type") or ""
            if str(content_type).upper() == "TEXT":
                text = content.get("text", "")
                if isinstance(text, str) and text.strip():
                    return text.strip(), "TEXT"
            return "", str(content_type) or "UNKNOWN"

        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict):
                    item_type = item.get("type") or ""
                    if str(item_type).upper() == "TEXT":
                        text = item.get("text", "")
                        if isinstance(text, str) and text.strip():
                            return text.strip(), "TEXT"
            # Return type of first item if no text found
            if content and isinstance(content[0], dict):
                return "", content[0].get("type", "UNKNOWN")

    # Format 5: direct text field on result
    direct_text = result.get("text")
    if direct_text and isinstance(direct_text, str):
        return direct_text.strip(), "TEXT"

    # Format 7: body field
    body = result.get("body")
    if body and isinstance(body, str):
        return body.strip(), "TEXT"

    return "", msg_type


# ---
# REDIS CLIENT
# ---

router = APIRouter()

_redis_client = None
_redis_lock = asyncio.Lock()

# DLQ file path - survives Redis outage, lost on pod eviction (last resort)
_DLQ_FILE_PATH = "/tmp/dlq.jsonl"
_DLQ_FILE_MAX_BYTES = 5 * 1024 * 1024  # 5MB cap (tmpfs is 10-50Mi)


async def get_redis():
    """Get async Redis client (thread-safe lazy initialization with pool config)."""
    global _redis_client
    if _redis_client is not None:
        return _redis_client

    async with _redis_lock:
        if _redis_client is not None:
            return _redis_client
        _redis_client = await aioredis.from_url(
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            max_connections=5,  # Webhook only pushes to stream — 5 sufficient for single-server
            socket_keepalive=True,
            health_check_interval=30
        )
        return _redis_client


async def _write_dlq(dlq_entry: str) -> None:
    """
    Write failed message to durable DLQ storage.

    Primary: Redis LPUSH to 'dlq:webhook' list.
      - Survives pod restarts (data lives in Redis)
      - Recoverable via: LRANGE dlq:webhook 0 -1
      - Monitorable via: LLEN dlq:webhook (KQL alert when >10)

    Fallback: Append to /tmp/dlq.jsonl (if Redis is completely unreachable).
      - Survives Redis outage within the pod's lifetime
      - Lost on pod eviction (but that's the scenario where Redis was already gone)
      - Log aggregator (Fluentd/Loki) can also capture from stderr as last resort
    """
    # Try Redis first - this is the durable path
    try:
        redis = await get_redis()
        if redis:
            await redis.lpush("dlq:webhook", dlq_entry)
            # ltrim/expire are best-effort maintenance — failure doesn't
            # un-store the message, so we return immediately regardless.
            try:
                await redis.ltrim("dlq:webhook", 0, 9999)  # Cap at 10K entries
                await redis.expire("dlq:webhook", 2592000)  # 30-day TTL for GDPR retention
            except Exception as maint_err:
                logger.warning(f"DLQ Redis ltrim/expire failed (message stored, maintenance skipped): {maint_err}")
            logger.info("DLQ message stored in Redis (dlq:webhook)")
            return
    except (ConnectionError, TimeoutError, OSError, RedisConnectionError, RedisError) as redis_dlq_err:
        logger.warning(f"DLQ Redis write failed, falling back to file: {redis_dlq_err}")
    except Exception as redis_dlq_err:
        logger.error(f"DLQ Redis unexpected error (possible bug): {type(redis_dlq_err).__name__}: {redis_dlq_err}")

    # Fallback: local file (atomic append with newline delimiter)
    try:
        # Check file size before writing to prevent tmpfs exhaustion
        try:
            file_size = os.path.getsize(_DLQ_FILE_PATH)
        except OSError:
            file_size = 0

        if file_size >= _DLQ_FILE_MAX_BYTES:
            logger.error(f"DLQ file at {file_size} bytes exceeds {_DLQ_FILE_MAX_BYTES} cap, skipping file write")
        else:
            with open(_DLQ_FILE_PATH, "a", encoding="utf-8") as f:
                f.write(dlq_entry + "\n")
                f.flush()
                os.fsync(f.fileno())
            logger.info(f"DLQ message stored in {_DLQ_FILE_PATH}")
            return
    except OSError as file_err:
        logger.error(f"DLQ file write ALSO failed: {file_err}")
    except Exception as file_err:
        logger.error(f"DLQ file write unexpected error (possible bug): {type(file_err).__name__}: {file_err}")

    # Last resort: stderr (may be captured by log aggregator). Mask PII —
    # never dump raw sender/text to stdout/stderr (GDPR). The full entry is
    # already attempted in Redis + file (access-controlled); stderr is just a
    # "we lost one here" breadcrumb for ops.
    try:
        _e = json.loads(dlq_entry)
        masked = (
            f"dlq={_e.get('dlq')} sender=***{str(_e.get('sender', ''))[-4:]} "
            f"msg_id={_e.get('message_id')} text_len={len(_e.get('text') or '')} "
            f"error={_e.get('error')}"
        )
    except Exception:  # noqa: BLE001 — masking must never break the last-resort path
        masked = "DLQ entry (unparseable, masked for PII)"
    sys.stderr.write(f"DLQ_WEBHOOK: {masked}\n")
    sys.stderr.flush()


# ---
# MAIN WEBHOOK HANDLER
# ---

# Per-IP rate limiter for the webhook endpoint. Defends against:
#   - DoS flood (Redis stream growth, Azure cost burst from 1000+ msg/sec
#     downstream)
#   - Brute-force HMAC discovery (when VERIFY_WHATSAPP_SIGNATURE=True)
# Limits: 200 requests / 60s / IP. Realistic Infobip throughput per
# webhook source IP is well under that. Trusted IPs (Infobip CIDR) are
# rate-limited the same way — Infobip itself stays well below the cap
# under normal traffic, so the limit only bites on actual flood.
import time as _wh_time
from collections import deque as _wh_deque

_WH_RATE_BUCKET: dict[str, _wh_deque] = {}
_WH_RATE_LIMIT = 200
_WH_RATE_WINDOW_S = 60


def _webhook_check_ip(ip: str, *, now: float | None = None) -> bool:
    """Return True if IP is within rate-limit budget; False if blocked.
    Records the request on success."""
    n = _wh_time.time() if now is None else now
    cutoff = n - _WH_RATE_WINDOW_S
    bucket = _WH_RATE_BUCKET.get(ip)
    if bucket is None:
        bucket = _wh_deque(maxlen=_WH_RATE_LIMIT * 2)
        _WH_RATE_BUCKET[ip] = bucket
    while bucket and bucket[0] < cutoff:
        bucket.popleft()
    if len(bucket) >= _WH_RATE_LIMIT:
        return False
    bucket.append(n)
    return True


@router.post("/whatsapp")
async def whatsapp_webhook(request: Request):
    """
    Receive WhatsApp webhook messages and push to Redis STREAM.

    Flow:
    1. Per-IP rate-limit check (200/60s/IP)
    2. Validate webhook signature (if enabled)
    3. Parse JSON body
    4. Extract message data (sender, text, message_id) from ALL formats
    5. VALIDATE sender is present
    6. Handle non-text messages (forward type info to Redis)
    7. Push to Redis STREAM: "whatsapp_stream_inbound"
    8. Worker picks up from stream via consumer group
    """
    _stats["total_received"] += 1

    # Rate limit BEFORE any work — cheapest possible reject path.
    client_ip = request.client.host if request.client else "unknown"
    if not _webhook_check_ip(client_ip):
        logger.warning(
            "webhook rate-limited ip=%s (cap %d/%ds)",
            client_ip, _WH_RATE_LIMIT, _WH_RATE_WINDOW_S,
        )
        raise HTTPException(status_code=429, detail="rate limit")

    # Extract request_id from middleware for end-to-end tracing
    request_id = getattr(request.state, "request_id", "") if hasattr(request, "state") else ""

    with trace_span(_tracer, "webhook.receive", {
        "request.id": request_id,
        "http.method": "POST",
    }) as wh_span:
        return await _process_webhook(request, request_id, wh_span)


async def _process_webhook(request: Request, request_id: str, span) -> dict:
    """Inner webhook processing, wrapped by trace span."""
    global _redis_client
    # Graceful shutdown: reject new messages during drain.
    # The worker and Redis may already be stopping. Accepting messages
    # now and returning 200 to Infobip would cause permanent loss.
    # Returning 503 tells Infobip to retry after the pod restarts.
    try:
        from main import APP_STOPPING
        if APP_STOPPING:
            logger.warning("Webhook rejected: APP_STOPPING=True (graceful shutdown)")
            raise HTTPException(status_code=503, detail="Shutting down")
    except ImportError:
        pass

    try:
        # Get raw body for signature validation
        raw_body = await request.body()

        # Validate signature if enabled
        if settings.VERIFY_WHATSAPP_SIGNATURE:
            signature = request.headers.get("X-Hub-Signature-256", "")
            if not verify_webhook_signature(raw_body, signature, settings.INFOBIP_SECRET_KEY):
                logger.warning(
                    f"Invalid webhook signature from {request.client.host if request.client else 'unknown'}"
                )
                _diag_log("signature_failed", {"ip": request.client.host if request.client else "unknown"})
                raise HTTPException(status_code=401, detail="Invalid signature")

        # Parse JSON body
        try:
            body = json.loads(raw_body)
        except (json.JSONDecodeError, ValueError) as e:
            _stats["total_parse_errors"] += 1
            logger.error(f"Invalid JSON in webhook body: {e}")
            _diag_log("json_parse_error", {"error": str(e), "body_preview": raw_body[:200].decode(errors='replace')})
            return {"status": "ok", "error": "invalid_json"}

        logger.info(
            f"Received WhatsApp webhook: keys={list(body.keys())} "
            f"results={len(body.get('results', []))}"
        )
        _diag_log("received", {"keys": list(body.keys()), "result_count": len(body.get("results", []))})

        # Extract message details from Infobip format
        results = body.get("results", [])
        if not results:
            _stats["total_no_results"] += 1
            logger.warning(f"No results in webhook body. Keys: {list(body.keys())}")
            _diag_log("no_results", {"body_keys": list(body.keys())})
            return {"status": "ok", "note": "no_results"}

        pushed = 0
        for result in results:
            if not isinstance(result, dict):
                logger.error(f"Invalid result item type: {type(result).__name__}, skipping")
                continue

            # Infobip uses "from", legacy/test may use "sender"
            # Try ALL known sender field names from Infobip formats
            sender = (
                result.get("from")
                or result.get("sender")
                or result.get("phoneNumber")
                or result.get("phone")
                or (result.get("contact", {}).get("phone", "") if isinstance(result.get("contact"), dict) else "")
                or ""
            )
            message_id = result.get("messageId", result.get("message_id", ""))

            # CRITICAL: Validate sender is present
            if not sender:
                _stats["total_no_sender"] += 1
                logger.error(
                    "MISSING SENDER in webhook! "
                    f"message_id={message_id}, keys={list(result.keys())}"
                )
                _diag_log("no_sender", {"keys": list(result.keys()), "message_id": message_id})
                continue

            # ── Edge tenant resolution (V6.2 strict isolation, with onboarding) ──
            # Look up TenantId from phone BEFORE any API call.
            #
            # Path A: tenant known → push with tenant_id, normal worker processing.
            # Path B: tenant unknown → push with empty tenant_id, marked
            #   needs_onboarding=true. Worker calls try_auto_onboard via the
            #   MobilityOne /Persons API; on success the user_mapping is created
            #   and subsequent messages take Path A. On failure the worker
            #   responds with the standard "not registered" message via the
            #   GDPR/consent gate.
            #
            # To prevent a flood of unknown senders from hammering MobilityOne,
            # we cache the "tried-and-not-found" verdict for 5 minutes via the
            # worker's identify_user (which already does this through user_service).
            # The webhook stays fast: zero API calls here.
            # GW-A4 fix (Filip 2026-05-20): wrap tenant resolution. Without
            # this, a transient Redis/DB blip raises → propagates to the outer
            # handler (:650) which returns 200 "to prevent retries" → Infobip
            # never retries → message PERMANENTLY LOST. Treating a resolve
            # failure as "unknown phone" pushes the message to the stream
            # anyway; the worker's onboarding path handles it gracefully.
            try:
                tenant_id = await resolve_tenant_for_phone(sender)
            except Exception as e:  # noqa: BLE001 — never drop a message on resolve failure
                logger.warning(
                    "tenant resolve failed for ***%s (%s) — treating as unknown",
                    sender[-4:], type(e).__name__,
                )
                tenant_id = None
            needs_onboarding = not tenant_id
            if needs_onboarding:
                logger.info(
                    f"EDGE_UNKNOWN phone ***{sender[-4:]} — passing to worker for auto-onboard"
                )
                _diag_log("edge_unknown_passing_through", {"sender_suffix": sender[-4:]})
                tenant_id = ""  # Worker treats empty as "must onboard"

            # Extract text using robust multi-format parser
            text, msg_type = extract_text_and_type(result)

            if not text:
                _stats["total_no_text"] += 1
                logger.info(
                    f"Non-text message from {sender[-4:]}...: type={msg_type}"
                )
                _diag_log("non_text", {"sender": sender[-4:], "type": msg_type})

                # Forward non-text messages to Redis so worker can respond
                # "We only support text messages" is better than silence
                stream_data = {
                    "sender": sender,
                    "text": f"[NON_TEXT:{msg_type}]",
                    "message_id": message_id,
                    "original_type": msg_type,
                    "request_id": request_id,
                    "tenant_id": tenant_id,
                    "needs_onboarding": "1" if needs_onboarding else "0",
                }

                for redis_attempt in range(3):
                    try:
                        redis = await get_redis()
                        await redis.xadd(
                            "whatsapp_stream_inbound", stream_data,
                            maxlen=100_000, approximate=True,
                        )
                        pushed += 1
                        logger.info(f"Non-text message forwarded: {sender[-4:]}... type={msg_type}")
                        break
                    except (ConnectionError, TimeoutError, OSError, RedisConnectionError, RedisError) as redis_err:
                        if redis_attempt < 2:
                            await asyncio.sleep(0.5 * (2 ** redis_attempt))
                            async with _redis_lock:
                                _redis_client = None
                            continue
                        _stats["total_redis_errors"] += 1
                        logger.error(f"Redis push failed for non-text after 3 attempts: {redis_err}")
                        _diag_log("redis_error", {"error": str(redis_err), "type": msg_type})
                continue

            # Push text message to Redis STREAM
            stream_data = {
                "sender": sender,
                "text": text,
                "message_id": message_id,
                "request_id": request_id,
                "tenant_id": tenant_id,
                "needs_onboarding": "1" if needs_onboarding else "0",
            }

            # Pre-push deduplication.
            # Infobip retries webhooks ~500ms after delivery uncertainty.
            # Without this check, the SAME message_id can land in the
            # Redis stream twice; the worker-side msg_lock catches it,
            # but only after both copies are already enqueued. SET NX
            # with short TTL (60s) ensures only the FIRST delivery
            # makes it into the stream.
            if message_id:
                try:
                    redis = await get_redis()
                    dedup_ok = await redis.set(
                        f"wh_dedup:{message_id}", "1",
                        nx=True, ex=60,
                    )
                    if not dedup_ok:
                        logger.info(
                            "webhook duplicate skipped message_id=%s sender=%s",
                            message_id[:20], sender[-4:] if sender else "",
                        )
                        _diag_log("duplicate_skipped", {
                            "message_id": message_id[:20],
                            "sender": sender[-4:] if sender else "",
                        })
                        continue
                except Exception as redis_err:  # noqa: BLE001
                    # Dedup is opportunistic; on Redis failure we proceed
                    # (worker lock still catches duplicates). Log so ops
                    # see when the cheap pre-filter is degraded.
                    logger.warning(
                        "webhook dedup check failed (falling through): %s",
                        redis_err,
                    )

            # Push with retry (max 3 attempts with exponential backoff)
            for redis_attempt in range(3):
                try:
                    redis = await get_redis()
                    await redis.xadd(
                        "whatsapp_stream_inbound", stream_data,
                        maxlen=100_000, approximate=True,
                    )
                    pushed += 1
                    _stats["total_pushed"] += 1
                    _stats["last_success_at"] = datetime.now(timezone.utc).isoformat()
                    # EDGE-6 fix (Filip 2026-05-20): do NOT log message text —
                    # it lands in stdout → Log Analytics (90d retention, wide
                    # access) and may contain OIB/IBAN/address. Log only the
                    # masked sender + message_id for correlation. (DLQ entry
                    # below keeps full text — it's an access-controlled recovery
                    # store with 30d TTL, not broad logging.)
                    logger.info(
                        f"Message pushed to stream: {sender[-4:]}... "
                        f"msg_id={message_id} len={len(text)}"
                    )
                    _diag_log("pushed", {"sender": sender[-4:], "text_len": len(text)})
                    break

                except (ConnectionError, OSError, TimeoutError, RedisConnectionError, RedisError) as redis_err:
                    if redis_attempt < 2:
                        delay = 0.5 * (2 ** redis_attempt)  # 0.5s, 1.0s
                        logger.warning(
                            f"Redis push attempt {redis_attempt + 1}/3 failed: {redis_err}, retrying in {delay}s..."
                        )
                        await asyncio.sleep(delay)
                        # Reset Redis client on failure to force reconnect
                        # Must acquire lock to prevent concurrent handlers from
                        # using a half-torn-down client reference
                        async with _redis_lock:
                            _redis_client = None
                        continue

                    # All retries failed - DLQ
                    _stats["total_redis_errors"] += 1
                    _stats["last_error_at"] = datetime.now(timezone.utc).isoformat()
                    _stats["last_error"] = f"Redis push failed after 3 attempts: {redis_err}"

                    # EDGE-6 completion (Filip 2026-05-20): do NOT log raw text.
                    # PIIScrubFilter catches structured PII (IBAN/email/phone/
                    # keyword-OIB) but NOT bare numbers / names / addresses in
                    # free text. msg_id + len suffice for debugging; full text
                    # is preserved in the DLQ entry (access-controlled).
                    logger.error(
                        f"REDIS PUSH FAILED after 3 attempts - MESSAGE TO DLQ! "
                        f"sender={sender[-4:]}, msg_id={message_id}, len={len(text)}, "
                        f"error={redis_err}",
                        exc_info=True
                    )
                    _diag_log("redis_push_failed", {
                        "sender": sender[-4:],
                        "message_id": message_id,
                        "error": str(redis_err),
                        "attempts": 3
                    })

                    # DLQ: Durable storage for failed messages
                    # Primary: Redis LPUSH to dlq:webhook (survives pod restarts)
                    # Fallback: Local file /tmp/dlq.jsonl (survives Redis outage)
                    dlq_entry = json.dumps({
                        "dlq": "webhook",
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "sender": sender,
                        "text": text,
                        "message_id": message_id,
                        "error": str(redis_err)
                    })
                    await _write_dlq(dlq_entry)

        span.set_attribute("webhook.messages_pushed", pushed)
        return {"status": "ok", "pushed": pushed}

    except HTTPException:
        raise
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        # Data parsing/validation errors — not transient, log and move on
        _stats["last_error_at"] = datetime.now(timezone.utc).isoformat()
        _stats["last_error"] = f"Parse error: {type(e).__name__}: {e}"
        logger.error(f"Webhook data error (returning 200 to prevent retries): {type(e).__name__}: {e}", exc_info=True)
        _diag_log("data_error", {"error": str(e), "type": type(e).__name__})
        span.set_attribute("webhook.error", f"{type(e).__name__}: {e}")
        return {"status": "ok", "error": "data_error"}
    except Exception as e:
        # CRITICAL: Always return 200 to WhatsApp/Infobip!
        # Returning 500 causes retry storms that cascade into duplicate messages.
        # This broad catch is intentional — it's the last-resort safety net.
        _stats["last_error_at"] = datetime.now(timezone.utc).isoformat()
        _stats["last_error"] = str(e)
        logger.error(f"Webhook processing error (returning 200 to prevent retries): {e}", exc_info=True)
        _diag_log("exception", {"error": str(e)})
        span.record_exception(e)
        return {"status": "ok", "error": "processing_failed"}


# ---
# VERIFICATION & HEALTH CHECK
# ---

@router.get("/whatsapp")
async def whatsapp_webhook_health():
    """Webhook health check endpoint. Infobip pings this URL to validate setup."""
    return PlainTextResponse(content="ok", status_code=200)


# ---
# DIAGNOSTIC ENDPOINT - for remote debugging without SSH
# ---

@router.get("/whatsapp/debug")
async def webhook_debug(request: Request):
    """
    Diagnostic endpoint for remote debugging.

    SECURED: Requires ?token=ADMIN_TOKEN_1 query parameter.
    Returns 404 if token is missing or invalid (404 instead of 401
    to avoid revealing that the endpoint exists).

    Shows:
    - Webhook statistics (received, pushed, errors)
    - Last N events from ring buffer
    - Redis connection status
    - Stream info (pending messages, consumer groups)
    """
    # Auth: require admin token via query param.
    # 404 (not 401) avoids confirming the endpoint exists to unauth callers.
    from services.admin_auth import extract_bearer_or_query_token, verify_admin_token
    admin_user = verify_admin_token(extract_bearer_or_query_token(request))
    if not admin_user:
        raise HTTPException(status_code=404, detail="Not found")
    logger.info("admin_endpoint_call endpoint=debug user=%s ip=%s",
                admin_user,
                request.client.host if request.client else "unknown")

    async with _stats_lock:
        stats_snapshot = dict(_stats)
    diag = {
        "stats": stats_snapshot,
        "recent_events": list(_diag_buffer),
        "redis": {"status": "unknown"},
        "stream": {},
        "config": {
            "verify_signature": settings.VERIFY_WHATSAPP_SIGNATURE,
            "has_secret_key": bool(settings.INFOBIP_SECRET_KEY),
            "has_api_key": bool(settings.INFOBIP_API_KEY),
            "sender_number": settings.INFOBIP_SENDER_NUMBER,
            "redis_url_masked": (
                "redis://***@" + settings.REDIS_URL.split("@")[-1]
                if "@" in settings.REDIS_URL
                else "redis://<no-auth>"
            ) if settings.REDIS_URL else "not-configured",
        }
    }

    # Check Redis connection
    try:
        redis = await get_redis()
        await redis.ping()
        diag["redis"]["status"] = "connected"

        # Get stream info
        try:
            stream_info = await redis.xinfo_stream("whatsapp_stream_inbound")
            diag["stream"] = {
                "length": stream_info.get("length", 0),
                "first_entry": stream_info.get("first-entry"),
                "last_entry": stream_info.get("last-entry"),
            }
        except (ResponseError, RedisConnectionError, RedisError) as e:
            diag["stream"] = {"error": str(e)}

        # Get consumer group info
        try:
            groups = await redis.xinfo_groups("whatsapp_stream_inbound")
            diag["consumer_groups"] = [
                {
                    "name": g.get("name"),
                    "consumers": g.get("consumers"),
                    "pending": g.get("pending"),
                    "last_delivered": g.get("last-delivered-id"),
                }
                for g in groups
            ]
        except (ResponseError, RedisConnectionError, RedisError) as e:
            diag["consumer_groups"] = {"error": str(e)}

    except Exception as e:
        diag["redis"]["status"] = f"error: {e}"

    return diag


@router.get("/whatsapp/routing-log")
async def routing_accuracy_log(request: Request):
    """
    Production routing accuracy log — last 100 routing decisions for ONE tenant.

    SECURED: Requires ?token=ADMIN_TOKEN query parameter.
    REQUIRED: ?tenant=<tenant_id>  (R2: per-tenant scoping).
              Special value `__unscoped__` reads the legacy unscoped bucket
              for events that fired before identity resolution (input
              blocked, rate-limited, etc.) — operator must ask for it
              explicitly to prevent accidentally leaking across tenants.
    Optional: ?limit=N (default 100, max 500)
    """
    from services.admin_auth import extract_bearer_or_query_token, verify_admin_token
    from services.v2.telemetry import (
        ROUTING_LOG_LEGACY_KEY, redis_key_for_tenant,
    )
    admin_user = verify_admin_token(extract_bearer_or_query_token(request))
    if not admin_user:
        raise HTTPException(status_code=404, detail="Not found")

    tenant_id = request.query_params.get("tenant", "").strip()
    if not tenant_id:
        raise HTTPException(
            status_code=400,
            detail="tenant query param required (routing log is per-tenant)",
        )

    raw_limit = request.query_params.get("limit", "100")
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        limit = 100
    limit = max(1, min(limit, 500))

    if tenant_id == "__unscoped__":
        redis_key = ROUTING_LOG_LEGACY_KEY
    else:
        redis_key = redis_key_for_tenant(tenant_id)

    logger.info("admin_endpoint_call endpoint=routing-log user=%s tenant=%s ip=%s",
                admin_user, tenant_id,
                request.client.host if request.client else "unknown")

    try:
        redis_client = await get_redis()
        raw_entries = await redis_client.lrange(redis_key, 0, limit - 1)
        entries = []
        for raw in raw_entries:
            try:
                entries.append(json.loads(raw))
            except (json.JSONDecodeError, TypeError):
                pass
        return {
            "tenant_id": tenant_id,
            "redis_key": redis_key,
            "count": len(entries),
            "entries": entries,
        }
    except Exception as e:
        return {"error": str(e), "tenant_id": tenant_id, "count": 0, "entries": []}


@router.get("/admin/gdpr-requests")
async def gdpr_requests_log(request: Request):
    """Pending GDPR delete/export + human-handover requests for a tenant.

    Operators MUST act on these within 48h (GDPR Articles 17 & 20).

    SECURED: ?token=ADMIN_TOKEN  (or Authorization: Bearer)
    REQUIRED: ?tenant=<tenant_id>  (audit log is per-tenant)
    Optional: ?limit=N (default 100, max 500)
              ?kind=gdpr|handover|all (default gdpr)
    """
    from services.admin_auth import extract_bearer_or_query_token, verify_admin_token
    admin_user = verify_admin_token(extract_bearer_or_query_token(request))
    if not admin_user:
        raise HTTPException(status_code=404, detail="Not found")

    tenant_id = request.query_params.get("tenant", "").strip()
    if not tenant_id:
        raise HTTPException(status_code=400, detail="tenant query param required")

    raw_limit = request.query_params.get("limit", "100")
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        limit = 100
    limit = max(1, min(limit, 500))

    kind = request.query_params.get("kind", "gdpr").lower()
    if kind not in ("gdpr", "handover", "all"):
        raise HTTPException(status_code=400, detail="kind must be gdpr|handover|all")

    logger.info("admin_endpoint_call endpoint=gdpr-requests user=%s tenant=%s kind=%s ip=%s",
                admin_user, tenant_id, kind,
                request.client.host if request.client else "unknown")

    from services.v2.gdpr_audit import GdprAuditStore
    store = GdprAuditStore(await get_redis())

    out: dict = {"tenant_id": tenant_id, "gdpr": [], "handover": []}
    try:
        if kind in ("gdpr", "all"):
            out["gdpr"] = await store.list_gdpr_requests(tenant_id, limit)
        if kind in ("handover", "all"):
            out["handover"] = await store.list_handover_requests(tenant_id, limit)
        out["count"] = len(out["gdpr"]) + len(out["handover"])
        return out
    except Exception as e:
        return {"error": str(e), "tenant_id": tenant_id, "gdpr": [], "handover": [], "count": 0}


# ---------------------------------------------------------------------------
# Bot-side GDPR cleanup (Option B from the readiness plan)
# ---------------------------------------------------------------------------

# Redis key templates for the bot-side state. All are derived from prefixes
# defined in the per-store modules (kept in sync; tests/test_admin_auth or
# a future smoke test should detect drift). Listed here explicitly so the
# operator can read the endpoint and see EVERY key we touch.
_GDPR_BOT_KEYS = [
    "v2:identity:{phone}",           # identity snapshot
    "v2:pending_mut:{phone}",        # pending mutation confirm
    "v2:pending_mut:exec:{phone}",   # exec-lock for pending mutation
    "v2_pending_clarify:{phone}",    # top-3 cards state
    "v2_conv_history:{phone}",       # conversation history
    "v2:rl:m:{phone}",               # rate-limit per-minute counter
    "v2:rl:h:{phone}",               # rate-limit per-hour counter
    "tenant_phone:{phone}",          # tenant resolver cache
]


@router.post("/admin/gdpr-process")
async def gdpr_process(request: Request):
    """One-click bot-side GDPR cleanup. Operator still deletes in MobilityOne.

    SECURED: ?token=ADMIN_TOKEN (or Authorization: Bearer).
    REQUIRED: ?tenant=<tenant_id>&phone=<phone>
    Optional: ?dry_run=true to preview without deleting.

    Steps performed (bot-side only — MobilityOne data is the operator's
    responsibility):
      1. Validate tenant ↔ phone binding (refuse if mismatched)
      2. DELETE the user_mappings row (hard delete; GDPR record-of-erasure
         lives in gdpr:requests:{tenant_id} forever)
      3. DELETE all per-phone Redis keys (identity, pending state,
         conv history, rate-limit counters, tenant cache)
      4. Log the operation with the admin user + phone tail

    Returns a JSON summary of what was deleted plus a reminder to delete
    the Person record in MobilityOne admin UI (and Trips, MileageReports,
    VehicleCalendar, Expenses, Cases, etc. — see plan file).
    """
    from services.admin_auth import (
        extract_bearer_or_query_token, verify_admin_token,
    )
    from services.tenant_resolver import (
        get_tenant_resolver, _normalize_phone,
    )

    admin_user = verify_admin_token(extract_bearer_or_query_token(request))
    if not admin_user:
        raise HTTPException(status_code=404, detail="Not found")

    tenant_id = request.query_params.get("tenant", "").strip()
    phone_raw = request.query_params.get("phone", "").strip()
    dry_run = request.query_params.get("dry_run", "").lower() in ("1", "true", "yes")

    if not tenant_id or not phone_raw:
        raise HTTPException(
            status_code=400,
            detail="tenant and phone query params required",
        )

    phone = _normalize_phone(phone_raw)
    if not phone:
        raise HTTPException(status_code=400, detail="phone could not be normalized")

    resolver = await get_tenant_resolver()

    # Tenant ↔ phone binding sanity check (prevents operator from one tenant
    # accidentally deleting another tenant's user). Look up CURRENT binding
    # from Postgres directly — a stale cache entry must never decide an
    # erasure authorization.
    current_tenant = await resolver.resolve_tenant_for_phone(phone, bypass_cache=True)
    if current_tenant is None:
        # Mapping already gone (or user was never mapped). Still proceed
        # with Redis cleanup — there may be stale cache entries to remove.
        binding_status = "unmapped_or_already_deleted"
    elif current_tenant != tenant_id:
        raise HTTPException(
            status_code=409,
            detail=(
                f"phone is mapped to a different tenant — "
                f"operator must use the correct tenant for this user"
            ),
        )
    else:
        binding_status = "mapped_to_tenant"

    logger.warning(
        "admin_endpoint_call endpoint=gdpr-process user=%s tenant=%s "
        "phone_tail=***%s dry_run=%s binding=%s ip=%s",
        admin_user, tenant_id, phone[-4:], dry_run, binding_status,
        request.client.host if request.client else "unknown",
    )

    if dry_run:
        return {
            "dry_run": True,
            "tenant_id": tenant_id,
            "phone_tail": "***" + phone[-4:],
            "binding": binding_status,
            "would_delete_user_mappings": current_tenant is not None,
            "would_delete_redis_keys": [
                tmpl.format(phone=phone) for tmpl in _GDPR_BOT_KEYS
            ],
            "reminder": (
                "Then delete in MobilityOne admin UI: Person + Trips + "
                "MileageReports + VehicleCalendar + Expenses + Cases + "
                "PersonOrgUnits + PersonPeriodicActivities + TeamMembers."
            ),
        }

    # Actual deletion
    redis_client = await get_redis()
    keys_to_delete = [tmpl.format(phone=phone) for tmpl in _GDPR_BOT_KEYS]

    # Redis deletion — single DEL command for atomicity + reduced RTT.
    deleted_keys_count = 0
    try:
        deleted_keys_count = int(await redis_client.delete(*keys_to_delete) or 0)
    except Exception as e:
        logger.error("GDPR Redis delete failed for ***%s: %s", phone[-4:], e)
        # Continue to user_mappings purge — Redis failure is recoverable
        # via TTLs, but Postgres row is the legal-record concern.

    # Postgres user_mappings purge (hard DELETE).
    db_deleted_rows = 0
    db_error: str | None = None
    if current_tenant is not None:
        try:
            db_deleted_rows = await resolver.purge_user_mapping(phone)
        except Exception as e:  # noqa: BLE001
            db_error = f"{type(e).__name__}: {str(e)[:120]}"

    return {
        "dry_run": False,
        "tenant_id": tenant_id,
        "phone_tail": "***" + phone[-4:],
        "binding_before": binding_status,
        "user_mappings_rows_deleted": db_deleted_rows,
        "user_mappings_error": db_error,
        "redis_keys_attempted": keys_to_delete,
        "redis_keys_deleted_count": deleted_keys_count,
        "reminder": (
            "Bot-side cleanup complete. Now delete the user in MobilityOne "
            "admin UI: Person record + cascade (Trips, MileageReports, "
            "VehicleCalendar, Expenses, Cases, PersonOrgUnits, "
            "PersonPeriodicActivities, TeamMembers)."
        ),
    }
