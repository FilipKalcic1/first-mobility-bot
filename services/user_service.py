"""
User Service

User identity management.
DEPENDS ON: api_gateway.py, cache_service.py, models.py, config.py

Uses SchemaExtractor for schema-driven field access
"""

import logging
from datetime import datetime, timezone
from typing import Optional, Tuple, Dict, Any

from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert as pg_insert
from models import UserMapping
from config import get_settings
from services.api_gateway import HttpMethod
from services.schema_extractor import get_schema_extractor
from services.tenant_service import get_tenant_service
from services.errors import ConversationError, ErrorCode
from services.tracing import get_tracer, trace_span
logger = logging.getLogger(__name__)
_tracer = get_tracer("user_service")
def _get_settings():
    return get_settings()

class UserService:
    """
    User identity management.

    Handles:
    - User lookup by phone
    - Auto-onboarding from API
    - Context building
    """

    def __init__(
        self,
        db: AsyncSession,
        gateway=None,
        cache=None,
        redis_client=None
    ):
        """
        Initialize user service.

        Args:
            db: Database session
            gateway: API Gateway (optional)
            cache: Cache service (optional)
            redis_client: Redis client for tenant caching (optional)
        """
        self.db = db
        self.gateway = gateway
        self.cache = cache
        self.redis = redis_client
        self.default_tenant_id = _get_settings().tenant_id

        # Initialize tenant service for dynamic tenant resolution
        self._tenant_service = get_tenant_service(db_session=db, redis_client=redis_client)

    async def get_active_identity(self, phone: str) -> Optional['UserMapping']:
        """
        Get user from database, trying multiple phone formats.

        Args:
            phone: Phone number

        Returns:
            UserMapping or None
        """
        with trace_span(_tracer, "user_service.get_active_identity", {"phone_suffix": phone[-4:] if phone else ""}) as span:
            try:
                # Generate possible phone number variations
                variations = set([phone])

                digits_only = "".join(filter(str.isdigit, phone))
                if digits_only != phone:
                    variations.add(digits_only)

                # From +385... to 385...
                if phone.startswith("+"):
                    variations.add(phone[1:])

                # From 385... to +385...
                if phone.startswith("385"):
                    variations.add("+" + phone)

                # From 385... to 0...
                if digits_only.startswith("385") and len(digits_only) > 3:
                    variations.add("0" + digits_only[3:])

                # From 0... to 385...
                if digits_only.startswith("0") and len(digits_only) > 1:
                    variations.add("385" + digits_only[1:])

                logger.debug(f"Attempting user lookup with phone variations: {variations}")

                span.set_attribute("variations_count", len(variations))

                stmt = select(UserMapping).where(
                    UserMapping.phone_number.in_(list(variations)),
                    UserMapping.is_active == True
                )

                result = await self.db.execute(stmt)
                users = result.scalars().all()

                # If different users share phone-variation siblings, refuse rather
                # than return a nondeterministic one (cross-account takeover risk).
                distinct_ids = {u.id for u in users}
                if len(distinct_ids) > 1:
                    err = ConversationError(
                        ErrorCode.USER_NOT_FOUND,
                        f"Ambiguous phone ***{phone[-4:]} matched {len(distinct_ids)} distinct users",
                        metadata={"sender_suffix": phone[-4:], "distinct_count": len(distinct_ids)},
                    )
                    logger.error(f"{err}")
                    span.set_attribute("result.ambiguous", True)
                    return None

                user = users[0] if users else None

                if user:
                    logger.info(f"Found active user for phone '***{phone[-4:]}' using variation '***{user.phone_number[-4:]}'")
                    span.set_attribute("result.found", True)
                else:
                    err = ConversationError(
                        ErrorCode.USER_NOT_FOUND,
                        f"No active user found for phone ***{phone[-4:]} with {len(variations)} variations",
                        metadata={"sender_suffix": phone[-4:], "variations_count": len(variations)},
                    )
                    logger.info(f"{err}")
                    span.set_attribute("result.found", False)

                return user
            except SQLAlchemyError as e:
                logger.error(f"DB lookup failed for phone '***{phone[-4:]}': {type(e).__name__}: {e}")
                span.set_attribute("result.error", type(e).__name__)
                return None
            except Exception as e:
                logger.error(f"Unexpected error in user lookup for phone '***{phone[-4:]}': {type(e).__name__}: {e}")
                span.set_attribute("result.error", type(e).__name__)
                return None

    async def record_consent(self, phone: str, given: bool) -> bool:
        """Record GDPR consent decision for a user.

        Args:
            phone: User phone number
            given: True if user accepted, False if declined

        Returns:
            True if consent was recorded successfully
        """
        try:
            user = await self.get_active_identity(phone)
            if not user:
                logger.warning(f"Cannot record consent - user not found: ***{phone[-4:]}")
                return False

            user.gdpr_consent_given = given
            user.gdpr_consent_at = datetime.now(timezone.utc) if given else None
            await self.db.commit()
            logger.info(f"GDPR consent {'accepted' if given else 'declined'} for ***{phone[-4:]}")
            return True
        except SQLAlchemyError as e:
            logger.error(f"Failed to record consent for ***{phone[-4:]}: {e}")
            await self.db.rollback()
            return False

    async def try_auto_onboard(self, phone: str) -> Optional[Tuple[str, str]]:
        """
        Try to auto-onboard user from MobilityOne API.

        OPTIMIZED:
        - Only 2 key phone variations (international + local) instead of 6
        - Stops on first match (no redundant calls)
        - Fast path: ~1 API call for most cases
        """
        with trace_span(_tracer, "user_service.try_auto_onboard", {"phone_suffix": phone[-4:] if phone else ""}) as span:
            if not self.gateway:
                logger.error("ONBOARD FAIL: Gateway is None!")
                span.set_attribute("result.error", "gateway_none")
                return None

            logger.info(f"AUTO-ONBOARD START for {phone[-4:]}...")

            try:
                digits_only = "".join(c for c in phone if c.isdigit())

                # Only 2 key variations: international (385...) and local (0...)
                # These cover 99% of cases without redundant API calls
                variations = []
                if digits_only.startswith("385"):
                    variations.append(digits_only)                        # 385991234567
                    variations.append("0" + digits_only[3:])             # 0991234567
                elif digits_only.startswith("0") and len(digits_only) >= 9:
                    variations.append("385" + digits_only[1:])           # 385991234567
                    variations.append(digits_only)                        # 0991234567
                else:
                    variations.append(digits_only)                        # as-is

                span.set_attribute("variations_count", len(variations))

                for phone_var in variations:
                    logger.info(f"Lookup: Phone(=)***{phone_var[-4:]}")

                    response = await self.gateway.execute(
                        method=HttpMethod.GET,
                        path="/tenantmgt/Persons",
                        params={"Filter": f"Phone(=){phone_var}"},
                        max_retries=0  # Fail fast - don't retry on auth/network errors
                    )

                    if not response.success:
                        if response.status_code == 0:
                            # Network/auth error - no point trying other phone variations
                            logger.warning(f"Network/auth error for Phone=***{phone_var[-4:]} - stopping lookup")
                            break
                        elif response.status_code == 500:
                            # Server error - try next variation (might be transient)
                            logger.warning(f"Server error 500 for Phone=***{phone_var[-4:]} - trying next variation")
                            continue
                        continue

                    data = response.data
                    items = data if isinstance(data, list) else data.get("Data", [])

                    if items:
                        person = items[0]
                        person_id = person.get("Id")
                        display_name = person.get("DisplayName", "Korisnik")
                        # CRITICAL: Extract tenant from API — this is the user's REAL company tenant
                        api_tenant_id = person.get("TenantId")

                        # Validate phone matches
                        api_phone = str(person.get("Phone") or person.get("Mobile") or "")
                        if not self._phones_match(phone, api_phone):
                            logger.warning(
                                f"Phone mismatch! Input: {phone[-4:]}, API: {api_phone[-4:] if api_phone else 'N/A'}"
                            )
                            continue

                        logger.info(
                            f"User found: {display_name}, "
                            f"person_id={person_id[:8] if person_id else 'N/A'}..., "
                            f"tenant_id={api_tenant_id or 'N/A'}"
                        )
                        await self._upsert_mapping(phone, person_id, display_name, tenant_id=api_tenant_id)
                        vehicle_info = await self._get_vehicle_info(person_id)
                        span.set_attribute("result.found", True)
                        span.set_attribute("result.display_name", display_name)
                        return (display_name, vehicle_info)

                logger.info(f"User not in MobilityOne: {phone[-4:]}...")
                span.set_attribute("result.found", False)
                return None

            except SQLAlchemyError as e:
                logger.error(f"Auto-onboard DB error: {type(e).__name__}: {e}")
                span.set_attribute("result.error", type(e).__name__)
                return None
            except Exception as e:
                logger.error(f"Auto-onboard failed: {type(e).__name__}: {e}", exc_info=True)
                span.set_attribute("result.error", type(e).__name__)
                return None

    def _extract_name(self, person: Dict) -> str:
        """Extract display name from person data."""
        name = (
            person.get("DisplayName") or
            f"{person.get('FirstName', '')} {person.get('LastName', '')}".strip() or
            "Korisnik"
        )

        # Clean "A-1 - Surname, Name" format
        if " - " in name:
            parts = name.split(" - ")
            if len(parts) > 1:
                name_part = parts[-1].strip()
                if ", " in name_part:
                    surname, firstname = name_part.split(", ", 1)
                    name = f"{firstname} {surname}"
                else:
                    name = name_part

        return name

    def _phones_match(self, input_phone: str, api_phone: str) -> bool:
        """
        Validate that phone numbers match.

        Compares last 9 digits to handle different formats:
        - +385955087196
        - 385955087196
        - 0955087196

        Args:
            input_phone: Phone from user input
            api_phone: Phone from API response

        Returns:
            True if phones match
        """
        clean_input = "".join(c for c in str(input_phone) if c.isdigit())
        clean_api = "".join(c for c in str(api_phone) if c.isdigit())

        # Exact match
        if clean_input == clean_api:
            return True

        # Last 9 digits match (handles country code differences)
        if len(clean_input) >= 9 and len(clean_api) >= 9:
            return clean_input[-9:] == clean_api[-9:]

        return False

    async def _get_vehicle_info(self, person_id: str) -> Dict[str, Any]:
        """
        Get ALL vehicle data for person.

        Returns complete data dict from API - no field filtering.
        Schema-driven: returns whatever API provides.

        Returns:
            Dict with ALL fields from MasterData API response
        """
        try:
            response = await self.gateway.execute(
                method=HttpMethod.GET,
                path="/automation/MasterData",
                params={"personId": person_id},
                max_retries=1
            )

            if not response.success:
                logger.warning(f"MasterData API failed for person_id={person_id[:8]}...")
                return {}

            # Use SchemaExtractor - returns ALL fields, no filtering
            extractor = get_schema_extractor()
            vehicle_data = extractor.extract_all(response.data, "get_MasterData")

            # Get vehicle ID from MasterData response for matching
            master_vehicle_id = vehicle_data.get("Id")
            if not master_vehicle_id:
                return vehicle_data

            # WORKAROUND: Use /vehiclemgt/Vehicles endpoint which WORKS with 'vehicles' scope!
            # This has fresh PeriodicActivities data (unlike automation/MasterData)
            try:
                # Use APIGateway execute method (already has token & tenant header management)
                vehicles_response = await self.gateway.execute(
                    method=HttpMethod.GET,
                    path="/vehiclemgt/Vehicles",
                    params={
                        "Filter": f"DriverId={person_id}",
                        "Rows": 20
                    },
                    max_retries=1
                )

                if vehicles_response.success and vehicles_response.data:
                    # Extract vehicles from response
                    vehicles = vehicles_response.data.get("Data", []) if isinstance(vehicles_response.data, dict) else vehicles_response.data

                    if vehicles:
                        # CRITICAL: Match by vehicle ID, don't just take first!
                        matching_vehicle = None
                        for vehicle in vehicles:
                            if vehicle.get("Id") == master_vehicle_id:
                                matching_vehicle = vehicle
                                break

                        if not matching_vehicle and len(vehicles) > 0:
                            # CRITICAL FIX: DON'T take first vehicle blindly!
                            # If user has exactly ONE vehicle, use it (safe)
                            # If user has MULTIPLE vehicles, DON'T guess - leave None
                            if len(vehicles) == 1:
                                matching_vehicle = vehicles[0]
                                logger.info("Single vehicle found for driver, using it")
                            else:
                                # Multiple vehicles, no match - DON'T guess!
                                logger.warning(
                                    f"VEHICLE_SELECTION_NEEDED: Driver has {len(vehicles)} vehicles, "
                                    f"expected ID {master_vehicle_id} not found. "
                                    f"Available: {[v.get('RegistrationNumber', v.get('Id', 'unknown')[:8]) for v in vehicles[:3]]}"
                                )

                        if matching_vehicle:
                            # Extract FRESH PeriodicActivities
                            if "PeriodicActivities" in matching_vehicle and matching_vehicle["PeriodicActivities"]:
                                fresh_activities = matching_vehicle["PeriodicActivities"]
                                # OVERRIDE stale data with FRESH data
                                vehicle_data["PeriodicActivities"] = fresh_activities

            except Exception as e:
                logger.warning(f"Could not fetch from /vehiclemgt/Vehicles: {e}")

            return vehicle_data

        except Exception as e:
            logger.warning(f"Vehicle info failed: {e}")
            return {}

    async def _upsert_mapping(self, phone: str, person_id: str, name: str, tenant_id: str = None) -> None:
        """
        Save user mapping to database with API-discovered tenant.

        MULTI-TENANCY:
        - Uses tenant_id from Persons API response (TenantId field) — primary source
        - Falls back to default tenant if API didn't return TenantId
        """
        # Use API-discovered tenant, fall back to default
        if not tenant_id:
            tenant_id = self._tenant_service.get_default_tenant()
            logger.warning(f"No TenantId from API for {phone[-4:]} — using default: {tenant_id}")

        try:
            now = datetime.now(timezone.utc)
            stmt = pg_insert(UserMapping).values(
                phone_number=phone,
                api_identity=person_id,
                display_name=name,
                tenant_id=tenant_id,  # Dynamic tenant!
                is_active=True,
                updated_at=now
            ).on_conflict_do_update(
                index_elements=['phone_number'],
                set_={
                    'api_identity': person_id,
                    'display_name': name,
                    # NOTE: Don't overwrite tenant_id on conflict - admin may have changed it
                    'is_active': True,
                    'updated_at': now
                }
            )
            await self.db.execute(stmt)
            await self.db.commit()
            logger.info(f"Saved mapping for {phone[-4:]}... with tenant={tenant_id}")
        except IntegrityError as e:
            logger.warning(f"Mapping conflict for {phone[-4:]}...: {e}")
            await self.db.rollback()
        except SQLAlchemyError as e:
            logger.error(f"Save mapping DB error: {type(e).__name__}: {e}")
            await self.db.rollback()
        except Exception as e:
            logger.error(f"Save mapping unexpected error: {type(e).__name__}: {e}")
            await self.db.rollback()

    async def build_context(
        self,
        person_id: str,
        phone: str,
        user_mapping: 'UserMapping' = None
    ) -> Dict[str, Any]:
        """
        Build operational context for user.

        Uses SchemaExtractor for schema-driven field access.
        Caches result for 5 minutes to reduce API calls.

        MULTI-TENANCY:
        - Uses tenant_id from UserMapping if available
        - Falls back to phone-prefix rules
        - Finally falls back to default tenant

        Args:
            person_id: MobilityOne person ID
            phone: Phone number
            user_mapping: Optional UserMapping instance for tenant resolution

        Returns:
            Context dictionary with correct tenant_id
        """
        # Check cache first (5 min TTL)
        cache_key = f"context:{person_id}"
        if self.cache:
            try:
                cached = await self.cache.get_json(cache_key)
                if cached is not None:
                    logger.info(f"BUILD_CONTEXT: Using cached context for {person_id[:8]}...")
                    return cached
            except Exception as e:
                logger.debug(f"Cache read failed: {e}")

        # DYNAMIC TENANT RESOLUTION
        tenant_id = await self._tenant_service.get_tenant_for_user(phone, user_mapping)

        logger.info(f"BUILD_CONTEXT: person_id={person_id[:8]}..., phone=***{phone[-4:]}, tenant_id={tenant_id} (dynamic)")
        context = {
            "person_id": person_id,
            "phone": phone,
            "tenant_id": tenant_id,
            "display_name": "Korisnik",
            "vehicle": {}
        }
        logger.info(f"BUILD_CONTEXT: Created context with keys: {list(context.keys())}, tenant={tenant_id}")

        if not self.gateway or not person_id:
            return context

        try:
            # Get ALL vehicle data - no field filtering
            vehicle_data = await self._get_vehicle_info(person_id)

            if vehicle_data:
                # Pass ALL data from API - schema-driven
                context["vehicle"] = vehicle_data

                # Extract display name if available
                if vehicle_data.get("Driver"):
                    context["display_name"] = self._extract_name({"DisplayName": vehicle_data["Driver"]})

        except Exception as e:
            logger.warning(f"Build context failed: {e}")

        # ALWAYS cache context - with different TTLs based on data quality
        if self.cache:
            try:
                if context.get("vehicle"):
                    # Full context with vehicle data - cache 5 minutes
                    cache_ttl = 300
                    logger.info(f"BUILD_CONTEXT: Cached context for {person_id[:8]}... (5 min TTL)")
                else:
                    # Negative cache (no vehicle data / API failed) - cache 60s
                    # Prevents hammering a dead API on every message
                    cache_ttl = 60
                    logger.info(f"BUILD_CONTEXT: Negative-cached context for {person_id[:8]}... (60s TTL, no vehicle data)")
                await self.cache.set_json(cache_key, context, ttl=cache_ttl)
            except Exception as e:
                logger.debug(f"Cache write failed: {e}")

        return context

    async def invalidate_context_cache(self, person_id: str) -> bool:
        """
        Invalidate cached context for a user.

        Call this when:
        - Vehicle data changes
        - User reports stale data
        - After refresh_user_from_api()

        Args:
            person_id: MobilityOne person ID

        Returns:
            True if cache was invalidated
        """
        if not self.cache:
            return False

        cache_key = f"context:{person_id}"
        try:
            await self.cache.delete(cache_key)
            logger.info(f"Invalidated context cache for {person_id[:8]}...")
            return True
        except Exception as e:
            logger.warning(f"Cache invalidation failed: {e}")
            return False

    async def refresh_user_from_api(self, phone: str) -> Optional[Tuple[str, str]]:
        """
        Force refresh user data from API, ignoring database cache.

        Use this when:
        - User reports wrong vehicle info
        - Suspected stale data
        - After vehicle change

        Args:
            phone: Phone number

        Returns:
            (display_name, vehicle_info) tuple or None
        """
        logger.info(f"FORCE REFRESH for {phone[-4:]}...")

        # Get old person_id BEFORE deleting so we can invalidate cache
        old_user = await self.get_active_identity(phone)
        if old_user and old_user.api_identity:
            await self.invalidate_context_cache(old_user.api_identity)

        # Clear stale api_identity so try_auto_onboard fetches fresh data
        # NOTE: We do NOT delete the row — that would lose gdpr_consent_given
        try:
            stmt = (
                update(UserMapping)
                .where(UserMapping.phone_number == phone)
                .values(api_identity=None, updated_at=datetime.now(timezone.utc))
            )
            await self.db.execute(stmt)
            await self.db.commit()
            logger.info(f"Cleared api_identity for {phone[-4:]} (force refresh)")
        except Exception as e:
            logger.warning(f"Could not clear api_identity: {e}")
            await self.db.rollback()

        # Fresh lookup from API — _upsert_mapping will UPDATE the existing row
        return await self.try_auto_onboard(phone)

    async def verify_user_identity(self, phone: str) -> Dict[str, Any]:
        """
        Debug method to verify user identity chain.

        Returns detailed info about:
        - Database record
        - API lookup result
        - Phone validation status

        Args:
            phone: Phone number

        Returns:
            Debug info dictionary
        """
        result = {
            "phone": phone,
            "database": None,
            "api": None,
            "phone_match": None,
            "recommendation": None
        }

        # 1. Check database
        db_user = await self.get_active_identity(phone)
        if db_user:
            result["database"] = {
                "person_id": db_user.api_identity,
                "display_name": db_user.display_name,
                "updated_at": str(db_user.updated_at) if db_user.updated_at else None
            }

        # 2. Check API
        if self.gateway:
            digits_only = "".join(c for c in phone if c.isdigit())
            response = await self.gateway.execute(
                HttpMethod.GET,
                "/tenantmgt/Persons",
                params={"Filter": f"Phone(=){digits_only}", "Rows": 5}
            )

            if response.success:
                items = response.data.get("Data", []) if isinstance(response.data, dict) else response.data
                if items:
                    api_person = items[0]
                    api_phone = api_person.get("Phone") or api_person.get("Mobile") or ""

                    result["api"] = {
                        "person_id": api_person.get("Id"),
                        "display_name": api_person.get("DisplayName"),
                        "phone": api_phone,
                        "total_matches": len(items)
                    }

                    # 3. Phone validation
                    result["phone_match"] = self._phones_match(phone, api_phone)

        # 4. Recommendation
        if result["database"] and result["api"]:
            db_id = result["database"]["person_id"]
            api_id = result["api"]["person_id"]

            if db_id != api_id:
                result["recommendation"] = "⚠️ DATABASE STALE! person_id mismatch. Run refresh_user_from_api()"
            elif not result["phone_match"]:
                result["recommendation"] = "⚠️ PHONE MISMATCH! Wrong person may be linked. Run refresh_user_from_api()"
            else:
                result["recommendation"] = "✅ OK - database matches API"
        elif result["api"] and not result["database"]:
            result["recommendation"] = "ℹ️ User not in database yet - will be auto-onboarded"
        elif result["database"] and not result["api"]:
            result["recommendation"] = "⚠️ User in database but NOT in API! May be deleted in MobilityOne"
        else:
            result["recommendation"] = "❌ User not found anywhere"

        return result