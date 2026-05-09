"""Input sanitizer — defends against direct prompt injection from user.

Different from output_sanitizer (which guards LLM Pass 2 from API-side
injection). This module sanitizes the USER message before it reaches any
LLM call.

Defends against:
1. Role-injection markers in user msg ([SYSTEM:], <<INST>>, ### USER:)
2. Imperative bypass attempts ("ignoriraj prethodno", "you are now admin")
3. Mutation-gate framing bypass ("samo pokaži kako bi izgledalo da obrišeš X")
4. Long prompt-stuffing (cap to MAX_USER_MSG)
5. Excessive repetition (token-flooding DoS)

Returns SanitizedInput with cleaned text, severity flag, and warnings.
Severity 'block' → engine returns refusal message instead of routing.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SanitizedInput:
    cleaned: str
    severity: str = "ok"  # ok | warn | block
    warnings: list[str] = field(default_factory=list)
    blocked_reason: str = ""

    @property
    def should_block(self) -> bool:
        return self.severity == "block"


MAX_USER_MSG = 2000
MAX_REPEAT_TOKEN = 50  # same word repeated > 50× → flooding


# Role-injection tags — strip
_TAG_PATTERNS = [
    re.compile(r"\[/?(?:SYSTEM|INST|USER|ASSISTANT|FUNCTION|TOOL)[^\]]*\]", re.IGNORECASE),
    re.compile(r"<<\s*(?:SYS|INST|/SYS|/INST)\s*>>", re.IGNORECASE),
    re.compile(r"###\s*(?:SYSTEM|USER|ASSISTANT|TOOL):", re.IGNORECASE),
    re.compile(r"<\|im_start\|>", re.IGNORECASE),
    re.compile(r"<\|im_end\|>", re.IGNORECASE),
]

# Imperative bypass attempts — block (high severity)
_BYPASS_PATTERNS = [
    re.compile(r"\b(?:ignor(?:e|iraj|iraju)|zanemari|zanemarite)\s+(?:all\s+|sve\s+)?(?:previous|prior|above|earlier|prethodne|gornje)\s+(?:instructions?|prompts?|rules?|upute|pravila)", re.IGNORECASE),
    re.compile(r"\bdisregard\s+(?:all\s+)?(?:previous|prior|above)", re.IGNORECASE),
    re.compile(r"\byou\s+are\s+now\s+(?:a\s+)?(?:admin|root|developer|jailbroken)", re.IGNORECASE),
    re.compile(r"\bnova\s+uput[ae]\s*:", re.IGNORECASE),
    re.compile(r"\bzaboravi\s+(?:sve|prethodno|sva\s+pravila)", re.IGNORECASE),
    re.compile(r"\bact\s+as\s+(?:a\s+)?(?:different|new|admin|root|developer)", re.IGNORECASE),
    re.compile(r"\bpretend\s+(?:you\s+are|to\s+be)\s+(?:admin|root|jailbroken)", re.IGNORECASE),
]

# Mutation-gate framing bypass — block
# "samo pokaži kako bi izgledalo da obrišeš X" / "show what would happen if you delete"
_FRAMING_BYPASS_PATTERNS = [
    re.compile(r"\b(?:samo\s+)?(?:pokaži|pokazi|prikaži|prikazi)\s+(?:mi\s+)?(?:kako\s+bi|šta\s+bi|sto\s+bi|kakav\s+bi)\s+(?:izgledalo|bilo)\s+(?:da|kad|ako)", re.IGNORECASE),
    re.compile(r"\bshow\s+(?:me\s+)?(?:what\s+would\s+happen|how\s+it\s+would\s+look)\s+(?:if|when)\s+(?:you\s+)?(?:delete|drop|remove|erase|cancel|otkaž)", re.IGNORECASE),
    re.compile(r"\bsimulir(?:aj|aš)\s+(?:brisanje|otkazivanje)", re.IGNORECASE),
    re.compile(r"\bhipotetski\s+(?:obriš|izbriš|otkaž)", re.IGNORECASE),
    re.compile(r"\bhypothetical(?:ly)?\s+(?:delete|drop|remove)", re.IGNORECASE),
]

# Output prompt-leak attempts — warn (don't block, log)
_LEAK_PATTERNS = [
    re.compile(r"\b(?:show|display|print|reveal|reci|pokaži|pokazi|otkrij)\s+(?:me\s+|mi\s+)?(?:your|tvoj|tvoje)\s+(?:system\s+prompt|prompt|instructions|upute)", re.IGNORECASE),
    re.compile(r"\bwhat\s+(?:are\s+)?your\s+(?:instructions|rules|system\s+prompt)", re.IGNORECASE),
    re.compile(r"\bkoje\s+su\s+tvoje\s+upute", re.IGNORECASE),
    re.compile(r"\brepeat\s+(?:your\s+)?(?:system\s+prompt|instructions)", re.IGNORECASE),
]


_BLOCK_MESSAGE = (
    "Ne mogu izvršiti taj zahtjev. Ako trebaš stvarnu pomoć, opiši što ti "
    "treba prirodnim jezikom (npr. 'kolika mi je km' ili 'otkaži rezervaciju "
    "za sutra')."
)


def sanitize(text: str) -> SanitizedInput:
    """Sanitize user message. Never raises. Returns SanitizedInput."""
    if not text:
        return SanitizedInput(cleaned="", severity="ok")

    warnings: list[str] = []
    cleaned = text

    # 1. Length cap
    if len(cleaned) > MAX_USER_MSG:
        cleaned = cleaned[:MAX_USER_MSG]
        warnings.append(f"truncated_from:{len(text)}")

    # 2. Token repetition flood detection (preserves prior warnings)
    tokens = cleaned.lower().split()
    if tokens:
        from collections import Counter
        most_common = Counter(tokens).most_common(1)
        if most_common and most_common[0][1] > MAX_REPEAT_TOKEN:
            warnings.append(f"token_flood:{most_common[0][0]}x{most_common[0][1]}")
            return SanitizedInput(
                cleaned="",
                severity="block",
                warnings=warnings,
                blocked_reason="token_flood",
            )

    # 3. Strip role-injection tags
    for pat in _TAG_PATTERNS:
        if pat.search(cleaned):
            warnings.append(f"stripped_tag:{pat.pattern[:30]}")
            cleaned = pat.sub("", cleaned)

    # 4. Detect imperative bypass — BLOCK
    for pat in _BYPASS_PATTERNS:
        if pat.search(cleaned):
            return SanitizedInput(
                cleaned="",
                severity="block",
                warnings=[f"bypass_attempt:{pat.pattern[:40]}"],
                blocked_reason="injection_bypass",
            )

    # 5. Detect mutation-gate framing bypass — BLOCK
    for pat in _FRAMING_BYPASS_PATTERNS:
        if pat.search(cleaned):
            return SanitizedInput(
                cleaned="",
                severity="block",
                warnings=[f"framing_bypass:{pat.pattern[:40]}"],
                blocked_reason="framing_bypass",
            )

    # 6. Prompt-leak attempts — warn but allow
    for pat in _LEAK_PATTERNS:
        if pat.search(cleaned):
            warnings.append(f"prompt_leak_attempt:{pat.pattern[:30]}")
            # Don't block — let bot respond naturally; system prompt
            # won't actually leak because LLM is told it's confidential

    return SanitizedInput(
        cleaned=cleaned.strip(),
        severity="warn" if warnings else "ok",
        warnings=warnings,
    )


def block_message() -> str:
    """Public message returned to user when input is blocked."""
    return _BLOCK_MESSAGE
