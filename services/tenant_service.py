"""
Tenant Service - Dynamic multi-tenant routing.

Manages tenant identification and routing for multi-tenant deployments.

ARCHITECTURE:
- Each user (phone number) is associated with a tenant (company)
- Tenant determines which API instance/data partition to use
- Tenant ID is DISCOVERED from MobilityOne Persons API response (TenantId field)
- Default tenant from ENV is used ONLY for the initial API lookup
- Once discovered, tenant is stored per-user in UserMapping.tenant_id

TENANT RESOLUTION ORDER:
1. UserMapping.tenant_id (if user exists in DB) -> highest priority
2. Default tenant from settings (for initial API bootstrap only)

KUBERNETES MULTI-TENANT:
- All tenants share same database (filtered by tenant_id)
- Each tenant can have different API endpoints (future)
- Cost tracking and rate limiting per tenant
"""

import logging
import re
from typing import Optional, Dict, Any
from dataclasses import dataclass
import threading

from sqlalchemy import update
from config import get_settings
from models import UserMapping
from services.errors import ConversationError, ErrorCode

logger = logging.getLogger(__name__)
def _get_settings():
    return get_settings()

@dataclass
class TenantConfig:
    """Configuration for a single tenant."""
    tenant_id: str
    name: str
    api_url: Optional[str] = None  # Override API URL per tenant (future)
    rate_limit: int = 20  # Requests per minute
    is_active: bool = True


class TenantService:
    """
    Manages tenant identification and routing.

    Features:
    - API-discovered tenant resolution (from Persons API TenantId field)
    - Per-user tenant storage in DB
    - Default tenant fallback for bootstrap
    - Tenant validation
    """

    def __init__(self, db_session=None, redis_client=None) -> None:
        """
        Initialize TenantService.

        Args:
            db_session: Optional database session for UserMapping lookup
            redis_client: Optional Redis for caching tenant lookups
        """
        self.db = db_session
        self.redis = redis_client
        self.default_tenant = _get_settings().MOBILITY_TENANT_ID

        logger.info(f"TenantService initialized: default={self.default_tenant}")

    def get_default_tenant(self) -> str:
        """
        Get default tenant ID (from env).

        Used ONLY for the initial Persons API call when we don't yet know
        which company a user belongs to. Once discovered, the user's actual
        tenant is stored in UserMapping.tenant_id.

        Returns:
            Default tenant ID from settings
        """
        return self.default_tenant

    async def get_tenant_for_user(
        self,
        phone: str,
        user_mapping: Any = None
    ) -> str:
        """
        Get tenant ID for a user.

        Resolution order:
        1. UserMapping.tenant_id (if provided and set) — discovered from API
        2. Redis cache
        3. Default tenant (fallback for bootstrap)

        Args:
            phone: User phone number
            user_mapping: Optional UserMapping model instance

        Returns:
            Tenant ID
        """
        # 1. Check UserMapping first (highest priority — API-discovered tenant)
        if user_mapping and hasattr(user_mapping, 'tenant_id') and user_mapping.tenant_id:
            logger.debug(f"Using tenant from UserMapping: {user_mapping.tenant_id}")
            return user_mapping.tenant_id

        # 2. Check Redis cache
        if self.redis:
            cache_key = f"tenant:{phone}"
            try:
                cached = await self.redis.get(cache_key)
                if cached:
                    logger.debug(f"Using cached tenant for {phone[-4:]}...: {cached}")
                    return cached
            except Exception as e:
                logger.warning(f"Redis cache read failed: {e}")

        # 3. Default tenant (bootstrap — will be replaced when Persons API returns real TenantId)
        logger.debug(f"Using default tenant for {phone[-4:]}...: {self.default_tenant}")
        return self.default_tenant

    async def update_user_tenant(
        self,
        phone: str,
        new_tenant_id: str,
        admin_id: Optional[str] = None
    ) -> bool:
        """
        Update tenant for a user (admin operation or API discovery).

        Args:
            phone: User phone number
            new_tenant_id: New tenant ID
            admin_id: Admin performing the change (for audit)

        Returns:
            True if updated successfully
        """
        if not self.db:
            logger.error("Cannot update tenant: no database session")
            return False

        try:
            result = await self.db.execute(
                update(UserMapping)
                .where(UserMapping.phone_number == phone)
                .values(tenant_id=new_tenant_id)
            )
            await self.db.commit()

            # Invalidate cache
            if self.redis:
                await self.redis.delete(f"tenant:{phone}")

            logger.info(f"Updated tenant for {phone[-4:]}... to {new_tenant_id} (by {admin_id})")
            return result.rowcount > 0

        except Exception as e:
            logger.error(f"Failed to update tenant: {e}")
            await self.db.rollback()
            return False

    def validate_tenant(self, tenant_id: str) -> bool:
        """
        Validate tenant ID format.

        Args:
            tenant_id: Tenant ID to validate

        Returns:
            True if valid format
        """
        if not tenant_id:
            return False

        # Basic validation - alphanumeric with hyphens, 3-50 chars (UUIDs are 36 chars)
        if not re.match(r'^[a-zA-Z0-9\-]{3,50}$', tenant_id):
            err = ConversationError(ErrorCode.TENANT_MISMATCH, f"Invalid tenant ID format: {tenant_id}")
            logger.warning(str(err))
            return False

        return True

    def get_tenant_stats(self) -> Dict[str, Any]:
        """
        Get tenant service statistics.

        Returns:
            Stats dictionary
        """
        return {
            "default_tenant": self.default_tenant,
            "resolution": "API-discovered (Persons API TenantId field)"
        }

# Singleton instance
_tenant_service: Optional[TenantService] = None
_tenant_singleton_lock = threading.Lock()

def get_tenant_service(db_session=None, redis_client=None) -> TenantService:
    """
    Get or create TenantService singleton.

    Args:
        db_session: Optional database session
        redis_client: Optional Redis client

    Returns:
        TenantService instance
    """
    global _tenant_service

    if _tenant_service is None:
        with _tenant_singleton_lock:
            if _tenant_service is None:
                _tenant_service = TenantService(db_session, redis_client)
                return _tenant_service
    # Always update dependencies — sessions are request-scoped and go stale
    if db_session:
        _tenant_service.db = db_session
    if redis_client:
        _tenant_service.redis = redis_client

    return _tenant_service
