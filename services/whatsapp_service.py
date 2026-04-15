"""
WhatsApp Integration Service

KRITIČNA KOMPONENTA - Rješava probleme:
1. Phone vs UUID trap: Validira da 'to' sadrži telefonski broj, ne UUID
2. Payload struktura: Infobip zahtijeva specifičnu strukturu
3. UTF-8 encoding: Osigurava sigurno enkodiranje poruka
4. Type guards: Sprječava slanje objekta umjesto stringa
5. Rate limiting & Backoff: Exponential backoff za 429 errore
6. Deep logging: Logira payload prije slanja

INFOBIP ZAHTJEVI:
- Content-Type: application/json
- Authorization: App {API_KEY}
- Payload: {"from": "...", "to": "...", "content": {"text": "..."}}
- 'to' MORA biti telefonski broj (385...), ne UUID!
"""

import asyncio
import json
import logging
import random
import re
import unicodedata
from typing import Optional, Dict, Any, Tuple, List
from dataclasses import dataclass

import httpx

from config import get_settings
from services.errors import ConversationError, GatewayError, ErrorCode, HTTP_STATUS_TO_ERROR_CODE
from services.utils.pattern_registry import UUID_PATTERN
from services.retry_utils import calculate_backoff
from services.tracing import get_tracer, trace_span
logger = logging.getLogger(__name__)
_tracer = get_tracer("whatsapp_service")
def _get_settings():
    return get_settings()

# Phone number pattern (international format)
# Accepts: +385..., 385..., 00385..., etc.
PHONE_PATTERN = re.compile(
    r'^(\+)?[0-9]{10,15}$'
)


@dataclass
class SendResult:
    """Result of WhatsApp send operation."""
    success: bool
    message_id: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    status_code: Optional[int] = None
    retry_after: Optional[int] = None  # For rate limiting


