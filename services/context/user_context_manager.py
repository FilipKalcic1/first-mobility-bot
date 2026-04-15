"""
User Context Manager

SINGLE SOURCE OF TRUTH for user context operations.

Problem solved:
- 354+ scattered user_context.get() calls
- Silent failures when context is missing
- Inconsistent field access (Id vs VehicleId vs vehicleId)
- No validation before using context values

Usage:
    ctx = UserContextManager(user_context)

    # Safe access (returns None if missing)
    vehicle_id = ctx.vehicle_id

    # Required access (raises MissingContextError if missing)
    vehicle_id = ctx.require_vehicle_id()

    # Vehicle selection (raises VehicleSelectionRequired if multiple)
    vehicle_id = ctx.get_vehicle_id_or_ask()
"""

import logging
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, field

from services.utils.pattern_registry import UUID_PATTERN

logger = logging.getLogger(__name__)


# ---
# EXCEPTIONS
# ---

class ContextError(Exception):
    """Base exception for context errors."""
    pass


class MissingContextError(ContextError):
    """Raised when required context is missing."""

    def __init__(
        self,
        param: str,
        prompt_hr: str,
        details: str = ""
    ):
        self.param = param
        self.prompt_hr = prompt_hr
        self.details = details
        super().__init__(f"Missing required context: {param}")


class VehicleSelectionRequired(ContextError):
    """Raised when user has multiple vehicles and must select one."""

    def __init__(
        self,
        vehicles: List[Dict[str, Any]],
        prompt_hr: str = "Imate više vozila. Koje vozilo?"
    ):
        self.vehicles = vehicles
        self.prompt_hr = prompt_hr
        super().__init__(f"Vehicle selection required: {len(vehicles)} vehicles")


class InvalidContextError(ContextError):
    """Raised when context value is invalid."""

    def __init__(self, param: str, value: Any, reason: str):
        self.param = param
        self.value = value
        self.reason = reason
        super().__init__(f"Invalid {param}: {reason}")


# ---
# VEHICLE CONTEXT
# ---

@dataclass
class VehicleContext:
    """
    Structured vehicle data extracted from user_context["vehicle"].

    Handles field name variations:
    - Id, VehicleId, vehicleId → id
    - LicencePlate, RegistrationNumber, Plate → plate
    - FullVehicleName, DisplayName, Name → name
    """
    id: Optional[str] = None
    plate: Optional[str] = None
    name: Optional[str] = None
    driver: Optional[str] = None
    mileage: Optional[int] = None
    brand: Optional[str] = None
    model: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "VehicleContext":
        """Create VehicleContext from raw vehicle dict."""
        if not data:
            return cls()

        return cls(
            id=cls._extract_id(data),
            plate=cls._extract_plate(data),
            name=cls._extract_name(data),
            driver=data.get("Driver") or data.get("driver"),
            mileage=cls._extract_mileage(data),
            brand=data.get("Brand") or data.get("brand"),
            model=data.get("Model") or data.get("model"),
            raw=data
        )

    @staticmethod
    def _extract_id(data: Dict) -> Optional[str]:
        """Extract ID with fallback chain."""
        return (
            data.get("Id") or
            data.get("VehicleId") or
            data.get("vehicleId") or
            data.get("id")
        )

    @staticmethod
    def _extract_plate(data: Dict) -> Optional[str]:
        """Extract license plate with fallback chain."""
        return (
            data.get("LicencePlate") or
            data.get("RegistrationNumber") or
            data.get("Plate") or
            data.get("plate") or
            data.get("Registration")
        )

    @staticmethod
    def _extract_name(data: Dict) -> Optional[str]:
        """Extract vehicle name with fallback chain."""
        return (
            data.get("FullVehicleName") or
            data.get("DisplayName") or
            data.get("Name") or
            data.get("name") or
            data.get("VehicleName")
        )

    @staticmethod
    def _extract_mileage(data: Dict) -> Optional[int]:
        """Extract mileage as integer."""
        # Use 'is not None' checks — 0 is a valid mileage but falsy
        for key in ("Mileage", "LastMileage", "CurrentMileage", "mileage"):
            value = data.get(key)
            if value is not None:
                try:
                    return int(value)
                except (ValueError, TypeError):
                    continue
        return None

    def is_valid(self) -> bool:
        """Check if vehicle has required data."""
        return self.id is not None

    def display_string(self) -> str:
        """Get human-readable display string."""
        parts = []
        if self.plate:
            parts.append(self.plate)
        if self.name:
            parts.append(self.name)
        elif self.brand and self.model:
            parts.append(f"{self.brand} {self.model}")
        return " - ".join(parts) if parts else "Nepoznato vozilo"


