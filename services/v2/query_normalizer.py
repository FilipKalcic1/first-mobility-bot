"""L1.5 Query Normalizer — pre-routing canonical rewrite for formal/manager queries.

EMPIRICAL STATUS (2026-05-06): UNVALIDATED — bench regression observed on
adversarial paraphrase corpus (-3pp on 276 adversarial: 7% → 4%). Hypothesis
that formal-style queries lift was FALSIFIED on synthetic-adversarial test
corpus where 88.5% of queries are ambiguous/broken-ground-truth (Phase 1
audit 2026-05-03). Normalizer's confident-guess on ambiguous queries
produces confidently-wrong routing.

Module retained as opt-in infrastructure — if Damir-export reveals real
formal-style production queries (manager-style "Trebam ažurirati X"), this
can be enabled per-deployment. NOT wired into production bootstrap.

To enable: explicitly pass `query_normalizer=QueryNormalizer(...)` to
V2Engine. Default `None` means engine bypasses this layer.



Real users mix two styles:
- Driver (natural): "kolika km", "obriši rezervaciju", "moje km zapise"
- Manager (formal): "Trebam deaktivirati poslovne jedinice koje su suvišne"

Stage 1 router was trained/prompted on natural style and underperforms on
formal. The Normalizer takes any input and returns:
  - canonical: short, topic-vocab-rich version of the query
  - style: detected (formal | natural | fragment)
  - intent_action: extracted CRUD verb (read|create|update|delete|aggregate)
  - intent_entity: extracted entity hint (cost_center, vehicle, mileage, ...)

Output is fed to V3 pickers in addition to the original. Pickers see both —
match either canonical OR original to TKB examples / use_when conditions.

Cost: +1 LLM call per query (~$0.0003 with gpt-4o-mini).
Latency: +400-600ms.

Skipped (passthrough) when:
- Query < 4 words (driver fragment, no normalization needed)
- Query starts with single-word command ("registracija", "tablica") — already canonical

CLAUDE.md alignment:
- §1.1 (determinizam > statistika): we still use LLM, but it's a cheap, focused
  rewrite over the full router. Failure mode is graceful — if normalizer
  errors, pass original through.
- §1.5 (real production data): normalizer logs original + canonical for
  active learning. Damir-export will reveal real distribution of formal vs
  natural and let us tune skip thresholds.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Optional

from services.utils.llm_json import parse_llm_json

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NormalizedQuery:
    """Output of L1.5 normalizer.

    `original` is always preserved; `canonical` is the rewritten form.
    Pickers should use canonical for intent matching, original for context.
    """
    original: str
    canonical: str
    style: str = "natural"  # natural | formal | fragment | unknown
    intent_action: Optional[str] = None
    intent_entity: Optional[str] = None
    intent_scope: Optional[str] = None  # one | many | aggregate | none
    skipped: bool = False
    reason: Optional[str] = None
    error: Optional[str] = None

    @property
    def both_for_routing(self) -> str:
        """Combined string for picker prompts. Canonical first; original
        second so LLM sees rewrite + raw user input."""
        if self.skipped or self.canonical == self.original:
            return self.original
        return f"{self.canonical}\n(original: {self.original})"


# Heuristic skip rules — cheap pre-LLM gate
def _should_skip(query: str) -> tuple[bool, str]:
    text = (query or "").strip()
    if not text:
        return True, "empty"
    word_count = len(text.split())
    if word_count <= 3:
        # Short driver fragment, already canonical
        return True, "short_fragment"
    # Single-word topic terms — already canonical
    SHORT_CANONICAL = {
        "registracija", "tablica", "kilometar", "kilometraza",
        "marka", "model", "auto", "moj", "moja",
        "rezervacija", "rezervacije", "prijava", "prijave",
        "casco", "osiguranje", "lizing", "sluzba",
    }
    if word_count <= 5 and any(w in text.lower() for w in SHORT_CANONICAL):
        return True, "topic_canonical_short"
    return False, "needs_normalization"


_NORMALIZER_PROMPT = """Si normalizator hrvatskih upita za WhatsApp bot za fleet management.

Korisnik ti šalje upit. Tvoj zadatak: prepiši ga u kratku CANONICAL formu koja jasno sadrži:
- TOPIC vocabulary (naziv stvari: km, registracija, rezervacija, cost center, trošak, vozilo, gorivo, prijave, ...)
- ACTION (čitaj / kreiraj / promijeni / obriši / agregat)

Cilj: upit koji vozač/manager u prirodnoj kratkoj formulaciji nikad ne bi rekao "deaktivirati poslovne jedinice koje su suvišne" — pretvori u "obriši cost centre, više njih (bulk)".

