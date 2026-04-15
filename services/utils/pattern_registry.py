"""
Pattern registry + context-key utilities — no ML dependencies.

Migrated out of `services.patterns` (deleted). Covers:
  - Regex patterns: UUID, Croatian licence plate, VIN, email, phone.
  - Value → param-type mapping (used by dependency_resolver).
  - Context-key canonicalization (PersonId → person_id, etc.).
  - User-specific query detection (possessive pronouns, diacritics-safe).
  - PersonId injection skip-list for tools that operate on shared data.
  - Query-intent enum with keyword detection (no ML fallback).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Pattern


@dataclass
class ValuePattern:
    pattern: Pattern
    param_type: str
    filter_field: str
    description: str


class PatternRegistry:
    """Single source of truth for domain regex patterns."""

    # UUID
    UUID_PATTERN_STR = r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}"
    UUID_PATTERN = re.compile(UUID_PATTERN_STR, re.IGNORECASE)
    UUID_CAPTURE = re.compile(f"({UUID_PATTERN_STR})", re.IGNORECASE)

    # Croatian plates (ZG-1234-AB, ZG 1234 AB, ZG1234AB)
    CROATIAN_PLATE_STR = r"[A-ZČĆŽŠĐ]{2}[\s\-]?\d{3,4}[\s\-]?[A-ZČĆŽŠĐ]{1,2}"
    CROATIAN_PLATE = re.compile(f"^{CROATIAN_PLATE_STR}$", re.IGNORECASE)
    CROATIAN_PLATE_CAPTURE = re.compile(f"({CROATIAN_PLATE_STR})", re.IGNORECASE)

    # VIN (17 alphanum, no I/O/Q)
    VIN_PATTERN_STR = r"[A-HJ-NPR-Z0-9]{17}"
    VIN_PATTERN = re.compile(f"^{VIN_PATTERN_STR}$")

    # Contact
    EMAIL_PATTERN_STR = r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"
    EMAIL_PATTERN = re.compile(f"^{EMAIL_PATTERN_STR}$")
    CROATIAN_PHONE_STR = r"(\+385|00385|0)?[1-9]\d{7,8}"
    CROATIAN_PHONE = re.compile(f"^{CROATIAN_PHONE_STR}$")

    @classmethod
    def get_value_patterns(cls) -> List[ValuePattern]:
        return [
            ValuePattern(cls.CROATIAN_PLATE, "vehicleid", "LicencePlate", "Croatian plate"),
            ValuePattern(cls.VIN_PATTERN, "vehicleid", "VIN", "VIN"),
            ValuePattern(cls.EMAIL_PATTERN, "personid", "Email", "Email"),
            ValuePattern(cls.CROATIAN_PHONE, "personid", "Phone", "Phone"),
        ]

    @classmethod
    def find_uuids(cls, text: str) -> List[str]:
        if not text:
            return []
        return [m.lower() for m in cls.UUID_CAPTURE.findall(text.lower())]

    @classmethod
    def find_plates(cls, text: str) -> List[str]:
        if not text:
            return []
        plates = cls.CROATIAN_PLATE_CAPTURE.findall(text.upper())
        return [p.replace(" ", "-").replace("--", "-") for p in plates]

    @classmethod
    def is_uuid(cls, text: str) -> bool:
        return bool(text) and bool(cls.UUID_PATTERN.fullmatch(text.strip()))

    @classmethod
    def is_croatian_plate(cls, text: str) -> bool:
        return bool(text) and bool(cls.CROATIAN_PLATE.match(text.strip().upper()))

    @classmethod
    def is_vin(cls, text: str) -> bool:
        return bool(text) and bool(cls.VIN_PATTERN.match(text.strip().upper()))

    @classmethod
    def is_email(cls, text: str) -> bool:
        return bool(text) and bool(cls.EMAIL_PATTERN.match(text.strip()))


UUID_PATTERN = PatternRegistry.UUID_PATTERN


# ---------------------------------------------------------------------------
# Context-key canonicalization
# ---------------------------------------------------------------------------

CONTEXT_KEY_CANONICAL: Dict[str, str] = {
    # person
    "personid": "person_id", "person_id": "person_id",
    "userid": "person_id", "user_id": "person_id",
    "driverid": "person_id", "driver_id": "person_id",
    "assignedtoid": "person_id", "assigned_to_id": "person_id",
    "ownerid": "person_id", "owner_id": "person_id",
    "employeeid": "person_id", "employee_id": "person_id",
    "createdby": "person_id", "created_by": "person_id",
    # vehicle
    "vehicleid": "vehicle_id", "vehicle_id": "vehicle_id",
    "carid": "vehicle_id", "car_id": "vehicle_id",
    "assetid": "vehicle_id", "asset_id": "vehicle_id",
    # tenant
    "tenantid": "tenant_id", "tenant_id": "tenant_id",
    "organizationid": "tenant_id", "organization_id": "tenant_id",
    "companyid": "tenant_id", "company_id": "tenant_id",
    "x-tenant-id": "tenant_id",
}


def normalize_context_key(param_name: str):
    if not param_name:
        return None
    return CONTEXT_KEY_CANONICAL.get(param_name.lower())


# ---------------------------------------------------------------------------
# User-specific query detection
# ---------------------------------------------------------------------------

USER_SPECIFIC_PATTERNS: List[str] = [
    r"\bmoje?\b", r"\bmoja\b", r"\bmoji\b",
    r"\bmeni\b", r"\bmi\b",
    r"\bsvoj[aeiou]?\b",
    r"\bsvog(?:a)?\b",
    r"\bsvom(?:e|u)?\b",
    r"dodijeljeno\s+mi", r"zadan[ao]?\s+mi",
    r"za\s+mene", r"pripada\s+mi",
    r"imam\s+li", r"imali?\b",
    r"moj[aei]?\s+vozil", r"moj[aei]?\s+auto",
    r"koji\s+auto.*meni", r"koje\s+vozilo.*meni",
    r"\bmy\b", r"\bmine\b", r"\bme\b",
    r"assigned\s+to\s+me", r"for\s+me", r"belongs\s+to\s+me",
    r"do\s+i\s+have", r"i\s+have",
]
USER_SPECIFIC_COMPILED = [re.compile(p, re.IGNORECASE) for p in USER_SPECIFIC_PATTERNS]


def is_user_specific(query: str) -> bool:
    if not query:
        return False
    q = query.lower().strip()
    return any(p.search(q) for p in USER_SPECIFIC_COMPILED)


USER_FILTER_PARAMS = {
    "personid", "person_id",
    "assignedtoid", "assigned_to_id",
    "driverid", "driver_id",
    "userid", "user_id",
    "ownerid", "owner_id",
    "employeeid", "employee_id",
    "createdby", "created_by",
}


# ---------------------------------------------------------------------------
# PersonId injection skip list
# ---------------------------------------------------------------------------

PERSON_ID_SKIP_PATTERNS = frozenset([
    "available", "location", "site", "config", "setting", "settings",
])


def should_skip_person_id_injection(tool_id: str) -> bool:
    if not tool_id:
        return False
    t = tool_id.lower()
    return any(p in t for p in PERSON_ID_SKIP_PATTERNS)


# ---------------------------------------------------------------------------
# Injectable context (person_id, tenant_id) extraction
# ---------------------------------------------------------------------------

INJECTABLE_CONTEXT_KEYS = frozenset(["person_id", "tenant_id"])


def get_injectable_context(user_context: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, value in user_context.items():
        if value is None:
            continue
        canonical = normalize_context_key(key)
        if canonical and canonical in INJECTABLE_CONTEXT_KEYS:
            out[canonical] = value
    return out


# ---------------------------------------------------------------------------
# Query-intent enum (coarser than ActionIntent) + keyword detection
# ---------------------------------------------------------------------------

class QueryIntent(Enum):
    READ = "read"
    WRITE = "write"
    DELETE = "delete"
    UNKNOWN = "unknown"


_WRITE_KEYWORDS = [
    "rezerviraj", "unesi", "prijavi", "dodaj", "kreiraj", "napravi",
    "ažuriraj", "azuriraj", "promijeni", "promjeni", "zakaži", "zakazi",
    "upiši", "upisi", "stavi", "otvori", "zauzmi", "pošalji", "posalji",
    "add", "create", "update", "change", "book", "reserve", "send", "submit",
]
_DELETE_KEYWORDS = [
    "obriši", "obrisi", "otkaži", "otkazi", "ukloni", "makni",
    "izbriši", "izbrisi", "izbaci", "poništi", "ponisti",
    "delete", "remove", "cancel",
]


def detect_intent(query: str) -> QueryIntent:
    """Coarse-grained intent (READ/WRITE/DELETE) via keyword scan."""
    if not query:
        return QueryIntent.READ
    q = query.lower()
    for kw in _DELETE_KEYWORDS:
        if kw in q:
            return QueryIntent.DELETE
    for kw in _WRITE_KEYWORDS:
        if kw in q:
            return QueryIntent.WRITE
    return QueryIntent.READ


__all__ = [
    "PatternRegistry", "ValuePattern", "UUID_PATTERN",
    "CONTEXT_KEY_CANONICAL", "normalize_context_key",
    "USER_SPECIFIC_PATTERNS", "USER_SPECIFIC_COMPILED",
    "is_user_specific", "USER_FILTER_PARAMS",
    "PERSON_ID_SKIP_PATTERNS", "should_skip_person_id_injection",
    "INJECTABLE_CONTEXT_KEYS", "get_injectable_context",
    "QueryIntent", "detect_intent",
]
