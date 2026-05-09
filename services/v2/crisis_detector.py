"""Mental health crisis detector — intercepts before all routing.

ETHICAL OBLIGATION: drivers are a high-stress profession (long hours,
isolation, accidents, financial pressure). When a user signals suicidal
ideation, self-harm, or acute crisis in conversation, the bot MUST NOT
route to a fleet API tool. It must:

1. Acknowledge the signal humanely
2. Provide Croatian crisis hotline (Plavi telefon 116 123, free 24/7)
3. Optional: 112 for medical emergency
4. NOT pretend to be a therapist
5. NOT continue normal flow until user explicitly resumes

This layer runs IMMEDIATELY after PII scrub + identity, BEFORE special
intents and all routing. False positive rate must be near-zero — bot
shouldn't surface crisis hotline for "smrt baterije" or "otkazati život"
in a non-clinical sense. Two-layer detection:

1. **Strict keyword pass**: Croatian phrases that are unambiguous in
   Croatian context (e.g. "ubit ću se", "ne želim više živjeti").
2. **Optional LLM screen** (future): for borderline phrases, ask LLM
   "is this a crisis signal?" before triggering. Currently disabled —
   keeping detector deterministic for predictable behavior.

Why deterministic-first: false positive on "ubit ću tu lozinku" is
acceptable if rare; false negative on real crisis is unacceptable. We
err toward detection, but only on phrases that are clinically clear
in Croatian context.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CrisisSignal:
    """Detected crisis signal."""
    detected: bool
    severity: str = "none"  # none | concern | acute
    matched_phrase: Optional[str] = None
    response: Optional[str] = None


# ACUTE — immediate crisis hotline + medical emergency mention
# Patterns are word-boundary anchored to avoid false positives.
_ACUTE_PATTERNS = [
    # Suicidal ideation — Croatian
    re.compile(r"\b(ubit|ubiti|ubili|ubijem)\s+(ću|cu|ćemo|cemo|si|mi)\s+se\b", re.IGNORECASE),
    re.compile(r"\bne\s+želim\s+više\s+živjeti\b", re.IGNORECASE),
    re.compile(r"\bne\s+zelim\s+vise\s+zivjeti\b", re.IGNORECASE),
    re.compile(r"\bživot\s+nema\s+(smisla|svrhe)\b", re.IGNORECASE),
    re.compile(r"\bzivot\s+nema\s+(smisla|svrhe)\b", re.IGNORECASE),
    re.compile(r"\bne\s+vidim\s+izlaz", re.IGNORECASE),
    re.compile(r"\bbolje\s+(da\s+)?umrem\b", re.IGNORECASE),
    re.compile(r"\bbolje\s+(da\s+)?(me\s+)?nema\b", re.IGNORECASE),
    # Match all Croatian noun declensions of "samoubojstvo": -tvo, -tva,
    # -tvu, -tvom, -tvima → use \w* suffix wildcard.
    re.compile(r"\bsamoubojs?tv\w*\b", re.IGNORECASE),
    re.compile(r"\bsamoubojic\w*\b", re.IGNORECASE),
    # Reflexive "si"/"sebi" for self-harm phrases — Croatian word order
    # is flexible, can come either before or after the verb.
    re.compile(r"\b(?:sebi|si)\s+naškodi(?:ti)?\b", re.IGNORECASE),
    re.compile(r"\bnaškodi(?:ti)?\s+(?:sebi|si)\b", re.IGNORECASE),
    re.compile(r"\b(?:sebi|si)\s+naskodi(?:ti)?\b", re.IGNORECASE),
    re.compile(r"\bnaskodi(?:ti)?\s+(?:sebi|si)\b", re.IGNORECASE),
    re.compile(r"\bpovrijediti\s+(?:sebe|se)\b", re.IGNORECASE),
    re.compile(r"\b(?:sebi|si)\s+naudi(?:ti)?\b", re.IGNORECASE),
    re.compile(r"\bnaudi(?:ti)?\s+(?:sebi|si)\b", re.IGNORECASE),
    # English (driver might switch language under stress)
    re.compile(r"\b(kill|killing)\s+myself\b", re.IGNORECASE),
    re.compile(r"\bsuicid(?:e|al)\b", re.IGNORECASE),
    re.compile(r"\bend\s+(?:my\s+)?life\b", re.IGNORECASE),
    re.compile(r"\bself[\-\s]harm\b", re.IGNORECASE),
    re.compile(r"\bdon'?t\s+want\s+to\s+live\b", re.IGNORECASE),
]

# CONCERN — distress signals; bot acknowledges but doesn't trigger full
# hotline response. May surface "hoćeš razgovarati s nekim?" prompt.
_CONCERN_PATTERNS = [
    re.compile(r"\bne\s+mogu\s+više\b", re.IGNORECASE),
    # Croatian declensions of "očajan" (m), "očajna" (f), "očajno" (n),
    # plus "očajen", noun "očaj". Use suffix wildcard with anchor.
    re.compile(r"\boča?j(?:a[nm]|na|no|en|ni|ne)?\b", re.IGNORECASE),
    re.compile(r"\boca?j(?:a[nm]|na|no|en|ni|ne)?\b", re.IGNORECASE),
    re.compile(r"\bsve\s+je\s+sranje\b", re.IGNORECASE),
    re.compile(r"\bne\s+znam\s+što\s+ću\s+sa?\s+sobom\b", re.IGNORECASE),
]

# Negative match patterns — context that disqualifies false positive.
# E.g. "ubit ću tu lozinku" / "kill the engine" — figurative.
_FALSE_POSITIVE_CONTEXT = [
    re.compile(r"\bubit\s+ću\s+(tu\s+)?(lozinku|aplikaciju|servis|server|app)\b", re.IGNORECASE),
    re.compile(r"\bkill\s+(the\s+)?(server|process|engine|app)\b", re.IGNORECASE),
]


_ACUTE_RESPONSE = (
    "Čujem te. Žao mi je što prolaziš kroz teško razdoblje.\n\n"
    "Molim te, kontaktiraj Plavi telefon — besplatno, 24/7, povjerljivo:\n"
    "📞 *116 123*\n\n"
    "Ako si u neposrednoj opasnosti, nazovi *112*.\n\n"
    "Ovaj sustav je za fleet management i ne zamjenjuje stručnu pomoć. "
    "Tvoj posao može pričekati — molim te, pazi na sebe."
)


_CONCERN_RESPONSE = (
    "Čujem da ti je teško. Ako trebaš razgovor, Plavi telefon je dostupan "
    "besplatno 24/7 na 📞 *116 123*.\n\n"
    "Ako se vratiš na pitanje vezano uz fleet, samo napiši i nastavljam."
)


def detect(query: str) -> CrisisSignal:
    """Run keyword detection on user message. O(N) over patterns.
    Returns CrisisSignal with detected=True if any pattern matched
    AND no false-positive context disqualifies it.
    """
    if not query or not query.strip():
        return CrisisSignal(detected=False)

    text = query.strip()

    # Disqualify obvious false positives FIRST
    for fp in _FALSE_POSITIVE_CONTEXT:
        if fp.search(text):
            return CrisisSignal(detected=False)

    # Acute
    for pat in _ACUTE_PATTERNS:
        m = pat.search(text)
        if m:
            return CrisisSignal(
                detected=True,
                severity="acute",
                matched_phrase=m.group(0),
                response=_ACUTE_RESPONSE,
            )

    # Concern
    for pat in _CONCERN_PATTERNS:
        m = pat.search(text)
        if m:
            return CrisisSignal(
                detected=True,
                severity="concern",
                matched_phrase=m.group(0),
                response=_CONCERN_RESPONSE,
            )

    return CrisisSignal(detected=False)
