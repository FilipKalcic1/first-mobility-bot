"""L0.5 — PII Scrubber.

Replaces personally identifiable patterns in user query with placeholders
BEFORE the query is sent to a third-party LLM (Azure OpenAI).

Detected patterns (structural regex, NOT domain hardcoding):
  - Croatian OIB:       11 consecutive digits (with luhn check optional)
  - Croatian JMBG:      13 consecutive digits
  - IBAN:               2 letters + up to 32 alphanumeric
  - Credit card:        13-19 digits in groups
  - Email address:      RFC-shaped local@domain
  - Long phone numbers: 8+ digits (includes user's own number)

Strategy: replace with explicit placeholders so LLM can still reason
("user provided [PHONE]") without seeing real values. Audit log keeps
the redaction record for incident response.

This is privacy infrastructure, not domain knowledge.

API:
    scrubber = PIIScrubber()
    result = scrubber.scrub(query)
    result.scrubbed_text   # safe to send to LLM
    result.redactions      # list of (kind, position) for audit
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class Redaction:
    kind: str         # "OIB" | "JMBG" | "IBAN" | "CARD" | "EMAIL" | "PHONE"
    placeholder: str  # what was substituted in
    original_length: int


@dataclass
class ScrubResult:
    scrubbed_text: str
    redactions: list = field(default_factory=list)


# Order matters — JMBG/OIB go BEFORE CARD because CARD's 13-19 digits
# would otherwise eat them. EMAIL and IBAN have unique-shape prefixes
# so they're safe at the top.
_PATTERNS: list[tuple[str, str, re.Pattern]] = [
    # IBAN: 2 letters + 2 digits + up to 30 alphanumeric (incl. spaces)
    ("IBAN",  "[IBAN_REDACTED]",
     re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9 ]{10,30}\b")),

    # Email: standard shape
    ("EMAIL", "[EMAIL_REDACTED]",
     re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),

    # JMBG: 13 consecutive digits (NOT in groups). Goes BEFORE CARD
    # so the 13-digit JMBG doesn't get classified as a card number.
    ("JMBG",  "[JMBG_REDACTED]",
     re.compile(r"(?<!\d)\d{13}(?!\d)")),

    # OIB: 11 consecutive digits (NOT in groups, NOT matching JMBG)
    ("OIB",   "[OIB_REDACTED]",
     re.compile(r"(?<!\d)\d{11}(?!\d)")),

    # Credit card: 13-19 digits, possibly grouped by spaces or dashes.
    # Specifically requires SOME separator (spaces or dashes between
    # digit groups) so it doesn't catch a single-block 13-digit JMBG.
    ("CARD",  "[CARD_REDACTED]",
     re.compile(r"\b\d{4}[ -]\d{4}[ -]\d{4}[ -]\d{1,7}\b")),

    # Phone numbers: 8-12 digits, optionally prefixed by + or 00.
    # Matches Croatian mobile (385XX...) and landline (0XX...).
    # Digit boundaries (like OIB/JMBG) so it doesn't redact a SUBSTRING of a
    # longer number, nor a legitimate 14+ digit field value (Filip 2026-05-26).
    ("PHONE", "[PHONE_REDACTED]",
     re.compile(r"(?<!\d)(?:\+|00)?\d{8,12}(?!\d)")),
]


class PIIScrubber:
    """Replaces PII with placeholders. Order is fixed by _PATTERNS."""

    def scrub(self, text: Optional[str]) -> ScrubResult:
        if not text:
            return ScrubResult(scrubbed_text=text or "")

        scrubbed = text
        redactions: list[Redaction] = []

        for kind, placeholder, pattern in _PATTERNS:
            # Default-arg binding pins the CURRENT loop values — a bare
            # closure would late-bind and stamp every redaction with the
            # LAST pattern's kind if the callback ever outlived the loop.
            def _replace(match, kind=kind, placeholder=placeholder):
                redactions.append(Redaction(
                    kind=kind,
                    placeholder=placeholder,
                    original_length=len(match.group(0)),
                ))
                return placeholder
            scrubbed = pattern.sub(_replace, scrubbed)

        return ScrubResult(scrubbed_text=scrubbed, redactions=redactions)
