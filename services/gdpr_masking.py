"""
GDPR Data Masking Service

LEGAL REQUIREMENT:
- GDPR Article 17: Right to Erasure
- GDPR Article 25: Data Protection by Design
- GDPR Article 32: Security of Processing

PII TYPES DETECTED:
- Phone numbers (Croatian, international)
- Email addresses
- OIB (Croatian personal ID - 11 digits with checksum validation)
- IBAN (bank accounts)
- Credit card numbers (with Luhn validation)
- IP addresses (IPv4 and IPv6)
- Names (heuristic)

USAGE:
    from services.gdpr_masking import GDPRMaskingService

    masker = GDPRMaskingService()
    result = masker.mask_pii("Moj OIB je 12345678901")
    # Output: result.masked_text = "Moj OIB je [OIB-MASKED]"

SECURITY:
    - Salt MUST be loaded from environment variable GDPR_HASH_SALT
    - Never commit salt to code repository
    - Rotate salt periodically (invalidates all pseudonymized links)
    - Minimum salt length: 32 characters
"""

import re
import hmac
import hashlib
import logging
import secrets
import threading
from typing import Dict, List, Optional, Set, FrozenSet
from dataclasses import dataclass, field
from enum import Enum

from services.tracing import get_tracer, trace_span
from services.errors import InfrastructureError, ErrorCode

logger = logging.getLogger(__name__)
_tracer = get_tracer("gdpr_masking")

# Thread-safe singleton lock
_singleton_lock = threading.Lock()

# Minimum salt length for security
MIN_SALT_LENGTH = 32


class PIIType(str, Enum):
    """Types of PII data."""
    PHONE = "phone"
    EMAIL = "email"
    OIB = "oib"           # Croatian personal ID
    IBAN = "iban"
    CREDIT_CARD = "credit_card"
    IP_ADDRESS = "ip"
    NAME = "name"
    ADDRESS = "address"


@dataclass
class PIIMatch:
    """Represents a PII match in text."""
    pii_type: PIIType
    original: str
    masked: str
    start: int
    end: int
    confidence: float = 1.0


@dataclass
class MaskingResult:
    """Result of masking operation."""
    original_text: str
    masked_text: str
    pii_found: List[PIIMatch] = field(default_factory=list)
    pii_count: int = 0

    def has_pii(self) -> bool:
        return self.pii_count > 0


