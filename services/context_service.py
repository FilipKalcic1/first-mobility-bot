"""
Context Service

User context and conversation history management.
NO DEPENDENCIES on other services (except config, cache_service).

Phase 4:
- UserContext Pydantic model for type safety
- get_user_context() with cache-first strategy
- Guest Context fallback (Fail-Open design)
- Phone number validation

- Phone number validation in context operations
- Prevents UUID/phone mixup in context keys
- Improved logging for forensic debugging
"""

import json
import time
import logging
from typing import List, Dict, Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from config import get_settings
from services.errors import ConversationError, ErrorCode, InfrastructureError
from services.utils.pattern_registry import UUID_PATTERN
from services.tracing import get_tracer, trace_span

logger = logging.getLogger(__name__)
_tracer = get_tracer("context_service")
def _get_settings():
    """Lazy settings access — avoid module-level parsing before env vars are set."""
    return get_settings()


# ---
# USER CONTEXT MODEL (Phase 4)
# ---

class VehicleContext(BaseModel):
    """Vehicle information from MasterData API."""
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    id: Optional[str] = Field(default=None, alias="Id")
    registration: Optional[str] = Field(default=None, alias="RegistrationNumber")
    driver: Optional[str] = Field(default=None, alias="Driver")
    mileage: Optional[int] = Field(default=None, alias="Mileage")
    brand: Optional[str] = Field(default=None, alias="Brand")
    model: Optional[str] = Field(default=None, alias="Model")
    # Raw data for pass-through (schema-driven)
    raw: Dict[str, Any] = Field(default_factory=dict)


