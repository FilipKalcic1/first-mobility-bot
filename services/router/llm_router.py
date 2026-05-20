"""LLM router (Phase 2): anchor top-50 → gpt-4o-mini tool-calling → RouterResult.

One LLM call replaces the V2 anchor+judge two-step. The LLM both picks
the tool AND extracts parameters in a single structured tool_calls
response — params are validated against the registry schema by the
OpenAI SDK, eliminating a separate slot-filling pass.

Pipeline:
1. AnchorIndex.top_k(query, k=50) → 50 candidate tool_ids by max-cosine
2. ToolSchemaBuilder.build_for_tools(50) → OpenAI tools=[...] schemas
3. chat.completions.create(model=gpt-4o-mini, tools=[...], tool_choice="auto")
4. Parse message.tool_calls[0] → operation_id + JSON-decoded arguments
5. Validate: operation_id in top-50, params keys match the schema

Failure modes — return RouterResult with no tool_id:
- No anchor candidates (empty registry, or query embedding failed)
- LLM refused to call any tool (message.tool_calls is empty)
- LLM hallucinated a tool not in the top-50 — log as error, return alternatives
- JSON args un-parseable — log, return tool but with empty params
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from dataclasses import dataclass, field
from typing import Optional

from services.router.anchor_index import AnchorIndex, EmbedFn
from services.router.tool_schema_builder import ToolSchemaBuilder

logger = logging.getLogger(__name__)


# Sensible defaults — overridable via constructor kwargs.
TOP_K_RETRIEVAL = 50
ROUTER_TEMPERATURE = 0.0
ROUTER_MAX_TOKENS = 600  # enough for one tool_call with up to ~20 args
# ROUTE-1 (Filip 2026-05-20): per-call timeout. The worker wraps the whole
# request in asyncio.wait_for(90s), but without a per-call timeout a single
# slow Azure call would consume the entire budget as ONE hang — no retry.
# 30s per call lets the retry loop (3 attempts) fire within the 90s envelope
# on a transient stall (timeout → APITimeoutError → retryable).
ROUTER_CALL_TIMEOUT_S = 30.0


@dataclass
class RouterResult:
    """Outcome of a single route() call.

    Always returned (never raises); engine inspects fields to decide next
    layer (L5 clarify / L6 mutation gate / L7 execute).
    """
    tool_id: Optional[str] = None
    params: dict = field(default_factory=dict)
    confidence: float = 0.0
    rationale: str = ""
    alternatives: list[dict] = field(default_factory=list)  # [{tool_id, score}]
    missing_required: list[str] = field(default_factory=list)
    error: Optional[str] = None
    # Diagnostic — what anchor retrieval surfaced
    top_candidates: list[tuple[str, float]] = field(default_factory=list)
    anchor_score: float = 0.0  # cosine of picked tool's best anchor


class LLMRouter:
    """L3 router. Stateless except for the embedding-cached AnchorIndex."""

    def __init__(
        self,
        *,
        anchor_index: AnchorIndex,
        schema_builder: ToolSchemaBuilder,
        registry: dict,
        tkb: dict,
        llm_client,
        embed_fn: EmbedFn,
        deployment_name: str,
        top_k_retrieval: int = TOP_K_RETRIEVAL,
    ):
        self._anchor_index = anchor_index
        self._schema_builder = schema_builder
        self._registry = registry
        self._tkb = tkb
        self._llm = llm_client
        self._embed_fn = embed_fn
        self._deployment = deployment_name
        self._top_k = max(1, int(top_k_retrieval))
        self._tools_by_id: dict[str, dict] = {
            t["operation_id"]: t for t in registry.get("tools", [])
        }

    async def initialize(self) -> None:
        """Build anchor index. Idempotent."""
        await self._anchor_index.build(self._embed_fn)

    async def route(
        self,
        query: str,
        identity_summary: str = "",
        conversation_history: Optional[list[dict]] = None,
        tool_filter: Optional[frozenset[str]] = None,
    ) -> RouterResult:
        """Route one Croatian query to a tool + params. Never raises.

        ``tool_filter`` — when provided (Phase E persona scoping), limits
        anchor retrieval AND candidate construction to the named subset.
        See services/router/catalog_scoper.py for how this is computed.
        """
        if not query or not query.strip():
            return RouterResult(error="empty_query")

        # Stage A — anchor retrieval
        try:
            top = await self._anchor_index.top_k(
                query, self._embed_fn, k=self._top_k,
                allowed_tool_ids=tool_filter,
            )
        except Exception as e:  # noqa: BLE001 — anchor failure is recoverable
            logger.exception("anchor retrieval failed: %s", e)
            return RouterResult(error=f"anchor_error:{type(e).__name__}")

        if not top:
            return RouterResult(error="no_candidates")

        candidate_ids = {tid for tid, _ in top}
        # Safety net: if filter is set, drop any leaked candidates (defensive —
        # AnchorIndex already honored the filter, but registry/index drift could
        # leak). This is the second-line of persona enforcement.
        if tool_filter is not None:
            candidate_tools = [
                self._tools_by_id[tid] for tid, _ in top
                if tid in self._tools_by_id and tid in tool_filter
            ]
        else:
            candidate_tools = [
                self._tools_by_id[tid] for tid, _ in top if tid in self._tools_by_id
            ]
        if not candidate_tools:
            return RouterResult(
                error="candidates_missing_from_registry",
                top_candidates=top,
            )

        # Stage B — build OpenAI tool schemas for top-K
        tools_param = self._schema_builder.build_for_tools(
            candidate_tools, self._tkb,
        )
        if not tools_param:
            return RouterResult(
                error="schema_build_empty",
                top_candidates=top,
            )

        # Stage C — LLM tool-calling
        system = (
            "Ti si router za Croatian fleet-management WhatsApp bot. "
            "Korisnikov upit ide kroz tebe i ti odabireš JEDAN backend "
            "endpoint koji najbolje rješava upit. Endpoint pozivaš preko "
            "tool-calling — odabri tool i popuni argumente. "
            "PRAVILA: "
            "1) Odaberi TOČNO JEDAN tool od ponuđenih. NE izmišljaj tool koji nije u listi. "
            "2) Argumenti: popuni samo one koje korisnik eksplicitno spominje. "
            "Brojevi (npr. 'rezervacija 123' → id=123). Datumi: ISO format. "
            "Filtere koristi samo ako korisnik filtrira ('za vozilo DA053F'). "
            "3) Razlikuj kardinalnost: 'sve vozila' = LIST tool; 'vozilo 123' = BY_ID tool; "
            "'ukupno' = AGGREGATE. Pogledaj sufikse u tool nazivima. "
            "4) Ako NIJEDAN tool ne pristaje, NE pozivaj nijedan tool — odgovori "
            'kratkom tekstom "Ne razumijem zahtjev." '
            "5) Akcija mora odgovarati: GET za čitanje, POST za kreiranje, "
            "PUT/PATCH za promjenu, DELETE za brisanje."
        )

        identity_block = (
            f"Identity korisnika: {identity_summary}"
            if identity_summary else "Identity korisnika: (unknown)"
        )

        history_block = ""
        if conversation_history:
            recent = conversation_history[-3:]
            hist_lines = []
            for turn in recent:
                if isinstance(turn, dict):
                    u = turn.get("user") or turn.get("query") or ""
                    b = turn.get("bot") or turn.get("response") or ""
                    if u:
                        hist_lines.append(f"User: {u[:120]}")
                    if b:
                        hist_lines.append(f"Bot:  {b[:120]}")
            if hist_lines:
                history_block = (
                    "Nedavni razgovor (za kontekst follow-up pitanja):\n"
                    + "\n".join(hist_lines)
                )

        user_prompt_parts = [identity_block]
        if history_block:
            user_prompt_parts.append(history_block)
        user_prompt_parts.append(f'Korisnikov upit:\n"""\n{query}\n"""')
        user_prompt = "\n\n".join(user_prompt_parts)

        # Retry with exponential backoff on 429 / 5xx. Three attempts max:
        # 0.5-1.5s, 1.5-2.5s, 3-5s jittered. Total worst case ~9s. After
        # that we give up and return llm_error so engine surfaces fallback.
        response = None
        last_err: Optional[Exception] = None
        for attempt in range(3):
            try:
                response = await self._llm.chat.completions.create(
                    model=self._deployment,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user_prompt},
                    ],
                    tools=tools_param,
                    tool_choice="auto",
                    temperature=ROUTER_TEMPERATURE,
                    max_tokens=ROUTER_MAX_TOKENS,
                    timeout=ROUTER_CALL_TIMEOUT_S,
                )
                break
            except Exception as e:  # noqa: BLE001
                last_err = e
                kind = type(e).__name__
                status_code = getattr(e, "status_code", None)
                is_5xx = isinstance(status_code, int) and 500 <= status_code < 600
                retryable = (
                    kind in ("RateLimitError", "APITimeoutError", "APIConnectionError")
                    or "429" in str(e)
                    or is_5xx
                )
                if not retryable or attempt == 2:
                    logger.warning(
                        "LLM router call failed (attempt %d, %s): %s",
                        attempt + 1, kind, str(e)[:200],
                    )
                    return RouterResult(
                        error=f"llm_error:{kind}",
                        top_candidates=top,
                    )
                # 429 windows on Azure are often ~60s; backoff aggressively.
                sleep_s = (5.0 + attempt * 10.0) + random.uniform(0, 3.0)
                logger.info(
                    "LLM router 429/5xx attempt=%d sleep=%.1fs",
                    attempt + 1, sleep_s,
                )
                await asyncio.sleep(sleep_s)

        if response is None:
            return RouterResult(
                error=f"llm_error:{type(last_err).__name__ if last_err else 'unknown'}",
                top_candidates=top,
            )

        msg = response.choices[0].message
        tool_calls = getattr(msg, "tool_calls", None) or []

        if not tool_calls:
            # LLM refused to call any tool — usually means no match.
            content = (getattr(msg, "content", None) or "").strip()
            return RouterResult(
                error="no_tool_call",
                rationale=content[:300],
                top_candidates=top,
                alternatives=[
                    {"tool_id": tid, "score": round(s, 3)} for tid, s in top[:5]
                ],
            )

        call = tool_calls[0]
        schema_name = call.function.name
        operation_id = self._schema_builder.operation_id_for(schema_name)

        if not operation_id or operation_id not in candidate_ids:
            return RouterResult(
                error=f"hallucinated_tool:{schema_name}",
                top_candidates=top,
                alternatives=[
                    {"tool_id": tid, "score": round(s, 3)} for tid, s in top[:5]
                ],
            )

        # Parse arguments
        raw_args = call.function.arguments or "{}"
        try:
            args = json.loads(raw_args)
            if not isinstance(args, dict):
                args = {}
        except json.JSONDecodeError:
            args = {}
            logger.warning(
                "router: tool=%s arguments not valid JSON: %s",
                operation_id, raw_args[:200],
            )

        # Validate required params
        tool = self._tools_by_id.get(operation_id, {})
        required = list(tool.get("required_params") or [])
        for pname, pdef in (tool.get("parameters") or {}).items():
            if isinstance(pdef, dict) and pdef.get("required"):
                if pname not in required:
                    required.append(pname)
        # Filter required to user_input only (auto-injected ones don't count)
        user_input_required = [
            p for p in required
            if (tool.get("parameters", {}).get(p, {}) or {}).get(
                "dependency_source", "user_input"
            ) == "user_input"
        ]
        missing = [p for p in user_input_required if p not in args]

        # Anchor score for picked tool
        anchor_score = next((s for tid, s in top if tid == operation_id), 0.0)

        # Confidence heuristic: combination of anchor score + presence of params.
        # Strong anchor (>0.8) + all required filled = high confidence (0.9).
        # Weak anchor (<0.5) or missing required = lower (≤0.6).
        conf = 0.6 * max(0.0, min(1.0, (anchor_score + 1) / 2))  # [-1,1] → [0,1]
        if not missing:
            conf += 0.3
        if anchor_score > 0.75:
            conf += 0.1
        conf = max(0.0, min(1.0, conf))

        return RouterResult(
            tool_id=operation_id,
            params=args,
            confidence=conf,
            rationale=(getattr(msg, "content", None) or "").strip()[:300],
            alternatives=[
                {"tool_id": tid, "score": round(s, 3)}
                for tid, s in top[:5]
                if tid != operation_id
            ][:3],
            missing_required=missing,
            top_candidates=top,
            anchor_score=anchor_score,
        )
