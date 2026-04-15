"""
User Handler - User identification and greeting.

Extracted from engine/__init__.py for better modularity.
"""

import logging
from typing import Dict, Any

from services.user_service import UserService
from services.context import UserContextManager

logger = logging.getLogger(__name__)

class UserHandler:
    """
    Handles user identification and greeting.

    Responsibilities:
    - Identify user from phone number
    - Auto-onboard new users
    - Build personalized greetings
    """

    def __init__(self, db_session, gateway, cache_service) -> None:
        """
        Initialize UserHandler.

        Args:
            db_session: Database session
            gateway: API gateway
            cache_service: Cache service
        """
        self.db = db_session
        self.gateway = gateway
        self.cache = cache_service

    async def identify_user(self, phone: str, db_session=None) -> Dict[str, Any]:
        """
        Identify user and build context with dynamic tenant resolution.

        ALWAYS returns a context — never None.
        If user is not in MobilityOne, returns guest context so bot still works.

        Args:
            phone: User phone number
            db_session: Database session for this request (required for concurrency safety)

        Returns:
            User context dict (always non-None)
        """
        db = db_session or self.db
        user_service = UserService(db, self.gateway, self.cache)

        user = await user_service.get_active_identity(phone)

        if user and user.api_identity:
            # User exists with person_id — build full context
            ctx = await user_service.build_context(user.api_identity, phone, user_mapping=user)
            ctx["display_name"] = user.display_name
            ctx["is_new"] = False
            return ctx

        if user and not user.api_identity:
            # User exists but person_id is missing — try to resolve from API
            logger.info(f"User {phone[-4:]} exists but has no api_identity — attempting auto-onboard")
            result = await user_service.try_auto_onboard(phone)
            if result:
                # Re-read user — api_identity should be updated now
                refreshed = await user_service.get_active_identity(phone)
                if refreshed and refreshed.api_identity:
                    ctx = await user_service.build_context(refreshed.api_identity, phone, user_mapping=refreshed)
                    ctx["display_name"] = refreshed.display_name
                    ctx["is_new"] = False
                    return ctx
            # Auto-onboard failed (API down) — user is registered but we can't get their data
            logger.warning(f"User {phone[-4:]} exists but API unreachable — limited context")
            tenant_id = await user_service._tenant_service.get_tenant_for_user(phone, user)
            return {
                "person_id": None,
                "phone": phone,
                "tenant_id": tenant_id,
                "display_name": user.display_name or "Korisnik",
                "vehicle": {},
                "is_new": False,
                "is_guest": False  # NOT guest — user IS in our DB with consent
            }

        result = await user_service.try_auto_onboard(phone)

        if result:
            display_name, vehicle_data = result
            user = await user_service.get_active_identity(phone)

            if user:
                # Pass user_mapping for dynamic tenant resolution
                ctx = await user_service.build_context(user.api_identity, phone, user_mapping=user)
                ctx["display_name"] = display_name
                ctx["is_new"] = True
                return ctx

            # Auto-onboard succeeded (user IS in MobilityOne) but DB write failed.
            # Build context from auto-onboard data so user isn't treated as guest.
            logger.warning(f"Auto-onboard OK but DB lookup failed for {phone[-4:]} - building context from API data")
            ctx = {
                "person_id": None,
                "phone": phone,
                "tenant_id": user_service.default_tenant_id,
                "display_name": display_name,
                "vehicle": vehicle_data if isinstance(vehicle_data, dict) else {},
                "is_new": True,
                "is_guest": False  # NOT guest — user IS in MobilityOne
            }
            return ctx

        # User not found in MobilityOne — block via guest context
        logger.info(f"Guest user: {phone[-4:]}... - not in MobilityOne")
        return {
            "person_id": None,
            "phone": phone,
            "tenant_id": user_service.default_tenant_id,
            "display_name": "Korisnik",
            "vehicle": {},
            "is_new": True,
            "is_guest": True
        }

    def build_greeting(self, user_context: Dict[str, Any]) -> str:
        """
        Build personalized greeting for new user.

        Args:
            user_context: User context dict

        Returns:
            Greeting message
        """
        # Guest users are blocked by consent gate — this shouldn't be reached
        if user_context.get("is_guest"):
            return (
                "Vaš broj nije registriran u MobilityOne sustavu.\n"
                "Kontaktirajte vašeg administratora za registraciju."
            )

        ctx = UserContextManager(user_context)
        vehicle = ctx.vehicle

        greeting = f"Pozdrav {ctx.display_name}!\n\n"
        greeting += "Ja sam MobilityOne AI asistent.\n\n"

        if vehicle and vehicle.plate:
            greeting += "Vidim da vam je dodijeljeno vozilo:\n"
            greeting += f"   **{vehicle.name or 'vozilo'}** ({vehicle.plate})\n"
            greeting += f"   Kilometraža: {vehicle.mileage or 'N/A'} km\n\n"
            greeting += "Kako vam mogu pomoći?\n"
            greeting += "* Unos kilometraže\n"
            greeting += "* Prijava kvara\n"
            greeting += "* Rezervacija vozila\n"
            greeting += "* Pitanja o vozilu"
        elif vehicle and vehicle.id:
            greeting += f"Vidim da vam je dodijeljeno vozilo: {vehicle.name or 'vozilo'}\n\n"
            greeting += "Kako vam mogu pomoći?"
        else:
            greeting += "Trenutno nemate dodijeljeno vozilo.\n\n"
            greeting += "Želite li rezervirati vozilo? Recite mi:\n"
            greeting += "* Za koji period (npr. 'sutra od 8 do 17')\n"
            greeting += "* Ili samo recite 'Trebam vozilo' pa ćemo dalje"

        return greeting