class UserContext(BaseModel):
    """
    User identity context.

    Phase 4: Type-safe user context with cache-first strategy.

    Fields:
        person_id: MobilityOne person UUID
        phone: User phone number (primary key for lookup)
        tenant_id: Tenant ID for multi-tenancy
        display_name: User display name
        vehicle: Vehicle context (if available)
        is_guest: True if this is a guest/fallback context
    """
    person_id: Optional[str] = Field(default=None)
    phone: str = Field(...)
    tenant_id: str = Field(default="")
    display_name: str = Field(default="Korisnik")
    vehicle: Optional[VehicleContext] = Field(default=None)
    is_guest: bool = Field(default=False)
    cached_at: Optional[float] = Field(default=None)

    model_config = ConfigDict(extra="allow")

    @classmethod
    def guest(cls, phone: str) -> "UserContext":
        """
        Create a guest context for unknown users.

        Fail-Open design: Bot continues working even if user lookup fails.
        """
        return cls(
            person_id=None,
            phone=phone,
            tenant_id=_get_settings().tenant_id,
            display_name="Korisnik",
            vehicle=None,
            is_guest=True,
            cached_at=time.time()
        )

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "UserContext":
        """Create UserContext from dictionary (e.g., from cache)."""
        vehicle_data = data.get("vehicle")
        vehicle = None
        if vehicle_data:
            vehicle = VehicleContext(**{**vehicle_data, "raw": vehicle_data})

        return cls(
            person_id=data.get("person_id"),
            phone=data.get("phone", ""),
            tenant_id=data.get("tenant_id", _get_settings().tenant_id),
            display_name=data.get("display_name", "Korisnik"),
            vehicle=vehicle,
            is_guest=data.get("is_guest", False),
            cached_at=data.get("cached_at")
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for caching."""
        result = {
            "person_id": self.person_id,
            "phone": self.phone,
            "tenant_id": self.tenant_id,
            "display_name": self.display_name,
            "is_guest": self.is_guest,
            "cached_at": self.cached_at
        }
        if self.vehicle:
            result["vehicle"] = self.vehicle.raw if self.vehicle.raw else self.vehicle.model_dump(by_alias=True)
        return result


async def get_user_context(
    phone: str,
    cache_service=None,
    user_service=None,
    db_session=None
) -> UserContext:
    """
    Get user context with cache-first strategy.

    Phase 4: Cache-First Identity System

    Strategy:
    1. Try Redis cache first (fast path)
    2. Try database lookup (warm path)
    3. Try API auto-onboard (cold path)
    4. Return Guest Context (fail-open)

    Args:
        phone: User phone number
        cache_service: CacheService instance (optional)
        user_service: UserService instance (optional)
        db_session: Database session (optional)

    Returns:
        UserContext (always returns something - never fails)
    """
    with trace_span(_tracer, "context.get_user", {
        "phone.suffix": phone[-4:] if phone else "N/A",
        "has_cache": cache_service is not None,
        "has_user_service": user_service is not None,
    }) as span:
        result = await _get_user_context_inner(
            phone, cache_service, user_service, db_session,
        )
        span.set_attribute("context.is_guest", result.is_guest)
        span.set_attribute("context.source", "cache" if result.cached_at else "lookup")
        return result


async def _get_user_context_inner(
    phone: str,
    cache_service=None,
    user_service=None,
    db_session=None,
) -> UserContext:
    """Inner implementation of get_user_context."""
    cache_key = f"user_context:{phone}"

    # GATE 1: Try Redis cache
    if cache_service:
        try:
            cached = await cache_service.get_json(cache_key)
            if cached:
                logger.debug(f"USER_CONTEXT: Cache HIT for {phone[-4:]}")
                return UserContext.from_dict(cached)
        except Exception as e:
            err = InfrastructureError(
                ErrorCode.REDIS_UNAVAILABLE,
                f"USER_CONTEXT: Cache read failed: {e}",
                cause=e,
            )
            logger.warning(f"{err}")

    # GATE 2: Try database + API via UserService
    if user_service:
        try:
            # First try database
            user_mapping = await user_service.get_active_identity(phone)

            if user_mapping:
                # Build full context with vehicle data
                context_data = await user_service.build_context(
                    user_mapping.api_identity,
                    phone
                )

                context = UserContext.from_dict(context_data)
                context.cached_at = time.time()

                # Cache for next time
                if cache_service:
                    await cache_service.set_json(
                        cache_key,
                        context.to_dict(),
                        ttl=_get_settings().CACHE_TTL_USER
                    )

                logger.info(f"USER_CONTEXT: DB lookup SUCCESS for {phone[-4:]}")
                return context

            # Try auto-onboard from API
            result = await user_service.try_auto_onboard(phone)
            if result:
                display_name, vehicle_info = result

                # Rebuild context after onboard
                user_mapping = await user_service.get_active_identity(phone)
                if user_mapping:
                    context_data = await user_service.build_context(
                        user_mapping.api_identity,
                        phone
                    )
                    context = UserContext.from_dict(context_data)
                    context.cached_at = time.time()

                    if cache_service:
                        await cache_service.set_json(
                            cache_key,
                            context.to_dict(),
                            ttl=_get_settings().CACHE_TTL_USER
                        )

                    logger.info(f"USER_CONTEXT: Auto-onboard SUCCESS for {phone[-4:]}")
                    return context

        except Exception as e:
            err = InfrastructureError(
                ErrorCode.DATABASE_UNAVAILABLE,
                f"USER_CONTEXT: Lookup failed: {e}",
                cause=e,
            )
            logger.warning(f"{err}")

    # GATE 3: Fail-Open - Return Guest Context
    logger.warning(f"USER_CONTEXT: Returning GUEST context for {phone[-4:]}")
    return UserContext.guest(phone)


class ContextService:
    """
    Manages conversation history in Redis.

    Added phone validation to prevent UUID/phone mixup.
    """

    def __init__(self, redis_client):
        """
        Initialize context service.

        Args:
            redis_client: Redis async client
        """
        self.redis = redis_client
        self.ttl = _get_settings().CACHE_TTL_CONTEXT
        self.max_history = 10  # Matches MAX_HISTORY_MESSAGES in ai_orchestrator

    def _validate_user_id(self, user_id: str) -> bool:
        """
        Validate user_id and warn if it looks like a UUID instead of phone number.

        Detects the UUID trap where person_id gets used
        instead of phone number for context keys. This is a systemic bug —
        we fail loudly rather than silently corrupt conversation state.

        Raises:
            ConversationError: if user_id is empty or looks like a UUID.
        """
        if not user_id:
            raise ConversationError(
                ErrorCode.PHONE_INVALID,
                "CONTEXT: Empty user_id provided",
                metadata={"user_id": user_id},
            )

        if UUID_PATTERN.match(user_id):
            raise ConversationError(
                ErrorCode.PHONE_INVALID,
                "UUID TRAP: user_id appears to be a UUID (person_id), not a phone number. "
                "This indicates a wiring bug at the caller; refusing to key context by UUID.",
                metadata={"user_id_prefix": user_id[:20]},
            )

        return True

    def _key(self, user_id: str) -> str:
        """
        Build Redis key for user history.

        Logs warning if user_id looks like UUID.
        """
        self._validate_user_id(user_id)
        return f"chat_history:{user_id}"

    async def get_history(self, user_id: str) -> List[Dict[str, Any]]:
        """
        Get conversation history.

        Args:
            user_id: User identifier (phone number)

        Returns:
            List of messages
        """
        try:
            key = self._key(user_id)
            raw = await self.redis.lrange(key, 0, -1)

            messages = []
            for item in raw:
                if item:
                    try:
                        messages.append(json.loads(item))
                    except json.JSONDecodeError:
                        continue

            return messages
        except Exception as e:
            err = InfrastructureError(
                ErrorCode.REDIS_UNAVAILABLE,
                f"Get history failed: {e}",
                cause=e,
            )
            logger.warning(f"{err}")
            return []

    async def add_message(
        self,
        user_id: str,
        role: str,
        content: str,
        **kwargs
    ) -> bool:
        """
        Add message to history.

        Args:
            user_id: User identifier
            role: Message role (user, assistant, system, tool)
            content: Message content
            **kwargs: Additional metadata

        Returns:
            True if successful
        """
        key = self._key(user_id)

        message = {
            "role": role,
            "content": content,
            "timestamp": time.time(),
            **kwargs
        }

        try:
            # Add to list
            await self.redis.rpush(key, json.dumps(message))

            # Set expiry
            await self.redis.expire(key, self.ttl)

            # Trim to max length
            length = await self.redis.llen(key)
            if length > self.max_history:
                await self.redis.ltrim(key, -self.max_history, -1)

            return True
        except Exception as e:
            err = InfrastructureError(
                ErrorCode.REDIS_UNAVAILABLE,
                f"Add message failed: {e}",
                cause=e,
            )
            logger.warning(f"{err}")
            return False

    async def clear_history(self, user_id: str) -> bool:
        """
        Clear conversation history.

        Args:
            user_id: User identifier

        Returns:
            True if successful
        """
        try:
            await self.redis.delete(self._key(user_id))
            return True
        except Exception as e:
            err = InfrastructureError(
                ErrorCode.REDIS_UNAVAILABLE,
                f"Clear history failed: {e}",
                cause=e,
            )
            logger.warning(f"{err}")
            return False

    async def get_recent_messages(
        self,
        user_id: str,
        count: int = 10
    ) -> List[Dict[str, str]]:
        """
        Get recent messages formatted for AI.

        Args:
            user_id: User identifier
            count: Number of recent messages

        Returns:
            List of {role, content} dicts
        """
        history = await self.get_history(user_id)

        # Get last N messages
        recent = history[-count:] if len(history) > count else history

        # Format for AI
        formatted = []
        for msg in recent:
            role = msg.get("role", "user")
            content = msg.get("content", "")

            if role in ("user", "assistant") and content:
                formatted.append({"role": role, "content": content})

        return formatted