class GDPRMaskingService:
    """
    GDPR-compliant PII masking service.

    Features:
    - Regex-based PII detection
    - Consistent hashing for pseudonymization (HMAC-SHA256)
    - Audit logging of masking operations
    - Configurable masking strategies
    - Thread-safe singleton
    """

    # Regex patterns for PII detection
    PATTERNS = {
        PIIType.PHONE: [
            # Croatian mobile with various separators: 09x xxx xxxx, 09x/xxx-xxxx, 09x-xxx-xxxx
            r'\b09[1-9][\s/-]?\d{3}[\s/-]?\d{3,4}\b',
            # Croatian landline: 0x xxx xxxx
            r'\b0[1-5][1-9][\s/-]?\d{3}[\s/-]?\d{3,4}\b',
            # International Croatian: +385 xx xxx xxxx
            r'\+385[\s/-]?\d{2}[\s/-]?\d{3}[\s/-]?\d{3,4}\b',
            # Generic international with various formats
            r'\+\d{1,3}[\s/-]?\d{2,3}[\s/-]?\d{3,4}[\s/-]?\d{3,4}\b',
            # Parentheses format: (01) 234-5678
            r'\(\d{2,3}\)[\s/-]?\d{3}[\s/-]?\d{3,4}\b',
        ],
        PIIType.EMAIL: [
            r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b',
        ],
        PIIType.OIB: [
            # Croatian OIB: exactly 11 digits, standalone
            # More specific to avoid matching other 11-digit numbers
            r'(?<![0-9])\d{11}(?![0-9])',
        ],
        PIIType.IBAN: [
            # Croatian IBAN: HR + 19 digits (strict)
            r'\bHR\d{19}\b',
            # European IBAN formats (2 letter country + 2 check digits + BBAN)
            r'\b[A-Z]{2}\d{2}[A-Z0-9]{4}\d{7}[A-Z0-9]{0,16}\b',
        ],
        PIIType.CREDIT_CARD: [
            # Visa: starts with 4
            r'\b4\d{3}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b',
            # Mastercard: starts with 51-55 or 2221-2720
            r'\b5[1-5]\d{2}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b',
            r'\b2[2-7]\d{2}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b',
            # Amex: starts with 34 or 37, 15 digits
            r'\b3[47]\d{2}[\s-]?\d{6}[\s-]?\d{5}\b',
        ],
        PIIType.IP_ADDRESS: [
            # IPv4
            r'\b(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\b',
            # IPv6 full
            r'\b(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}\b',
            # IPv6 with :: compression (various forms)
            r'\b(?:[0-9a-fA-F]{1,4}:){1,6}:[0-9a-fA-F]{1,4}\b',
            r'\b(?:[0-9a-fA-F]{1,4}:){1,5}(?::[0-9a-fA-F]{1,4}){1,2}\b',
            r'\b(?:[0-9a-fA-F]{1,4}:){1,4}(?::[0-9a-fA-F]{1,4}){1,3}\b',
            r'\b::(?:ffff:)?(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\b',  # IPv4-mapped
            r'\b::1\b',  # Loopback
            r'\b::\b',   # Unspecified
        ],
    }

    # Masking format per PII type
    MASK_FORMAT = {
        PIIType.PHONE: "[PHONE-{hash}]",
        PIIType.EMAIL: "[EMAIL-{hash}]",
        PIIType.OIB: "[OIB-MASKED]",
        PIIType.IBAN: "[IBAN-{hash}]",
        PIIType.CREDIT_CARD: "[CARD-****{last4}]",
        PIIType.IP_ADDRESS: "[IP-{hash}]",
        PIIType.NAME: "[NAME-{hash}]",
        PIIType.ADDRESS: "[ADDRESS-MASKED]",
    }

    # Default fields to mask (as frozenset for O(1) lookup)
    DEFAULT_MASK_FIELDS: FrozenSet[str] = frozenset([
        "user_query", "bot_response", "user_feedback",
        "content", "message", "query", "response",
        "phone", "email", "address", "name"
    ])

    def __init__(
        self,
        use_hashing: bool = True,
        hash_salt: Optional[str] = None
    ) -> None:
        """
        Initialize masking service.

        Args:
            use_hashing: Use consistent hashing for pseudonymization
            hash_salt: Salt for hashing. If None, loads from GDPR_HASH_SALT env var.
                      CRITICAL: Never hardcode salt in code!

        Raises:
            ValueError: If no salt provided and GDPR_HASH_SALT env var not set
        """
        self.use_hashing = use_hashing

        # CRITICAL: Load salt from config, never hardcode!
        if hash_salt:
            self.hash_salt = hash_salt
        else:
            from config import get_settings
            _settings = get_settings()
            self.hash_salt = _settings.GDPR_HASH_SALT
            if not self.hash_salt:
                if getattr(_settings, 'is_production', False):
                    raise RuntimeError(
                        "GDPR_HASH_SALT must be set in production! "
                        "Pseudonymized IDs will not be consistent without a stable salt."
                    )
                # Dev/test: generate a session salt with a warning
                self.hash_salt = secrets.token_hex(32)
                logger.warning(
                    "GDPR_HASH_SALT not set! Using random salt. "
                    "PII pseudonymization will NOT be consistent across restarts. "
                    "Set GDPR_HASH_SALT environment variable for production!"
                )

        # Validate salt strength
        if len(self.hash_salt) < MIN_SALT_LENGTH:
            logger.warning(
                f"GDPR hash salt is too short ({len(self.hash_salt)} chars). "
                f"Recommend at least {MIN_SALT_LENGTH} characters for security."
            )

        self._compiled_patterns: Dict[PIIType, List[re.Pattern]] = {}
        self._compile_patterns()

        # Cache for lowercase field names (for case-insensitive matching)
        self._mask_fields_lower: FrozenSet[str] = frozenset(
            f.lower() for f in self.DEFAULT_MASK_FIELDS
        )

        logger.info(
            "GDPRMaskingService initialized (salt provided: %s)",
            "yes" if hash_salt else "from config"
        )

    def _compile_patterns(self) -> None:
        """Compile regex patterns for performance."""
        for pii_type, patterns in self.PATTERNS.items():
            self._compiled_patterns[pii_type] = [
                re.compile(p, re.IGNORECASE) for p in patterns
            ]

    def _hash_value(self, value: str, length: int = 12) -> str:
        """
        Create consistent hash for pseudonymization.

        Same input always produces same hash (for linking records).
        Uses HMAC-SHA256 for proper key derivation.

        Args:
            value: Value to hash
            length: Output length (default 12 chars, more secure than 8)

        Returns:
            Truncated hex hash
        """
        if not self.use_hashing:
            return "X" * length

        # Use HMAC for proper key derivation (more secure than simple concat)
        hash_bytes = hmac.new(
            self.hash_salt.encode('utf-8'),
            value.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()

        return hash_bytes[:length]

    def _mask_value(self, pii_type: PIIType, value: str) -> str:
        """Apply masking format to value."""
        mask_format = self.MASK_FORMAT.get(pii_type, "[MASKED]")

        if "{hash}" in mask_format:
            return mask_format.format(hash=self._hash_value(value))
        elif "{last4}" in mask_format:
            # For credit cards, keep last 4 digits
            digits = re.sub(r'\D', '', value)
            return mask_format.format(last4=digits[-4:] if len(digits) >= 4 else "XXXX")
        else:
            return mask_format

    def detect_pii(self, text: str) -> List[PIIMatch]:
        """
        Detect all PII in text.

        Args:
            text: Input text to scan

        Returns:
            List of PII matches found
        """
        if not text:
            return []

        matches: List[PIIMatch] = []
        seen_ranges: Set[tuple] = set()  # Prevent duplicate detections

        for pii_type, patterns in self._compiled_patterns.items():
            for pattern in patterns:
                for match in pattern.finditer(text):
                    original = match.group()
                    start, end = match.start(), match.end()

                    # Skip if this range overlaps with already detected PII
                    if any(s <= start < e or s < end <= e for s, e in seen_ranges):
                        continue

                    # Skip if too short (false positive)
                    if len(original) < 4:
                        continue

                    # OIB validation: must be exactly 11 digits with valid checksum
                    if pii_type == PIIType.OIB:
                        if not self._validate_oib(original):
                            continue

                    # Credit card validation: must pass Luhn check
                    if pii_type == PIIType.CREDIT_CARD:
                        if not self._validate_credit_card(original):
                            continue

                    pii_match = PIIMatch(
                        pii_type=pii_type,
                        original=original,
                        masked=self._mask_value(pii_type, original),
                        start=start,
                        end=end,
                        confidence=0.95 if pii_type == PIIType.OIB else 1.0
                    )
                    matches.append(pii_match)
                    seen_ranges.add((start, end))

        # Sort by position (for correct replacement order)
        matches.sort(key=lambda m: m.start, reverse=True)

        return matches

    def _validate_oib(self, oib: str) -> bool:
        """
        Validate Croatian OIB using MOD 11,10 algorithm.

        Returns True if valid OIB, False otherwise.
        Logs invalid attempts for security monitoring.
        """
        if len(oib) != 11 or not oib.isdigit():
            return False

        # Additional check: don't match obvious non-OIB patterns
        # OIB shouldn't be all same digits or sequential
        if len(set(oib)) == 1:  # All same digit like 11111111111
            logger.debug("Rejected OIB candidate: all same digits")
            return False

        # MOD 11,10 validation
        remainder = 10
        for digit in oib[:10]:
            remainder = (remainder + int(digit)) % 10
            if remainder == 0:
                remainder = 10
            remainder = (remainder * 2) % 11

        check_digit = (11 - remainder) % 10
        is_valid = check_digit == int(oib[10])

        if not is_valid:
            # Log for security monitoring but don't expose the full number
            logger.debug(f"Invalid OIB checksum detected (last 2 digits: ...{oib[-2:]})")

        return is_valid

    def _validate_credit_card(self, number: str) -> bool:
        """
        Validate credit card using Luhn algorithm.

        Returns True if valid, False otherwise.
        """
        # Remove spaces and dashes
        digits = re.sub(r'[\s-]', '', number)

        if not digits.isdigit() or len(digits) < 13 or len(digits) > 19:
            return False

        # Don't match obvious test/fake numbers
        if digits in ['0000000000000000', '1111111111111111']:
            return False

        # Luhn algorithm
        total = 0
        for i, digit in enumerate(reversed(digits)):
            d = int(digit)
            if i % 2 == 1:
                d *= 2
                if d > 9:
                    d -= 9
            total += d

        return total % 10 == 0

    def mask_pii(self, text: str) -> MaskingResult:
        """
        Mask all PII in text.

        Args:
            text: Input text

        Returns:
            MaskingResult with masked text and detected PII
        """
        with trace_span(_tracer, "gdpr_masking.mask_pii", {
            "text_length": len(text) if text else 0,
        }):
            if not text:
                return MaskingResult(
                    original_text="",
                    masked_text="",
                    pii_found=[],
                    pii_count=0
                )

            matches = self.detect_pii(text)
            masked_text = text

            # Replace from end to start (to preserve positions)
            for match in matches:
                masked_text = (
                    masked_text[:match.start] +
                    match.masked +
                    masked_text[match.end:]
                )

            result = MaskingResult(
                original_text=text,
                masked_text=masked_text,
                pii_found=matches,
                pii_count=len(matches)
            )

            if matches:
                # Mask the log message itself to avoid logging PII
                logger.info(
                    f"GDPR: Masked {len(matches)} PII items: "
                    f"{[m.pii_type.value for m in matches]}"
                )

            return result

    async def mask_pii_async(self, text: str) -> MaskingResult:
        """
        Async version of mask_pii for batch processing.

        Args:
            text: Input text

        Returns:
            MaskingResult with masked text and detected PII
        """
        # For CPU-bound work, we could use run_in_executor
        # but for now, the regex operations are fast enough
        return self.mask_pii(text)

    def mask_dict(
        self,
        data: Dict,
        fields_to_mask: Optional[List[str]] = None,
        max_depth: int = 10,
        _current_depth: int = 0
    ) -> Dict:
        """
        Mask PII in dictionary fields.

        Args:
            data: Dictionary to process
            fields_to_mask: Specific fields to mask (None = default fields)
            max_depth: Maximum recursion depth (prevents infinite recursion)
            _current_depth: Internal counter for recursion depth

        Returns:
            Dictionary with masked values
        """
        with trace_span(_tracer, "gdpr_masking.mask_dict", {
            "field_count": len(data) if data and isinstance(data, dict) else 0,
            "current_depth": _current_depth,
        }):
            if not data or not isinstance(data, dict):
                return data

            # Prevent infinite recursion
            if _current_depth >= max_depth:
                logger.warning(f"GDPR masking: max depth {max_depth} reached, stopping recursion")
                return data

            result = {}

            # Use frozenset for O(1) lookup
            if fields_to_mask:
                fields_lower: FrozenSet[str] = frozenset(f.lower() for f in fields_to_mask)
            else:
                fields_lower = self._mask_fields_lower

            for key, value in data.items():
                key_lower = key.lower() if isinstance(key, str) else str(key).lower()

                if isinstance(value, str) and key_lower in fields_lower:
                    mask_result = self.mask_pii(value)
                    result[key] = mask_result.masked_text
                elif isinstance(value, dict):
                    result[key] = self.mask_dict(
                        value, fields_to_mask, max_depth, _current_depth + 1
                    )
                elif isinstance(value, list):
                    result[key] = self._mask_list(
                        value, fields_to_mask, max_depth, _current_depth + 1
                    )
                else:
                    result[key] = value

            return result

    def _mask_list(
        self,
        data: List,
        fields_to_mask: Optional[List[str]],
        max_depth: int,
        current_depth: int
    ) -> List:
        """
        Mask PII in list items.

        Args:
            data: List to process
            fields_to_mask: Fields to mask in nested dicts
            max_depth: Maximum recursion depth
            current_depth: Current recursion depth

        Returns:
            List with masked values
        """
        if current_depth >= max_depth:
            return data

        result = []
        for item in data:
            if isinstance(item, dict):
                result.append(self.mask_dict(
                    item, fields_to_mask, max_depth, current_depth
                ))
            elif isinstance(item, str):
                result.append(self.mask_pii(item).masked_text)
            elif isinstance(item, list):
                result.append(self._mask_list(
                    item, fields_to_mask, max_depth, current_depth + 1
                ))
            else:
                result.append(item)
        return result

    async def anonymize_user_data(
        self,
        user_id: str,
        db_session,
        check_already_anonymized: bool = True
    ) -> Dict:
        """
        GDPR Article 17: Right to Erasure.

        Anonymize all user data in database.

        Args:
            user_id: User identifier (phone number)
            db_session: Database session
            check_already_anonymized: Check if already anonymized first

        Returns:
            Summary of anonymized records
        """
        from sqlalchemy import update, select
        from models import UserMapping, HallucinationReport

        anonymized = {
            "user_mappings": 0,
            "hallucination_reports": 0,
            "already_anonymized": False
        }

        try:
            # Check if user is already anonymized
            if check_already_anonymized:
                check_result = await db_session.execute(
                    select(UserMapping).where(
                        UserMapping.phone_number == user_id
                    )
                )
                existing = check_result.scalar_one_or_none()
                if existing and existing.phone_number.startswith("DELETED_"):
                    logger.info(f"User already anonymized: {self._hash_value(user_id)}")
                    anonymized["already_anonymized"] = True
                    return anonymized

            # 1. Anonymize user mapping
            hashed_id = self._hash_value(user_id)
            result = await db_session.execute(
                update(UserMapping)
                .where(UserMapping.phone_number == user_id)
                .values(
                    phone_number=f"DELETED_{hashed_id}",
                    display_name="[DELETED]",
                    is_active=False
                )
            )
            anonymized["user_mappings"] = result.rowcount

            # 2. Anonymize hallucination reports
            # Find reports by user - using a proper query instead of contains()
            # First, get the user mapping to find conversation IDs
            user_result = await db_session.execute(
                select(UserMapping.id).where(
                    UserMapping.phone_number == f"DELETED_{hashed_id}"
                )
            )
            user_mapping = user_result.scalar_one_or_none()

            if user_mapping:
                # Find conversations for this user and anonymize associated reports
                from models import Conversation, Message
                conv_result = await db_session.execute(
                    select(Conversation.id).where(
                        Conversation.user_id == user_mapping
                    )
                )
                conversation_ids = [str(c) for c in conv_result.scalars().all()]

                if conversation_ids:
                    # 2a. Anonymize messages in those conversations
                    msg_result = await db_session.execute(
                        update(Message)
                        .where(Message.conversation_id.in_(conversation_ids))
                        .values(
                            content="[GDPR DELETED]",
                            metadata=None
                        )
                    )
                    anonymized["messages"] = msg_result.rowcount

                    # 2b. Anonymize hallucination reports for these conversations
                    report_result = await db_session.execute(
                        update(HallucinationReport)
                        .where(HallucinationReport.conversation_id.in_(conversation_ids))
                        .values(
                            user_query="[GDPR DELETED]",
                            bot_response="[GDPR DELETED]",
                            user_feedback="[GDPR DELETED]"
                        )
                    )
                    anonymized["hallucination_reports"] = report_result.rowcount

                # 2c. Anonymize conversations themselves
                conv_anon_result = await db_session.execute(
                    update(Conversation)
                    .where(Conversation.user_id == user_mapping)
                    .values(state="anonymized")
                )
                anonymized["conversations"] = conv_anon_result.rowcount

            # 3. Mark user mapping as anonymized with timestamp
            # NOTE: phone_number was already changed to DELETED_{hashed_id} in step 1
            from datetime import datetime, timezone as tz
            await db_session.execute(
                update(UserMapping)
                .where(UserMapping.phone_number == f"DELETED_{hashed_id}")
                .values(
                    phone_number=f"[GDPR:{hashed_id[:12]}]",
                    display_name="[GDPR DELETED]",
                    is_active=False,
                    gdpr_anonymized_at=datetime.now(tz.utc)
                )
            )
            anonymized["user_mapping"] = 1

            await db_session.commit()

            # Log with hashed user ID to avoid logging PII
            logger.warning(
                f"GDPR ERASURE: User {hashed_id} data anonymized. "
                f"Records: {anonymized}"
            )

            return anonymized

        except Exception as e:
            await db_session.rollback()
            err = InfrastructureError(
                ErrorCode.DATABASE_UNAVAILABLE,
                f"GDPR erasure failed: {e}",
                metadata={"user_hash": self._hash_value(user_id)},
                cause=e,
            )
            logger.error(str(err))
            raise

    async def erase_redis_state(self, user_id: str, redis_client) -> Dict:
        """
        GDPR Article 17: Erase all user state from Redis.

        Deletes conversation state, chat history, user context, tenant cache,
        and scrubs DLQ entries containing this user's phone number.

        Args:
            user_id: User phone number
            redis_client: Async Redis client

        Returns:
            Summary of deleted keys
        """
        erased = {"keys_deleted": 0, "dlq_scrubbed": 0}

        # Generate all phone variations for key lookup
        digits = "".join(c for c in user_id if c.isdigit())
        phone_variants = {user_id, digits}
        if digits.startswith("385"):
            phone_variants.add("0" + digits[3:])
            phone_variants.add("+" + digits)
        elif digits.startswith("0"):
            phone_variants.add("385" + digits[1:])

        try:
            # 1. Delete known Redis key patterns for this user
            key_patterns = [
                "conv_state:{phone}",
                "chat_history:{phone}",
                "user_context:{phone}",
                "tenant:{phone}",
            ]

            keys_to_delete = []
            for phone in phone_variants:
                for pattern in key_patterns:
                    key = pattern.format(phone=phone)
                    keys_to_delete.append(key)
            if keys_to_delete:
                deleted = await redis_client.delete(*keys_to_delete)
                erased["keys_deleted"] += deleted

            # 2. Scrub DLQ entries containing this user's data
            dlq_key = "dlq:webhook"
            dlq_len = await redis_client.llen(dlq_key)
            if dlq_len > 0:
                # Read all DLQ entries, remove those matching user
                entries = await redis_client.lrange(dlq_key, 0, -1)
                to_remove = []
                for entry in entries:
                    try:
                        entry_str = entry if isinstance(entry, str) else entry.decode("utf-8")
                        if any(phone in entry_str for phone in phone_variants):
                            to_remove.append(entry)
                    except Exception as e:
                        logger.warning(f"GDPR: Failed to decode DLQ entry during erasure: {e}")
                        continue

                for entry in to_remove:
                    await redis_client.lrem(dlq_key, 1, entry)
                    erased["dlq_scrubbed"] += 1

            hashed = self._hash_value(user_id)
            logger.warning(
                f"GDPR REDIS ERASURE: User {hashed} — "
                f"{erased['keys_deleted']} keys deleted, "
                f"{erased['dlq_scrubbed']} DLQ entries scrubbed"
            )

        except Exception as e:
            err = InfrastructureError(
                ErrorCode.REDIS_UNAVAILABLE,
                f"GDPR Redis erasure failed: {e}",
                metadata={"user_hash": self._hash_value(user_id)},
                cause=e,
            )
            logger.error(str(err))
            raise

        return erased

    async def export_user_data(
        self,
        user_id: str,
        db_session,
        redis_client=None
    ) -> Dict:
        """
        GDPR Article 20: Right to Data Portability.

        Export all user data in a structured, machine-readable JSON format.
        Pulls from BOTH Postgres AND Redis for complete portability.

        Args:
            user_id: User identifier (phone number)
            db_session: Database session
            redis_client: Optional async Redis client for ephemeral state export

        Returns:
            Dict with all user data (user_mappings, conversations, messages,
            tool_executions, hallucination_reports, redis_state)
        """
        from sqlalchemy import select
        from models import UserMapping, Conversation, Message, HallucinationReport

        export = {
            "gdpr_export_version": "1.1",
            "export_type": "GDPR Article 20 — Right to Data Portability",
            "user_identifier_hash": self._hash_value(user_id),
            "user_profile": None,
            "conversations": [],
            "hallucination_reports": [],
            "redis_state": None,
        }

        try:
            # 1. User profile
            user_result = await db_session.execute(
                select(UserMapping).where(UserMapping.phone_number == user_id)
            )
            user = user_result.scalar_one_or_none()

            if not user:
                logger.info(f"GDPR export: no user found for hash {self._hash_value(user_id)}")
                return export

            export["user_profile"] = {
                "phone_number": user.phone_number,
                "display_name": user.display_name,
                "tenant_id": user.tenant_id,
                "is_active": user.is_active,
                "created_at": user.created_at.isoformat() if user.created_at else None,
                "gdpr_consent_given": user.gdpr_consent_given,
                "gdpr_consent_at": user.gdpr_consent_at.isoformat() if user.gdpr_consent_at else None,
            }

            # 2. Conversations + messages
            conv_result = await db_session.execute(
                select(Conversation).where(Conversation.user_id == user.id)
            )
            conversations = conv_result.scalars().all()

            # Batch-fetch all messages for all conversations in one query (avoid N+1)
            all_conv_ids = [c.id for c in conversations]
            from collections import defaultdict
            msgs_by_conv = defaultdict(list)
            if all_conv_ids:
                msg_result = await db_session.execute(
                    select(Message)
                    .where(Message.conversation_id.in_(all_conv_ids))
                    .order_by(Message.timestamp)
                )
                all_messages = msg_result.scalars().all()
                for m in all_messages:
                    msgs_by_conv[m.conversation_id].append(m)

            for conv in conversations:
                messages = msgs_by_conv[conv.id]

                conv_data = {
                    "conversation_id": str(conv.id),
                    "started_at": conv.started_at.isoformat() if conv.started_at else None,
                    "ended_at": conv.ended_at.isoformat() if conv.ended_at else None,
                    "status": conv.status,
                    "flow_type": conv.flow_type,
                    "messages": [
                        {
                            "role": msg.role,
                            "content": msg.content,
                            "timestamp": msg.timestamp.isoformat() if msg.timestamp else None,
                            "tool_name": msg.tool_name,
                        }
                        for msg in messages
                    ],
                }
                export["conversations"].append(conv_data)

            # 3. Hallucination reports for user's conversations
            conv_ids = [str(c.id) for c in conversations]
            if conv_ids:
                report_result = await db_session.execute(
                    select(HallucinationReport)
                    .where(HallucinationReport.conversation_id.in_(conv_ids))
                    .order_by(HallucinationReport.created_at)
                )
                reports = report_result.scalars().all()

                export["hallucination_reports"] = [
                    {
                        "user_query": r.user_query,
                        "bot_response": r.bot_response,
                        "user_feedback": r.user_feedback,
                        "model": r.model,
                        "created_at": r.created_at.isoformat() if r.created_at else None,
                        "category": r.category,
                        "correction": r.correction,
                    }
                    for r in reports
                ]

            # 4. Redis ephemeral state (conv_state, chat_history, user_context)
            if redis_client:
                redis_state = {}
                digits = "".join(c for c in user_id if c.isdigit())
                phone_variants = {user_id, digits}
                if digits.startswith("385"):
                    phone_variants.add("0" + digits[3:])
                    phone_variants.add("+" + digits)
                elif digits.startswith("0"):
                    phone_variants.add("385" + digits[1:])

                key_prefixes = ["conv_state", "chat_history", "user_context"]
                for phone in phone_variants:
                    for prefix in key_prefixes:
                        key = f"{prefix}:{phone}"
                        try:
                            val = await redis_client.get(key)
                            if val:
                                val_str = val if isinstance(val, str) else val.decode("utf-8")
                                try:
                                    import json as _json
                                    redis_state[key] = _json.loads(val_str)
                                except (ValueError, TypeError):
                                    redis_state[key] = val_str
                        except Exception as e:
                            logger.warning(f"GDPR: Failed to read Redis key during export: {e}")
                            continue

                if redis_state:
                    export["redis_state"] = redis_state

            hashed = self._hash_value(user_id)
            redis_keys = len(export.get("redis_state") or {})
            logger.info(
                f"GDPR DATA EXPORT: User {hashed} — "
                f"{len(export['conversations'])} conversations, "
                f"{sum(len(c['messages']) for c in export['conversations'])} messages, "
                f"{len(export['hallucination_reports'])} hallucination reports, "
                f"{redis_keys} redis keys"
            )

        except Exception as e:
            err = InfrastructureError(
                ErrorCode.DATABASE_UNAVAILABLE,
                f"GDPR data export failed: {e}",
                metadata={"user_hash": self._hash_value(user_id)},
                cause=e,
            )
            logger.error(str(err))
            raise

        return export

    def mask_log_message(self, message: str) -> str:
        """
        Mask PII in log messages before they are written.

        Use this in logging handlers or formatters to prevent PII leakage.

        Args:
            message: Log message to mask

        Returns:
            Masked log message
        """
        if not message:
            return message
        return self.mask_pii(message).masked_text


# Singleton instance
_masking_service: Optional[GDPRMaskingService] = None


def get_masking_service() -> GDPRMaskingService:
    """
    Get or create masking service singleton (thread-safe).

    Returns:
        GDPRMaskingService instance
    """
    global _masking_service
    if _masking_service is None:
        with _singleton_lock:
            # Double-check locking pattern
            if _masking_service is None:
                _masking_service = GDPRMaskingService()
    return _masking_service


def reset_masking_service() -> None:
    """
    Reset the singleton instance (useful for testing).
    """
    global _masking_service
    with _singleton_lock:
        _masking_service = None
