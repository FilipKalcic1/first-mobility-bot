"""Action Intent types + heuristic detector — no ML dependencies.

HTTP-verb keyword classifier for Croatian/English bot queries. Deterministic
precedence: DELETE > UPDATE > CREATE > READ > NONE > UNKNOWN. Deletion/update
keywords beat create/read because "obriši rezervaciju" is unambiguous even
though "rezervacija" alone suggests read/create context.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import List

from services.dynamic_threshold import ClassificationSignal, NO_SIGNAL


class ActionIntent(str, Enum):
    """HTTP action intent detected from user query."""
    READ = "GET"
    CREATE = "POST"
    UPDATE = "PUT"
    DELETE = "DELETE"
    UNKNOWN = "UNKNOWN"
    NONE = "NONE"  # greetings, help, small-talk


@dataclass
class IntentDetectionResult:
    intent: ActionIntent
    confidence: float
    signal: ClassificationSignal = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.signal is None:
            self.signal = ClassificationSignal.from_alternatives(
                self.confidence, [], n_classes=6,
            )


# Left word boundary that respects Croatian diacritics.
_WORD = r"(?<![a-zA-ZčćžšđČĆŽŠĐ])"

from services.config_loader import load_json as _load_json

_VERBS = _load_json("domain", "http_verbs_hr.json")["action_intent_keywords"]
_DELETE_KEYWORDS = _VERBS["delete"]
_UPDATE_KEYWORDS = _VERBS["update"]
_CREATE_KEYWORDS = _VERBS["create"]
_READ_KEYWORDS = _VERBS["read"]
_GREETING_KEYWORDS = _VERBS["greeting"]


def _compile_alts(keywords: List[str]) -> re.Pattern:
    escaped = sorted((re.escape(k) for k in keywords), key=len, reverse=True)
    return re.compile(_WORD + r"(?:" + "|".join(escaped) + r")", re.IGNORECASE)


_DELETE_RE = _compile_alts(_DELETE_KEYWORDS)
_UPDATE_RE = _compile_alts(_UPDATE_KEYWORDS)
_CREATE_RE = _compile_alts(_CREATE_KEYWORDS)
_READ_RE = _compile_alts(_READ_KEYWORDS)
_GREETING_RE = _compile_alts(_GREETING_KEYWORDS)

_PRECEDENCE: List[tuple] = [
    (_DELETE_RE, ActionIntent.DELETE, 0.9),
    (_UPDATE_RE, ActionIntent.UPDATE, 0.85),
    (_CREATE_RE, ActionIntent.CREATE, 0.85),
    (_READ_RE, ActionIntent.READ, 0.8),
    (_GREETING_RE, ActionIntent.NONE, 0.7),
]


def detect_action_intent(query: str) -> IntentDetectionResult:
    """Deterministic intent detection by keyword precedence."""
    q = (query or "").strip()
    if not q:
        return IntentDetectionResult(ActionIntent.UNKNOWN, 0.0)

    for pattern, intent, conf in _PRECEDENCE:
        if pattern.search(q):
            return IntentDetectionResult(intent, conf)

    return IntentDetectionResult(ActionIntent.UNKNOWN, 0.0)


__all__ = ["ActionIntent", "IntentDetectionResult", "detect_action_intent", "NO_SIGNAL"]
