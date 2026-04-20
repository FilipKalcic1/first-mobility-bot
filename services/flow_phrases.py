"""
Flow Phrases - Centralized phrase matching for conversation flow.

Replaces hardcoded string matching across engine/__init__.py,
unified_router.py, and other modules.

Uses word-boundary matching to prevent substring false positives
(e.g., "NEKAKO" matching "ne").
"""

import re
import logging
from typing import List, Optional

from services.config_loader import load_json

logger = logging.getLogger(__name__)

# Phrase tables in config/flow/phrases_hr.json — editable without Python PR.
_PHRASES = load_json("flow", "phrases_hr.json")

SHOW_MORE_PHRASES: List[str] = _PHRASES["show_more"]
CONFIRM_YES_PHRASES: List[str] = _PHRASES["confirm_yes"]
CONFIRM_NO_PHRASES: List[str] = _PHRASES["confirm_no"]
EXIT_SIGNALS: List[str] = _PHRASES["exit_signals"]
ORDINAL_PHRASES: List[str] = _PHRASES["ordinals"]

# GDPR Consent message — shown ONCE before first interaction.
# User must reply "da", "prihvaćam", or "slažem se" to proceed.
GDPR_CONSENT_MESSAGE = (
    "Pozdrav! Ja sam MobilityOne AI asistent — automatizirani sustav, ne čovjek.\n\n"
    "Prije nego počnemo, trebam vaš pristanak:\n\n"
    "• Obrađujem vaše poruke kako bih vam pomogao s vozilima, "
    "rezervacijama i prijavom kvarova\n"
    "• Vaši podaci se čuvaju 365 dana i možete zatražiti brisanje u bilo kojem trenutku\n"
    "• Više informacija: kontaktirajte vašeg administratora\n\n"
    "Prihvaćate li uvjete korištenja? Odgovorite sa **Da** ili **Ne**."
)

GDPR_CONSENT_DECLINED = (
    "Razumijem. Bez pristanka ne mogu obrađivati vaše poruke.\n\n"
    "Ako se predomislite, jednostavno pošaljite novu poruku."
)

GDPR_CONSENT_REPEAT = (
    "Molim vas odgovorite sa **Da** ili **Ne** na uvjete korištenja "
    "kako bih mogao nastaviti."
)

# Consent keywords — also in config/flow/phrases_hr.json
CONSENT_ACCEPT_KEYWORDS = set(_PHRASES["consent_accept"])
CONSENT_DECLINE_KEYWORDS = set(_PHRASES["consent_decline"])

# Greeting phrases
# EU AI Act: Bot MUST self-identify as AI on every greeting interaction.
_AI_DISCLOSURE = "Ja sam MobilityOne AI asistent."

GREETING_PHRASES = {
    "bok": f"Bok! {_AI_DISCLOSURE} Kako vam mogu pomoći?",
    "hej": f"Hej! {_AI_DISCLOSURE} Kako vam mogu pomoći?",
    "pozdrav": f"Pozdrav! {_AI_DISCLOSURE} Kako vam mogu pomoći?",
    "zdravo": f"Zdravo! {_AI_DISCLOSURE} Kako vam mogu pomoći?",
    "dobar dan": f"Dobar dan! {_AI_DISCLOSURE} Kako vam mogu pomoći?",
    "dobro jutro": f"Dobro jutro! {_AI_DISCLOSURE} Kako vam mogu pomoći?",
    "dobra večer": f"Dobra večer! {_AI_DISCLOSURE} Kako vam mogu pomoći?",
    "dobra vecer": f"Dobra večer! {_AI_DISCLOSURE} Kako vam mogu pomoći?",
    "hvala": "Nema na čemu! Trebate li još nešto?",
    "thanks": "You're welcome! Need anything else?",
    "help": f"{_AI_DISCLOSURE} Mogu vam pomoći s:\n• Rezervacija vozila\n• Unos kilometraže\n• Prijava kvara\n• Informacije o vozilu",
    "pomoc": f"{_AI_DISCLOSURE} Mogu vam pomoći s:\n• Rezervacija vozila\n• Unos kilometraže\n• Prijava kvara\n• Informacije o vozilu",
    "pomoć": f"{_AI_DISCLOSURE} Mogu vam pomoći s:\n• Rezervacija vozila\n• Unos kilometraže\n• Prijava kvara\n• Informacije o vozilu",
}

# ---
# MATCHING FUNCTIONS (Word-boundary safe)
# ---

# Cache compiled regex patterns
_pattern_cache: dict = {}

def _get_word_pattern(phrase: str) -> re.Pattern:
    """Get compiled regex pattern for word-boundary matching."""
    if phrase not in _pattern_cache:
        # Escape regex special chars, add word boundaries
        escaped = re.escape(phrase)
        # Use \b for word boundary - prevents "nekako" matching "ne"
        _pattern_cache[phrase] = re.compile(
            r'(?:^|\b)' + escaped + r'(?:\b|$)',
            re.IGNORECASE
        )
    return _pattern_cache[phrase]

def matches_any(text: str, phrases: List[str]) -> bool:
    """
    Check if text matches any phrase using word-boundary matching.

    This prevents false positives like:
    - "nekako" matching "ne"
    - "pokaži" matching "pokaz" (this IS valid because pokaz is prefix)
    - "danas" matching "da"

    For single-word phrases (like "da", "ne"), uses strict word boundary.
    For multi-word phrases, uses substring match (they're specific enough).
    """
    text_lower = text.lower().strip()

    for phrase in phrases:
        if ' ' in phrase:
            # Multi-word phrase: substring match is safe (specific enough)
            if phrase in text_lower:
                return True
        else:
            # Single-word: use word boundary to prevent substring matches
            pattern = _get_word_pattern(phrase)
            if pattern.search(text_lower):
                return True

    return False

def matches_show_more(text: str) -> bool:
    """Check if text is a 'show more' request.

    Excludes exit signals that share words with show_more.
    E.g., "nešto drugo" contains "drugo" (show_more) but is an exit signal.
    """
    # Exit signals take priority - "nešto drugo" is NOT "show more"
    if matches_any(text, EXIT_SIGNALS):
        return False
    return matches_any(text, SHOW_MORE_PHRASES)

def matches_confirm_yes(text: str) -> bool:
    """Check if text is a confirmation 'yes'."""
    return matches_any(text, CONFIRM_YES_PHRASES)

def matches_confirm_no(text: str) -> bool:
    """Check if text is a confirmation 'no'."""
    # CRITICAL: Check "show more" first - it takes priority
    if matches_show_more(text):
        return False
    return matches_any(text, CONFIRM_NO_PHRASES)

def matches_exit_signal(text: str) -> bool:
    """Check if text is an exit/cancellation signal."""
    return matches_any(text, EXIT_SIGNALS)

def matches_item_selection(text: str) -> bool:
    """Check if text is a numeric or ordinal item selection."""
    text_stripped = text.strip()
    if text_stripped.isdigit():
        return True
    return matches_any(text, ORDINAL_PHRASES)

def matches_greeting(text: str) -> Optional[str]:
    """Check if text is a greeting and return response."""
    text_lower = text.lower().strip()

    for greeting, response in GREETING_PHRASES.items():
        if text_lower == greeting or text_lower.startswith(greeting + " "):
            return response

    return None