# ---
# USER CONTEXT MANAGER
# ---

class UserContextManager:
    """
    Centralized user context management.

    SINGLE SOURCE OF TRUTH for:
    - person_id
    - tenant_id
    - vehicle (with all its fields)
    - phone
    - display_name

    Provides:
    - Safe access (returns None if missing)
    - Required access (raises if missing)
    - Validation (checks UUID format, etc.)
    - Vehicle selection handling
    """

    def __init__(self, user_context: Dict[str, Any]):
        """
        Initialize with raw user_context dict.

        Args:
            user_context: Raw context dict from system
        """
        self._raw = user_context or {}
        self._vehicle: Optional[VehicleContext] = None
        self._validated = False

        # Parse vehicle on init
        vehicle_data = self._raw.get("vehicle")
        if vehicle_data and isinstance(vehicle_data, dict):
            self._vehicle = VehicleContext.from_dict(vehicle_data)

        logger.debug(
            f"UserContextManager initialized: "
            f"person_id={self.person_id is not None}, "
            f"vehicle={self._vehicle is not None and self._vehicle.is_valid()}"
        )

    # ---
    # PROPERTIES - Safe access (returns None if missing)
    # ---

    @property
    def person_id(self) -> Optional[str]:
        """Get person_id or None."""
        value = self._raw.get("person_id")
        if value and self._is_valid_uuid(value):
            return value
        return None

    @property
    def tenant_id(self) -> str:
        """Get tenant_id with fallback to default."""
        from config import get_settings
        return self._raw.get("tenant_id") or get_settings().tenant_id

    @property
    def phone(self) -> Optional[str]:
        """Get phone number or None."""
        return self._raw.get("phone")

    @property
    def display_name(self) -> str:
        """Get display name with fallback."""
        return self._raw.get("display_name") or "Korisnik"

    @property
    def vehicle(self) -> Optional[VehicleContext]:
        """Get parsed vehicle context or None."""
        return self._vehicle

    @property
    def vehicle_id(self) -> Optional[str]:
        """Get vehicle ID or None."""
        if self._vehicle:
            return self._vehicle.id
        return None

    @property
    def vehicle_plate(self) -> Optional[str]:
        """Get vehicle plate or None."""
        if self._vehicle:
            return self._vehicle.plate
        return None

    @property
    def vehicle_name(self) -> Optional[str]:
        """Get vehicle name or None."""
        if self._vehicle:
            return self._vehicle.name
        return None

    @property
    def is_guest(self) -> bool:
        """Check if this is a guest (no person_id)."""
        return self.person_id is None

    @property
    def is_new(self) -> bool:
        """Check if this is a new user (just onboarded)."""
        return bool(self._raw.get("is_new"))

    # ---
    # REQUIRED ACCESS - Raises if missing
    # ---

    def require_person_id(self) -> str:
        """
        Get person_id or raise MissingContextError.

        Returns:
            Valid person_id string

        Raises:
            MissingContextError: If person_id is missing or invalid
        """
        if not self.person_id:
            raise MissingContextError(
                param="person_id",
                prompt_hr="Niste prijavljeni u sustav. Molimo kontaktirajte administratora.",
                details="person_id is None or invalid UUID"
            )
        return self.person_id

    def require_vehicle(self) -> VehicleContext:
        """
        Get vehicle or raise MissingContextError.

        Returns:
            Valid VehicleContext

        Raises:
            MissingContextError: If vehicle is missing
        """
        if not self._vehicle or not self._vehicle.is_valid():
            raise MissingContextError(
                param="vehicle",
                prompt_hr="Nemate dodijeljeno vozilo. Koje vozilo koristite? (npr. ZG-1234-AB)",
                details="No vehicle in context or vehicle.id is None"
            )
        return self._vehicle

    def require_vehicle_id(self) -> str:
        """
        Get vehicle_id or raise MissingContextError.

        Returns:
            Valid vehicle_id string

        Raises:
            MissingContextError: If vehicle_id is missing
        """
        vehicle = self.require_vehicle()
        return vehicle.id

    def require_phone(self) -> str:
        """
        Get phone or raise MissingContextError.

        Returns:
            Phone number string

        Raises:
            MissingContextError: If phone is missing
        """
        if not self.phone:
            raise MissingContextError(
                param="phone",
                prompt_hr="Nepoznat broj telefona.",
                details="phone is None"
            )
        return self.phone

    # ---
    # VEHICLE SELECTION
    # ---

    def has_vehicle(self) -> bool:
        """Check if user has a vehicle in context."""
        return self._vehicle is not None and self._vehicle.is_valid()

    def get_vehicle_id_or_ask(self) -> str:
        """
        Get vehicle_id or raise appropriate error.

        If vehicle exists, returns vehicle_id.
        If no vehicle, raises MissingContextError with prompt.

        Note: For multiple vehicle handling, the calling code should
        check available_vehicles first and handle VehicleSelectionRequired.

        Returns:
            vehicle_id string

        Raises:
            MissingContextError: If no vehicle in context
        """
        if not self.has_vehicle():
            raise MissingContextError(
                param="vehicle_id",
                prompt_hr="Koje vozilo? (unesite tablice npr. ZG-1234-AB)",
                details="No vehicle in context"
            )
        return self._vehicle.id

    # ---
    # VALIDATION HELPERS
    # ---

    def _is_valid_uuid(self, value: str) -> bool:
        """Check if value is valid UUID."""
        if not value or not isinstance(value, str):
            return False
        return UUID_PATTERN.fullmatch(value) is not None

    def validate(self) -> List[str]:
        """
        Validate context and return list of issues.

        Returns:
            List of validation issue strings (empty if valid)
        """
        issues = []

        # Check person_id format
        raw_person_id = self._raw.get("person_id")
        if raw_person_id and not self._is_valid_uuid(raw_person_id):
            issues.append(f"Invalid person_id format: {raw_person_id[:20]}...")

        # Check vehicle
        if self._vehicle:
            if not self._vehicle.id:
                issues.append("Vehicle exists but has no ID")
            elif not self._is_valid_uuid(self._vehicle.id):
                issues.append(f"Invalid vehicle ID format: {self._vehicle.id[:20]}...")

        # Check tenant_id
        raw_tenant_id = self._raw.get("tenant_id")
        if raw_tenant_id and not self._is_valid_uuid(raw_tenant_id):
            issues.append(f"Invalid tenant_id format: {raw_tenant_id[:20]}...")

        self._validated = True

        if issues:
            logger.warning(f"Context validation issues: {issues}")

        return issues

    # ---
    # RAW ACCESS (for backwards compatibility)
    # ---

    def get(self, key: str, default: Any = None) -> Any:
        """
        Get raw value from context (backwards compatible).

        DEPRECATION WARNING: Use specific properties instead.
        """
        logger.debug(f"DEPRECATED: Using ctx.get('{key}') - use specific property instead")
        return self._raw.get(key, default)

    def to_dict(self) -> Dict[str, Any]:
        """Export context as dictionary."""
        result = {
            "person_id": self.person_id,
            "tenant_id": self.tenant_id,
            "phone": self.phone,
            "display_name": self.display_name,
            "is_guest": self.is_guest,
        }
        if self._vehicle:
            result["vehicle"] = self._vehicle.raw
            result["vehicle_id"] = self._vehicle.id
            result["vehicle_plate"] = self._vehicle.plate
        return result

    def __repr__(self) -> str:
        return (
            f"UserContextManager("
            f"person_id={self.person_id is not None}, "
            f"vehicle={self.has_vehicle()}, "
            f"tenant={self.tenant_id[:8] if self.tenant_id else None}...)"
        )
