"""Unified LLM Responder — replaces V3 multi-stage routing with ONE LLM call.

Architecture pivot (2026-05-07): the pre-LLM mindset of "build a classifier
+ rerank + format pipeline because LLMs can't be trusted" is obsolete with
frontier models. The new shape:

    User query
      ↓
    L1 quick-path (regex, free) → done for ~40% driver traffic
      ↓ (else)
    L3 retrieve top-30 candidate tools via FAISS over anchor embeddings
      ↓
    UnifiedResponder — ONE LLM call given:
      - top-30 tool schemas (compact)
      - recent conversation turns (multi-turn context)
      - user identity context (persona, vehicle, tenant)
      - instruction: pick tool + fill params + format Croatian response
      ↓
    L6 mutation gate (existing, untouched)
      ↓
    L7 execute → directly emit LLM-rendered response

What this addresses (vs V3 DomainPicker → DomainScopedToolPicker → executor → formatter):
- LLM reasons about the query rather than pattern-matching domain hints
- Response formatting is native (no separate brittle formatter stage)
- Novel phrasings work because model understands intent
- Conversation context handled in-prompt naturally
- TKB schemas are descriptive, not over-engineered paraphrase lookup tables

Trade-offs (honest):
- Cost: ~$0.003-0.01/call vs ~$0.0006 V3 (gpt-4o-mini × 2). Affordable.
- Latency: 2-4s vs 1.5-2.5s V3. Acceptable.
- Predictability: less. Mitigate with reasoning trace logging.
- Hallucination: managed by tool_id whitelist + mutation gate + param schema validation.

Drop-in: opt-in via `V2_USE_UNIFIED_RESPONDER=1` env. When OFF, V3 path runs.
When ON, V3 path is bypassed entirely.
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
class UnifiedDecision:
    """Output of the unified LLM call."""
    tool_id: Optional[str] = None
    params: dict = field(default_factory=dict)
    needs_confirm: bool = False  # mutation that needs explicit Da/Ne
    response_text: Optional[str] = None  # Croatian response (read path) or None (mutation path before confirm)
    needs_clarify: bool = False  # ask user a question
    clarify_question: Optional[str] = None
    clarify_options: list[str] = field(default_factory=list)
    reasoning: str = ""
    confidence: float = 0.0
    error: Optional[str] = None

    @property
    def is_actionable(self) -> bool:
        return bool(self.tool_id) and not self.error


_SYSTEM_PROMPT_TEMPLATE = """Si glasovni asistent za fleet-management WhatsApp bot na hrvatskom jeziku (firma MobilityOne).

KORISNIK
{identity_block}

ZADATAK
Korisnik ti je poslao poruku. Tvoj zadatak je:
1. Razumjeti što traži (možda formalno, neformalno, kratko, fragmentirano)
2. Iz priloženih alata odabrati PRAVI alat (po INTENT-u, ne literalnom matchingu)
3. Ako fali parametar (ID, datum, broj) — pitaj korisnika konkretno pitanje
4. Ako je MUTACIJA (POST/PUT/PATCH/DELETE) — postaviti needs_confirm=true, response_text=null
5. Ako je READ (GET) — već u response_text napiši kratki, prirodni Croatian preview odgovora baziran na onom što alat tipično vraća. Bot će kasnije napraviti pravi API call i zamijeniti placeholder.

ALATI (top 30, po relevantnosti)
{tools_block}

KONTEXT RAZGOVORA
{history_block}

PRAVILA ODGOVARANJA
- Hrvatski jezik, neformalno-pristojno
- Bez markdown formatting (WhatsApp plain text)
- Kratko (1-3 rečenice tipično)
- Brojeve s razmacima (npr. "34 521 km", ne "34521km")
- Datume na hrvatskom (npr. "12. svibnja", ne "12/05")
- Mutacije: response_text=null, needs_confirm=true
- Reads: response_text = kratak natural Croatian preview ("Tvoja kilometraža: {{TotalKm}} km" — placeholderi u {{}} bot zamijeni)
- Ne haluciniraj tool_id koji nije u listi

