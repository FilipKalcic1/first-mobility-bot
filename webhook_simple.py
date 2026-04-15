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
      - Monitorable via: LLEN dlq:webhook (Prometheus alert on >10)

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

    # Last resort: stderr (may be captured by log aggregator)
    sys.stderr.write(f"DLQ_WEBHOOK: {dlq_entry}\n")
    sys.stderr.flush()


# ---
# MAIN WEBHOOK HANDLER
# ---

@router.post("/whatsapp")
async def whatsapp_webhook(request: Request):
    """
    Receive WhatsApp webhook messages and push to Redis STREAM.

    Flow:
    1. Validate webhook signature (if enabled)
    2. Parse JSON body
    3. Extract message data (sender, text, message_id) from ALL formats
    4. VALIDATE sender is present
    5. Handle non-text messages (forward type info to Redis)
    6. Push to Redis STREAM: "whatsapp_stream_inbound"
    7. Worker picks up from stream via consumer group
    """
    _stats["total_received"] += 1

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

            # ── Option C: edge tenant resolution (V6.2 strict isolation) ──
            # Look up TenantId from phone BEFORE any API call. Unknown phones
            # are refused at the edge — no env fallback, no Redis stream push,
            # no worker work. Saves resources and forecloses cross-tenant bleed.
            tenant_id = await resolve_tenant_for_phone(sender)
            if not tenant_id:
                logger.warning(
                    f"TENANT_REFUSED unknown phone ***{sender[-4:]} — refusing at edge"
                )
                _diag_log("tenant_refused", {"sender_suffix": sender[-4:]})
                refusal_payload = json.dumps({
                    "to": sender,
                    "text": TENANT_REFUSAL_TEXT_HR,
                    "idempotency_key": (
                        f"{sender}:tenant_refused:"
                        f"{hashlib.md5(message_id.encode() if message_id else sender.encode(), usedforsecurity=False).hexdigest()[:12]}"
                    ),
                })
                try:
                    redis = await get_redis()
                    await redis.rpush("whatsapp_outbound", refusal_payload)
                except (ConnectionError, TimeoutError, OSError, RedisConnectionError, RedisError) as redis_err:
                    logger.error(f"Tenant refusal enqueue failed: {redis_err}")
                continue

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
                }

                for redis_attempt in range(3):
                    try:
                        redis = await get_redis()
                        await redis.xadd("whatsapp_stream_inbound", stream_data)
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
            }

            # Push with retry (max 3 attempts with exponential backoff)
            for redis_attempt in range(3):
                try:
                    redis = await get_redis()
                    await redis.xadd("whatsapp_stream_inbound", stream_data)
                    pushed += 1
                    _stats["total_pushed"] += 1
                    _stats["last_success_at"] = datetime.now(timezone.utc).isoformat()
                    logger.info(f"Message pushed to stream: {sender[-4:]}... - {text[:50]}")
                    _diag_log("pushed", {"sender": sender[-4:], "text_preview": text[:30]})
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

                    logger.error(
                        f"REDIS PUSH FAILED after 3 attempts - MESSAGE TO DLQ! "
                        f"sender={sender[-4:]}, text={text[:100]}, message_id={message_id}, "
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
async def whatsapp_webhook_verify(request: Request):
    """
    Webhook verification / health check endpoint.

    - Simple GET (no params): returns 200 OK (for Infobip URL validation)
    - With hub.mode params: Meta-style verification (if WHATSAPP_VERIFY_TOKEN set)
    """
    mode = request.query_params.get("hub.mode")

    # No hub.mode param = simple health check (Infobip just pings the URL)
    if not mode:
        return {"status": "ok", "webhook": "active"}

    # Meta-style verification (hub.mode + hub.verify_token + hub.challenge)
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")
    expected_token = settings.WHATSAPP_VERIFY_TOKEN

    if not expected_token:
        logger.warning("WHATSAPP_VERIFY_TOKEN not configured, skipping verification")
        return {"status": "ok"}

    if mode == "subscribe" and hmac.compare_digest(token or "", expected_token):
        logger.info("WhatsApp webhook verified successfully")
        if not challenge:
            raise HTTPException(status_code=400, detail="Missing challenge parameter")
        # Return challenge as-is (Meta/Infobip sends strings, not necessarily ints)
        return PlainTextResponse(content=str(challenge), status_code=200)

    logger.warning(f"Webhook verification failed: mode={mode}")
    raise HTTPException(status_code=403, detail="Verification failed")


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
    # Auth: require admin token via query param
    # Reads ADMIN_TOKEN_1..4 from env (same as admin_api.py)
    token = request.query_params.get("token", "")
    expected_tokens = set()
    for i in range(1, 5):
        env_token = os.environ.get(f"ADMIN_TOKEN_{i}")
        if env_token:
            expected_tokens.add(env_token)

    token_bytes = (token or "").encode()
    valid = False
    for stored in expected_tokens:
        if hmac.compare_digest(token_bytes, stored.encode()):
            valid = True  # No break — iterate all tokens for constant-time
    if not expected_tokens or not valid:
        raise HTTPException(status_code=404, detail="Not found")

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