Output JSON:
{
  "canonical": "kratko, prirodno hrvatski s topic vocab",
  "style": "formal | natural | fragment",
  "intent_action": "read | create | update | delete | aggregate | unknown",
  "intent_entity": "cost_center | vehicle | mileage | reservation | case | person | trip | expense | document | permission | role | other",
  "intent_scope": "one | many | aggregate | none"
}

PRAVILA:
- Sve na hrvatskom
- Canonical < 60 znakova
- Sačuvaj specifične brojeve / imena (datumi, ID-evi, "DA053F", "Marka")
- Ako original već koristi topic vocab + action → canonical = original (smije biti identical)
- BEZ markdown, samo strict JSON

PRIMJERI:

Input: "Trebam deaktitivirati više poslovnih jedinica koje su suvišne u našem sustavu."
Output: {"canonical": "obriši više cost centara (bulk delete)", "style": "formal", "intent_action": "delete", "intent_entity": "cost_center", "intent_scope": "many"}

Input: "Možeš mi reći detalje o autu?"
Output: {"canonical": "podaci o mom vozilu (snapshot)", "style": "natural", "intent_action": "read", "intent_entity": "vehicle", "intent_scope": "one"}

Input: "kolika km"
Output: {"canonical": "kolika km", "style": "fragment", "intent_action": "read", "intent_entity": "mileage", "intent_scope": "one"}

Input: "Molim te da izbaciš sve zapise o pređenim udaljenostima koji ispunjavaju određene kriterije."
Output: {"canonical": "obriši km zapise filterom (bulk delete)", "style": "formal", "intent_action": "delete", "intent_entity": "mileage", "intent_scope": "many"}

Input: "saljem ti km 30000"
Output: {"canonical": "unesi 30000 km", "style": "natural", "intent_action": "create", "intent_entity": "mileage", "intent_scope": "one"}"""


class QueryNormalizer:
    """L1.5 LLM-driven query normalizer.

    Stateless — single async pick(query) returns NormalizedQuery.

    Constructor takes llm_client + deployment. Optional max_retries (default
    2 with exponential backoff matching V3 picker pattern).
    """

    def __init__(
        self,
        llm_client,
        deployment_name: str,
        max_retries: int = 2,
        skip_short: bool = True,
    ):
        self._llm = llm_client
        self._deployment = deployment_name
        self._max_retries = max_retries
        self._skip_short = skip_short

    async def normalize(self, query: str) -> NormalizedQuery:
        if not query or not query.strip():
            return NormalizedQuery(
                original=query or "",
                canonical=query or "",
                skipped=True,
                reason="empty",
            )

        if self._skip_short:
            skip, reason = _should_skip(query)
            if skip:
                return NormalizedQuery(
                    original=query,
                    canonical=query,
                    style="fragment" if reason == "short_fragment" else "natural",
                    skipped=True,
                    reason=reason,
                )

        last_err: Optional[str] = None
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._llm.chat.completions.create(
                    model=self._deployment,
                    messages=[
                        {"role": "user", "content": f"{_NORMALIZER_PROMPT}\n\n---\n\nInput: \"{query}\""}
                    ],
                    temperature=0.0,
                    max_tokens=200,
                    response_format={"type": "json_object"},
                )
                raw = response.choices[0].message.content or ""
                parsed = parse_llm_json(raw)
                if parsed is None:
                    last_err = "json_parse_failed"
                    if attempt < self._max_retries:
                        await asyncio.sleep(0.4 * (2 ** attempt))
                        continue
                    return NormalizedQuery(
                        original=query, canonical=query, error=last_err,
                    )
                canonical = str(parsed.get("canonical", "") or "").strip()
                if not canonical:
                    canonical = query  # fallback — never lose original
                return NormalizedQuery(
                    original=query,
                    canonical=canonical,
                    style=str(parsed.get("style", "natural")),
                    intent_action=parsed.get("intent_action"),
                    intent_entity=parsed.get("intent_entity"),
                    intent_scope=parsed.get("intent_scope"),
                )
            except Exception as e:  # noqa: BLE001
                err_name = type(e).__name__
                last_err = f"llm_error:{err_name}"
                logger.debug(
                    "QueryNormalizer LLM call failed attempt=%d err=%s msg=%s",
                    attempt, err_name, str(e)[:160],
                )
                if attempt < self._max_retries:
                    await asyncio.sleep(0.4 * (2 ** attempt))
                    continue
                return NormalizedQuery(
                    original=query, canonical=query, error=last_err,
                )
        return NormalizedQuery(
            original=query, canonical=query, error=last_err or "exhausted",
        )