OUTPUT (strict JSON, no prose)
{{
  "tool_id": "exact_id_iz_liste_alata_ili_null",
  "params": {{"param_name": "value", ...}},
  "needs_confirm": true|false,
  "response_text": "kratak HR preview ili null (za mutaciju)",
  "needs_clarify": true|false,
  "clarify_question": "konkretno pitanje na HR ili null",
  "clarify_options": ["opcija 1", "opcija 2"],
  "reasoning": "1 rečenica zašto si odabrao taj alat",
  "confidence": 0.0
}}"""


def _format_identity(identity_summary: dict) -> str:
    # SECURITY FIX: see tool_use_responder._format_identity for rationale.
    # Backend-sourced identity fields are sanitized before being embedded
    # in the system prompt to prevent prompt-injection via DB content.
    from services.v2.output_sanitizer import _sanitize_string

    def _safe(value) -> str:
        if value is None:
            return ""
        cleaned, _ = _sanitize_string(str(value))
        return cleaned.replace("\n", " ").replace("\r", " ").strip()

    parts = []
    fn = _safe(identity_summary.get("first_name"))
    if fn:
        parts.append(f"Ime: {fn}")
    vn = _safe(identity_summary.get("vehicle_name"))
    if vn:
        parts.append(f"Vozilo: {vn}")
    lm = identity_summary.get("last_mileage")
    if lm is not None:
        parts.append(f"Zadnja km: {lm}")
    tid = _safe(identity_summary.get("tenant_id"))
    if tid:
        parts.append(f"Tenant: {tid}")
    return "\n".join(parts) if parts else "(nije dostupno)"


def _format_tools(candidates: list[dict]) -> str:
    """Format top-30 candidates as compact LLM-readable list."""
    if not candidates:
        return "(nema kandidata)"
    lines = []
    for c in candidates[:30]:
        tool_id = c.get("tool_id", "?")
        method = c.get("method", "GET")
        intent = c.get("intent_summary") or c.get("description", "")[:120]
        returns = c.get("returns") or {}
        params = c.get("required_params") or []

        parts = [f"- {tool_id} [{method}]"]
        if intent:
            parts.append(f"  intent: {intent[:140]}")
        if returns:
            ret_summary = ", ".join(list(returns.keys())[:5])
            parts.append(f"  returns: {ret_summary}")
        if params:
            parts.append(f"  params: {', '.join(params[:6])}")
        lines.append("\n".join(parts))
    return "\n\n".join(lines)


def _format_history(recent_turns: list[dict]) -> str:
    if not recent_turns:
        return "(nema povijesti — ovo je prva poruka u sesiji)"
    lines = []
    for t in recent_turns[-3:]:
        u = str(t.get("user") or "")[:140]
        b = str(t.get("bot") or "")[:140]
        if u:
            lines.append(f"USER: {u}")
        if b:
            lines.append(f"BOT: {b}")
    return "\n".join(lines)


class UnifiedResponder:
    """Single LLM call — routing + param filling + response generation.

    Constructor takes llm_client + deployment + retriever (callable that
    given query returns top-30 candidates as list of dicts).

    `respond(query, identity_summary, recent_turns)` returns UnifiedDecision.
    """

    def __init__(
        self,
        llm_client,
        deployment_name: str,
        retriever,  # callable(query: str, k: int) -> list[dict]
        max_retries: int = 2,
    ):
        self._llm = llm_client
        self._deployment = deployment_name
        self._retriever = retriever
        self._max_retries = max_retries

    async def respond(
        self,
        query: str,
        identity_summary: Optional[dict] = None,
        recent_turns: Optional[list[dict]] = None,
        top_k: int = 30,
    ) -> UnifiedDecision:
        if not query or not query.strip():
            return UnifiedDecision(error="empty_query")

        # L3 — retrieve top-K candidate tools via FAISS over anchors
        try:
            persona = (identity_summary or {}).get("persona", "driver")
            candidates = await self._retriever(query, persona, top_k)
        except Exception as e:  # noqa: BLE001
            logger.warning("Retriever failed: %s", e)
            return UnifiedDecision(error=f"retriever_error:{type(e).__name__}")

        if not candidates:
            return UnifiedDecision(error="no_candidates")

        prompt = _SYSTEM_PROMPT_TEMPLATE.format(
            identity_block=_format_identity(identity_summary or {}),
            tools_block=_format_tools(candidates),
            history_block=_format_history(recent_turns or []),
        )
        user_msg = f"NAJNOVIJA PORUKA KORISNIKA:\n{query}\n\nVrati JSON, ne dodavaj prose."

        valid_tool_ids = {c.get("tool_id") for c in candidates}

        last_err: Optional[str] = None
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._llm.chat.completions.create(
                    model=self._deployment,
                    messages=[
                        {"role": "user", "content": prompt + "\n\n---\n\n" + user_msg},
                    ],
                    temperature=0.0,
                    max_tokens=600,
                    response_format={"type": "json_object"},
                )
                raw = response.choices[0].message.content or ""
                parsed = parse_llm_json(raw)
                if parsed is None:
                    last_err = "json_parse_failed"
                    if attempt < self._max_retries:
                        await asyncio.sleep(0.4 * (2 ** attempt))
                        continue
                    return UnifiedDecision(error=last_err)
                return self._normalize(parsed, valid_tool_ids)
            except Exception as e:  # noqa: BLE001
                err_name = type(e).__name__
                last_err = f"llm_error:{err_name}"
                logger.debug(
                    "UnifiedResponder LLM call failed attempt=%d err=%s msg=%s",
                    attempt, err_name, str(e)[:200],
                )
                if attempt < self._max_retries:
                    await asyncio.sleep(0.4 * (2 ** attempt))
                    continue
                return UnifiedDecision(error=last_err)
        return UnifiedDecision(error=last_err or "exhausted")

    def _normalize(self, parsed: dict, valid_tool_ids: set) -> UnifiedDecision:
        tid = str(parsed.get("tool_id") or "").strip() or None

        # Hallucination guard — drop tool_ids not in candidate set
        if tid is not None and tid not in valid_tool_ids:
            logger.warning("LLM hallucinated tool_id=%s; dropping", tid)
            return UnifiedDecision(
                tool_id=None,
                error="hallucinated_tool_id",
                reasoning=str(parsed.get("reasoning", "")),
            )

        try:
            conf = float(parsed.get("confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0

        params = parsed.get("params") or {}
        if not isinstance(params, dict):
            params = {}

        return UnifiedDecision(
            tool_id=tid,
            params=params,
            needs_confirm=bool(parsed.get("needs_confirm", False)),
            response_text=parsed.get("response_text"),
            needs_clarify=bool(parsed.get("needs_clarify", False)),
            clarify_question=parsed.get("clarify_question"),
            clarify_options=list(parsed.get("clarify_options") or []),
            reasoning=str(parsed.get("reasoning", "")),
            confidence=max(0.0, min(1.0, conf)),
            error=None,
        )
