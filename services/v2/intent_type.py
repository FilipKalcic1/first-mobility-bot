"""L2a — Intent Type Classifier.

Tiny LLM call (single output) that splits user query into 4 buckets
BEFORE the anchor fast-path runs. Solves the kanibalizacija problem
("Moj auto je smeće jer ne radi kočnica" — the L2b anchor would match
'moj auto' but the user actually wants to file a case).

Output kinds (mutually exclusive):
  question_about_self    user is asking about their own data → L2b
  action_or_complaint    user is reporting / requesting / complaining → L3
  flow_request           user wants a multi-step interaction → L3 → L4
  other                  manager / edge / unclear → L3

Failure modes (Council Pass 4 USER critique: bot must not stall on
basic info even when LLM fails):
  - LLM error → fallback to `question_about_self` (safe default)
  - Confidence < 0.6 → fallback to `question_about_self`
  - Empty query → `other` (handled upstream)

Why a separate fast LLM call instead of folding into L3:
  - L3 reasoning is expensive (top-3 anchor + 950 tools + Judge).
    For 70% of queries (driver basics) it's overkill.
  - L2a is cheap: 4-way classification, ~50 tokens, ~150-250ms.
  - Without L2a: every query incurs L3 cost.
  - With L2a: 70% of queries bypass L3 entirely via L2b.

Trade-off: +200ms on every query (including L2b ones). But L2b path
saves much more (no L3 LLM call, no top-20 reranker).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# Output kinds.
KIND_QUESTION_ABOUT_SELF = "question_about_self"
KIND_ACTION_OR_COMPLAINT = "action_or_complaint"
KIND_FLOW_REQUEST = "flow_request"
KIND_OTHER = "other"

# Confidence floor — below this we fall back to safe default.
SAFE_FALLBACK_CONFIDENCE = 0.6


_SYSTEM_PROMPT = (
    "Ti si klasifikator namjere korisničkih upita za fleet management bot. "
    "Odgovaraš ISKLJUČIVO u JSON formatu sa dva polja: kind (string) i "
    "confidence (0.0-1.0). Ne dodaj objašnjenja izvan JSON-a."
)


@dataclass(frozen=True)
class IntentType:
    kind: str          # one of KIND_*
    confidence: float
    reasoning: str = ""


class IntentTypeClassifier:
    """4-way classifier with safe fallback on uncertainty.

    Pass 4 USER concern: 'vozač nikad ne čeka na osnovne info' —
    if the LLM is uncertain, default to KIND_QUESTION_ABOUT_SELF so
    the L2b anchor path runs immediately.
    """

    def __init__(self, llm_client, deployment_name: str):
        self._client = llm_client
        self._deployment = deployment_name

    async def classify(self, query: str) -> IntentType:
        if not query or not query.strip():
            return IntentType(kind=KIND_OTHER, confidence=0.0)

        try:
            response = await self._client.chat.completions.create(
                model=self._deployment,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": _build_prompt(query)},
                ],
                temperature=0,
                max_tokens=80,
                response_format={"type": "json_object"},
            )
            raw = response.choices[0].message.content or ""
        except Exception as e:  # noqa: BLE001
            logger.warning("intent type LLM error (safe fallback): %s", e)
            return IntentType(
                kind=KIND_QUESTION_ABOUT_SELF, confidence=0.0,
                reasoning=f"llm_error:{type(e).__name__}",
            )

        result = _parse(raw)

        # Safe-fallback if low confidence — vozač uvijek dobije put
        # do osnovnih info kroz L2b.
        if result.confidence < SAFE_FALLBACK_CONFIDENCE:
            return IntentType(
                kind=KIND_QUESTION_ABOUT_SELF,
                confidence=result.confidence,
                reasoning=f"low_confidence_fallback (was {result.kind})",
            )

        return result


def _build_prompt(query: str) -> str:
    return (
        "Klasificiraj sljedeću korisničku poruku u JEDNU od ovih 4 kategorija:\n\n"
        "- question_about_self: korisnik PITA za podatke o sebi ili svom "
        "vozilu (npr. 'koji mi je auto', 'kolika mi je km', 'kad mi "
        "ističe rega', 'koji je moj PIN').\n"
        "- action_or_complaint: korisnik PRIJAVLJUJE problem, kvar, "
        "štetu, ili traži da se nešto napravi (npr. 'auto mi trza', "
        "'puklo mi je staklo', 'izgubio sam karticu', 'fali mi PIN').\n"
        "- flow_request: korisnik traži da pokrene MULTI-STEP postupak "
        "(npr. 'rezerviraj auto', 'upiši km', 'prijavi kvar').\n"
        "- other: sve ostalo — manager analitika, opći upiti, nejasno.\n\n"
        "VAŽNO: razlikuj 'pitanje o vozilu' od 'žalbe na vozilo'. "
        "'Moj auto' u rečenici ne znači automatski question_about_self.\n\n"
        f'Korisnikov upit: """\n{query}\n"""\n\n'
        'Odgovori SAMO JSON-om: {"kind": "...", "confidence": 0.95}'
    )


def _parse(raw: str) -> IntentType:
    txt = (raw or "").strip()
    if txt.startswith("```"):
        txt = txt.strip("`")
        if txt.lower().startswith("json"):
            txt = txt[4:].lstrip()
    try:
        data = json.loads(txt)
    except json.JSONDecodeError:
        return IntentType(
            kind=KIND_QUESTION_ABOUT_SELF, confidence=0.0,
            reasoning=f"json_parse_failed: {txt[:80]}",
        )

    kind = data.get("kind") or KIND_OTHER
    valid_kinds = {
        KIND_QUESTION_ABOUT_SELF, KIND_ACTION_OR_COMPLAINT,
        KIND_FLOW_REQUEST, KIND_OTHER,
    }
    if kind not in valid_kinds:
        kind = KIND_OTHER

    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    return IntentType(kind=kind, confidence=confidence)