class WhatsAppService:
    """
    Production-grade WhatsApp integration service.

    Features:
    - Phone number validation (prevents UUID trap)
    - UTF-8 safe encoding
    - Type guards (string enforcement)
    - Exponential backoff with jitter
    - Deep logging for debugging
    - Message chunking for long messages
    """

    # Infobip limits
    MAX_MESSAGE_LENGTH = 4096  # WhatsApp text limit
    MAX_RETRIES = 3
    BASE_DELAY = 1.0   # Base delay for exponential backoff
    MAX_BACKOFF = 60.0  # Hard cap: prevents CPU death spiral on 0.5 CPU
    MAX_JITTER = 1.0    # Random jitter (0-1s) — decorrelates fleet retries

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        sender_number: Optional[str] = None
    ):
        """
        Initialize WhatsApp service.

        Args:
            api_key: Infobip API key (defaults to settings)
            base_url: Infobip base URL (defaults to settings)
            sender_number: Sender phone number (defaults to settings)
        """
        self.api_key = api_key or _get_settings().INFOBIP_API_KEY
        self.base_url = base_url or _get_settings().INFOBIP_BASE_URL
        self.sender_number = sender_number or _get_settings().INFOBIP_SENDER_NUMBER

        # Validate configuration
        self._validate_config()

        # Reusable HTTP client — avoids connection setup per message.
        # On 0.5 CPU, TLS handshake per send wastes ~50ms of the 100ms CFS window.
        self._client = httpx.AsyncClient(timeout=30.0)

        # Stats
        self._messages_sent = 0
        self._messages_failed = 0
        self._total_retries = 0

        logger.info(
            f"WhatsAppService initialized: "
            f"base_url={self.base_url}, "
            f"sender={self.sender_number[-4:] if self.sender_number else 'N/A'}"
        )

    def _validate_config(self) -> None:
        """Validate configuration at startup."""
        if not self.api_key:
            logger.warning(
                "INFOBIP_API_KEY not configured! "
                "WhatsApp sending will be disabled."
            )

        if not self.sender_number:
            logger.warning(
                "INFOBIP_SENDER_NUMBER not configured! "
                "WhatsApp sending will fail."
            )

        if self.api_key and len(self.api_key) < 10:
            logger.warning(
                "INFOBIP_API_KEY appears to be invalid (too short)."
            )

    # ---
    # VALIDATION METHODS (CRITICAL FOR 400 ERROR PREVENTION)
    # ---

    def validate_phone_number(self, number: str) -> Tuple[bool, str, Optional[str]]:
        """
        Validate phone number and detect UUID trap.

        KRITIČNO: Ovo sprječava slanje poruke na UUID umjesto telefonskog broja!

        Args:
            number: Phone number to validate

        Returns:
            Tuple of (is_valid, normalized_number, error_message)

        Examples:
            "+385991234567" -> (True, "385991234567", None)
            "00385991234567" -> (True, "385991234567", None)
            "550e8400-e29b-..." -> (False, None, "UUID detected in phone field!")
        """
        if not number:
            return (False, "", "Phone number is empty")

        # Clean whitespace
        number = number.strip()

        # CRITICAL: Detect UUID trap
        if UUID_PATTERN.match(number):
            logger.error(
                f"UUID TRAP DETECTED! "
                f"Field 'to' contains UUID instead of phone number: {number[:20]}..."
            )
            return (
                False,
                "",
                f"UUID detected in phone field! Expected phone number, got: {number[:20]}..."
            )

        # Remove common prefixes and normalize
        normalized = number

        # Remove + prefix (Infobip prefers without it)
        if normalized.startswith('+'):
            normalized = normalized[1:]

        # Remove 00 prefix
        if normalized.startswith('00'):
            normalized = normalized[2:]

        # Remove leading 0 for Croatian numbers (mobile: 10 digits, landline: 9 digits)
        if normalized.startswith('0') and 9 <= len(normalized) <= 10:
            # Convert 0991234567 → 385991234567 or 01xxxxxxx → 3851xxxxxxx
            normalized = '385' + normalized[1:]

        # Validate format
        if not PHONE_PATTERN.match(normalized):
            return (
                False,
                "",
                f"Invalid phone number format: {number}"
            )

        # Additional Croatian validation
        if normalized.startswith('385'):
            if len(normalized) < 11 or len(normalized) > 12:
                return (
                    False,
                    "",
                    f"Croatian phone number has invalid length: {number}"
                )

        logger.debug(f"Phone validated: {number} -> {normalized}")
        return (True, normalized, None)

    def ensure_string(self, value: Any) -> Tuple[str, bool]:
        """
        Ensure value is a string, not an object.

        KRITIČNO: AI može vratiti objekt umjesto stringa:
        {"odgovor": "Vozilo je Audi"} -> "Vozilo je Audi"

        Args:
            value: Value to convert to string

        Returns:
            Tuple of (string_value, was_converted)

        Examples:
            "Hello" -> ("Hello", False)
            {"text": "Hello"} -> ('{"text": "Hello"}' OR extracted text, True)
            123 -> ("123", True)
        """
        if isinstance(value, str):
            return (value, False)

        if value is None:
            logger.warning("ensure_string received None, returning empty string")
            return ("", True)

        # Handle dict - try to extract text intelligently
        if isinstance(value, dict):
            logger.warning(
                f"TYPE GUARD: Received dict instead of string: {list(value.keys())}"
            )

            # Try common text keys
            for key in ['text', 'message', 'content', 'odgovor', 'response', 'answer']:
                if key in value and isinstance(value[key], str):
                    logger.info(f"Extracted text from dict key '{key}'")
                    return (value[key], True)

            # Fallback — truncate to avoid leaking sensitive data
            logger.warning("TYPE GUARD: Unknown dict structure, returning generic error")
            return ("Došlo je do greške pri generiranju odgovora. Pokušajte ponovo.", True)

        # Handle list
        if isinstance(value, list):
            logger.warning(
                f"TYPE GUARD: Received list instead of string: {len(value)} items"
            )

            # Try to join string items
            if all(isinstance(item, str) for item in value):
                return ("\n".join(value), True)

            try:
                return (json.dumps(value, ensure_ascii=False), True)
            except Exception as e:
                logger.warning(f"JSON serialization failed for list, using str(): {e}")
                return (str(value), True)

        # Handle other types
        return (str(value), True)

    def ensure_utf8_safe(self, text: str) -> str:
        """
        Ensure text is UTF-8 safe for WhatsApp.

        KRITIČNO: WhatsApp može odbiti poruke s nevalidnim UTF-8 znakovima.

        This function:
        1. Normalizes Unicode (NFC)
        2. Removes control characters (except newline, tab)
        3. Replaces invalid sequences
        4. Handles emoji properly

        Args:
            text: Text to sanitize

        Returns:
            UTF-8 safe text
        """
        if not text:
            return ""

        # Step 1: Normalize Unicode (combine characters properly)
        text = unicodedata.normalize('NFC', text)

        # Step 2: Remove control characters (except \n, \t, \r)
        cleaned = []
        for char in text:
            # Keep printable characters and common whitespace
            if char in '\n\t\r':
                cleaned.append(char)
            elif unicodedata.category(char) == 'Cc':
                # Control character - skip
                continue
            else:
                cleaned.append(char)

        text = ''.join(cleaned)

        # Step 3: Encode/decode to ensure valid UTF-8
        try:
            text = text.encode('utf-8', errors='replace').decode('utf-8')
        except Exception as e:
            logger.warning(f"UTF-8 encoding issue: {e}")
            # Last resort: strip non-ASCII
            text = text.encode('ascii', errors='ignore').decode('ascii')

        return text

    # ---
    # PAYLOAD BUILDING (INFOBIP SPECIFIC)
    # ---

    def build_payload(
        self,
        to: str,
        text: str,
        from_number: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Build Infobip-compliant payload.

        INFOBIP FORMAT:
        {
            "from": "385...",
            "to": "385...",
            "content": {
                "text": "Message text here"
            }
        }

        NOT the messages[] wrapper - that's for batch sending.

        Args:
            to: Recipient phone number (must be validated)
            text: Message text (must be string)
            from_number: Optional sender number

        Returns:
            Infobip-compliant payload
        """
        payload = {
            "from": from_number or self.sender_number,
            "to": to,
            "content": {
                "text": text
            }
        }

        return payload

    def build_headers(self) -> Dict[str, str]:
        """
        Build Infobip headers.

        KRITIČNO:
        - Authorization mora biti "App {API_KEY}" (ne "Bearer")
        - Content-Type mora biti application/json
        """
        if not self.api_key:
            logger.error("Cannot build headers: API_KEY is empty!")
            return {}

        return {
            "Authorization": f"App {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

    # ---
    # SENDING WITH RETRY (EXPONENTIAL BACKOFF + JITTER)
    # ---

    async def send(
        self,
        to: str,
        text: Any,
        validate: bool = True
    ) -> SendResult:
        """
        Send WhatsApp message with full validation and retry logic.

        Args:
            to: Recipient phone number
            text: Message text (will be converted to string if needed)
            validate: Whether to validate phone number

        Returns:
            SendResult with success/failure info
        """
        with trace_span(_tracer, "whatsapp_service.send", {"to_suffix": to[-4:] if to else "", "validate": validate}) as span:
            # Step 1: Validate phone number
            if validate:
                is_valid, normalized_to, error = self.validate_phone_number(to)
                if not is_valid:
                    logger.error(f"Phone validation failed: {error}")
                    span.set_attribute("result.success", False)
                    span.set_attribute("result.error_code", ErrorCode.PHONE_INVALID)
                    return SendResult(
                        success=False,
                        error_code=ErrorCode.PHONE_INVALID,
                        error_message=error
                    )
                to = normalized_to

            # Step 2: Ensure text is string
            text_str, was_converted = self.ensure_string(text)
            if was_converted:
                logger.warning(f"Text was converted from {type(text).__name__} to string")

            # Step 3: Ensure UTF-8 safe
            text_str = self.ensure_utf8_safe(text_str)

            # Step 4: Check length
            if len(text_str) > self.MAX_MESSAGE_LENGTH:
                err = ConversationError(ErrorCode.MESSAGE_TOO_LONG, f"Message too long ({len(text_str)} chars), truncating to {self.MAX_MESSAGE_LENGTH}")
                logger.warning(str(err))
                text_str = text_str[:self.MAX_MESSAGE_LENGTH - 3] + "..."

            span.set_attribute("message.text_length", len(text_str))

            # Step 5: Build payload
            payload = self.build_payload(to, text_str)
            headers = self.build_headers()

            if not headers:
                span.set_attribute("result.success", False)
                span.set_attribute("result.error_code", ErrorCode.PARAMETER_MISSING)
                return SendResult(
                    success=False,
                    error_code=ErrorCode.PARAMETER_MISSING,
                    error_message="API key not configured"
                )

            # DEEP LOGGING: Log payload before sending
            logger.info(
                f"SENDING TO INFOBIP: "
                f"to={to[-4:]}..., "
                f"text_length={len(text_str)}, "
                f"payload={json.dumps(payload, ensure_ascii=False)[:200]}"
            )

            # Step 6: Send with retry
            url = f"https://{self.base_url}/whatsapp/1/message/text"

            result = await self._send_with_retry(url, payload, headers)
            span.set_attribute("result.success", result.success)
            if result.message_id:
                span.set_attribute("result.message_id", result.message_id)
            return result

    async def _send_with_retry(
        self,
        url: str,
        payload: Dict[str, Any],
        headers: Dict[str, str]
    ) -> SendResult:
        """
        Send with exponential backoff and jitter.

        Retry on:
        - 429 Rate Limit
        - 5xx Server Errors
        - Network timeouts
        """
        last_error = None

        for attempt in range(self.MAX_RETRIES):
            try:
                response = await self._client.post(
                    url,
                    json=payload,
                    headers=headers
                )

                # Success
                if response.status_code in (200, 201):
                    self._messages_sent += 1

                    try:
                        response_data = response.json()
                        # Infobip returns messageId at root level (single message)
                        # or in messages[] array (batch). Handle both.
                        message_id = (
                            response_data.get("messageId")
                            or response_data.get("messages", [{}])[0].get("messageId")
                        )
                    except Exception:
                        message_id = None

                    logger.info(
                        f"Message sent successfully: "
                        f"to={payload['to'][-4:]}..., "
                        f"message_id={message_id}"
                    )

                    return SendResult(
                        success=True,
                        message_id=message_id,
                        status_code=response.status_code
                    )

                # Rate limit - MUST retry with backoff
                if response.status_code == 429:
                    try:
                        retry_after = int(response.headers.get("Retry-After", 0))
                    except (ValueError, TypeError):
                        retry_after = 0

                    if attempt < self.MAX_RETRIES - 1:
                        delay = self._calculate_backoff(attempt, retry_after)
                        logger.warning(
                            f"Rate limited (429). Retry {attempt + 1}/{self.MAX_RETRIES} "
                            f"after {delay:.2f}s"
                        )
                        self._total_retries += 1
                        await asyncio.sleep(delay)
                        continue
                    else:
                        return SendResult(
                            success=False,
                            error_code=ErrorCode.RATE_LIMITED,
                            error_message="Rate limit exceeded after max retries",
                            status_code=429,
                            retry_after=retry_after
                        )

                # Server error - retry
                if response.status_code >= 500:
                    if attempt < self.MAX_RETRIES - 1:
                        delay = self._calculate_backoff(attempt)
                        logger.warning(
                            f"Server error ({response.status_code}). "
                            f"Retry {attempt + 1}/{self.MAX_RETRIES} after {delay:.2f}s"
                        )
                        self._total_retries += 1
                        await asyncio.sleep(delay)
                        continue

                # Client error - don't retry (except 429)
                self._messages_failed += 1

                try:
                    error_data = response.json()
                    error_message = error_data.get("requestError", {}).get(
                        "serviceException", {}
                    ).get("text", str(error_data))
                except (ValueError, KeyError, AttributeError):
                    error_message = response.text

                logger.error(
                    f"Send failed: status={response.status_code}, "
                    f"error={error_message[:200]}"
                )

                http_error_code = HTTP_STATUS_TO_ERROR_CODE.get(response.status_code, ErrorCode.SERVER_ERROR)
                return SendResult(
                    success=False,
                    error_code=http_error_code,
                    error_message=error_message,
                    status_code=response.status_code
                )

            except httpx.TimeoutException as e:
                last_error = str(e)
                err = GatewayError(ErrorCode.TIMEOUT, f"WhatsApp API timeout: {e}", status_code=408)
                logger.error(str(err))
                if attempt < self.MAX_RETRIES - 1:
                    delay = self._calculate_backoff(attempt)
                    logger.warning(
                        f"Timeout. Retry {attempt + 1}/{self.MAX_RETRIES} "
                        f"after {delay:.2f}s"
                    )
                    self._total_retries += 1
                    await asyncio.sleep(delay)
                    continue

            except Exception as e:
                last_error = str(e)
                err = GatewayError(ErrorCode.RETRY_EXHAUSTED, f"WhatsApp send error: {e}")
                logger.error(str(err))

                if attempt < self.MAX_RETRIES - 1:
                    delay = self._calculate_backoff(attempt)
                    logger.warning(
                        f"Retrying after error. Retry {attempt + 1}/{self.MAX_RETRIES} "
                        f"after {delay:.2f}s"
                    )
                    self._total_retries += 1
                    await asyncio.sleep(delay)
                    continue

        # All retries exhausted
        self._messages_failed += 1
        err = GatewayError(ErrorCode.RETRY_EXHAUSTED, f"All {self.MAX_RETRIES} retries exhausted for WhatsApp send: {last_error}")
        logger.error(str(err))

        return SendResult(
            success=False,
            error_code=ErrorCode.RETRY_EXHAUSTED,
            error_message=f"Failed after {self.MAX_RETRIES} attempts: {last_error}"
        )

    def _calculate_backoff(
        self,
        attempt: int,
        min_delay: int = 0
    ) -> float:
        """
        Calculate exponential backoff with jitter and hard cap.

        Formula: min(MAX_BACKOFF, max(min_delay, 2^attempt * base)) + random(0, jitter)

        The cap at 60s prevents a CPU death spiral on 0.5 CPU pods:
        without it, linear/uncapped retries during an Infobip outage
        tie up all semaphore slots with sleeping coroutines, starving
        new messages.
        """
        raw = calculate_backoff(attempt, base_delay=self.BASE_DELAY, max_delay=self.MAX_BACKOFF, jitter=self.MAX_JITTER)
        # Honour caller-supplied min_delay (e.g. Retry-After header)
        if min_delay > 0:
            base_without_jitter = min(self.MAX_BACKOFF, max(min_delay, self.BASE_DELAY * (2 ** attempt)))
            return base_without_jitter + random.uniform(0, self.MAX_JITTER)
        return raw

    # ---
    # BATCH SENDING (FOR FUTURE USE)
    # ---

    async def send_batch(
        self,
        messages: List[Tuple[str, str]],
        concurrency: int = 5
    ) -> List[SendResult]:
        """
        Send multiple messages concurrently with rate limiting.

        Args:
            messages: List of (to, text) tuples
            concurrency: Max concurrent sends (default 5)

        Returns:
            List of SendResult (same order as input)
        """
        semaphore = asyncio.Semaphore(concurrency)

        async def _send_one(to: str, text: str) -> SendResult:
            async with semaphore:
                result = await self.send(to, text)
                await asyncio.sleep(0.1)  # Rate limit spacing
                return result

        return list(await asyncio.gather(
            *(_send_one(to, text) for to, text in messages)
        ))

    # ---
    # STATS & HEALTH
    # ---

    async def close(self) -> None:
        """Close the reusable HTTP client to release connections."""
        if self._client:
            await self._client.aclose()
            self._client = None

    def get_stats(self) -> Dict[str, Any]:
        """Get service statistics."""
        return {
            "messages_sent": self._messages_sent,
            "messages_failed": self._messages_failed,
            "total_retries": self._total_retries,
            "success_rate": (
                self._messages_sent / (self._messages_sent + self._messages_failed)
                if (self._messages_sent + self._messages_failed) > 0
                else 0.0
            )
        }

    def health_check(self) -> Dict[str, Any]:
        """Check service health."""
        return {
            "healthy": bool(self.api_key and self.sender_number),
            "api_key_configured": bool(self.api_key),
            "sender_configured": bool(self.sender_number),
            "base_url": self.base_url
        }
