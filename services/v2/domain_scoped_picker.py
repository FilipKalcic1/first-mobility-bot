"""Domain-Scoped Tool Picker — Stage 2 of hierarchical V3 routing.

After DomainPicker (Stage 1) confirms a domain, this picker chooses a SPECIFIC
TOOL from the (typically 20-80) tools in that domain.

Key design decisions:
- Receives ONLY tools from the chosen domain — small candidate set, manageable
  for gpt-4o-mini.
- Includes RICH per-tool descriptions when available (config/rich_tool_docs.json).
  Falls back to operation_id parsing for tools without rich docs.
- Returns tool_id + confidence + missing_info + clarify_question.
- Mutation detection (POST/PUT/PATCH/DELETE) is mechanical from method.

Empirical projection: with 20-80 tools (vs 950) and rich descriptions,
gpt-4o-mini achieves 90%+ accuracy within domain (vs ~25% strict on 950 flat).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from services.utils.llm_json import parse_llm_json

logger = logging.getLogger(__name__)

DEFAULT_RICH_DOCS_PATH = Path(__file__).resolve().parents[2] / "config" / "rich_tool_docs.json"
DEFAULT_TKB_PATH = Path(__file__).resolve().parents[2] / "config" / "tool_knowledge_base.json"


@dataclass(frozen=True)
class ScopedToolHit:
    tool_id: str
    method: str
    confidence: float
    field_hint: Optional[str] = None
    reasoning: str = ""


@dataclass(frozen=True)
class ScopedDecision:
    """Output of Stage 2."""
    top_picks: list[ScopedToolHit] = field(default_factory=list)
    needs_clarify: bool = False
    clarify_question: Optional[str] = None
    clarify_options: list[str] = field(default_factory=list)
    missing_info: list[str] = field(default_factory=list)
    is_mutating: bool = False
    error: Optional[str] = None

    @property
    def top_pick(self) -> Optional[ScopedToolHit]:
        return self.top_picks[0] if self.top_picks else None

    @property
    def has_high_confidence(self) -> bool:
        return bool(self.top_picks) and self.top_picks[0].confidence >= 0.85


def _extract_method(tool_id: str) -> str:
    m = re.match(r"^(get|post|put|patch|delete)_", tool_id)
    return m.group(1).upper() if m else "GET"


def _is_mutating(tool_id: str) -> bool:
    return _extract_method(tool_id) in {"POST", "PUT", "PATCH", "DELETE"}


def _extract_modifier(tool_id: str) -> str:
    """Compact human-readable modifier hint for the LLM prompt."""
    if "documents_documentId_thumb" in tool_id: return "thumbnail dokumenta"
    if "documents_documentId_SetAsDefault" in tool_id: return "postavi kao primarni dokument"
    if "documents_documentId" in tool_id: return "specifičan dokument by ID"
    if "id_documents" in tool_id: return "lista dokumenata entiteta"
    if "multipatch" in tool_id: return "bulk update preko filtera"
    if "DeleteByCriteria" in tool_id: return "bulk delete preko filtera"
    if re.search(r"_(Agg|GroupBy|ProjectTo)", tool_id): return "agregacija/grupiranje"
    if "metadata" in tool_id: return "metadata sheme"
    if re.match(r"^.*_id$", tool_id): return "by ID (jedan zapis)"
    return "lista / standardna varijanta"


class DomainScopedToolPicker:
    """Stage 2 of hierarchical routing.

    Constructor takes the registry. `pick(query, domain_id, ...)` returns
    ScopedDecision after one LLM call over the domain's tool list.
    """

    def __init__(
        self,
        llm_client,
        deployment_name: str,
        registry,
        domain_picker,
        rich_docs_path: Optional[Path] = None,
        tkb_path: Optional[Path] = None,
        max_retries: int = 2,
    ):
        self._llm = llm_client
        self._deployment = deployment_name
        self._registry = registry
        self._domain_picker = domain_picker
        self._rich_docs_path = rich_docs_path or DEFAULT_RICH_DOCS_PATH
        self._tkb_path = tkb_path or DEFAULT_TKB_PATH
        self._max_retries = max_retries
        self._rich_docs: dict[str, dict] = {}
        # Tool Knowledge Base: rich, structured per-tool semantics —
        # intent_summary, returns, use_when, do_not_use_when, examples.
        # Empirically validated as the missing piece — gpt-4o-mini cannot
        # discriminate from "lista / standardna varijanta" hints alone.
        self._tkb: dict[str, dict] = {}
        self._loaded = False

    def load(self) -> None:
        if self._loaded:
            return
        if self._rich_docs_path.exists():
            try:
                with self._rich_docs_path.open(encoding="utf-8") as f:
                    self._rich_docs = json.load(f).get("tools", {})
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("rich_tool_docs load failed: %s", e)
        if self._tkb_path.exists():
            try:
                with self._tkb_path.open(encoding="utf-8") as f:
                    self._tkb = json.load(f).get("tools", {})
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("tool_knowledge_base load failed: %s", e)
        self._loaded = True
        logger.info(
            "DomainScopedToolPicker loaded: %d rich docs, %d TKB entries",
            len(self._rich_docs), len(self._tkb),
        )

    def _tools_in_domain(
        self,
        domain_id: str,
    ) -> list[dict]:
        """Return list of tool descriptors for tools whose entity is mapped
        to this domain. Each descriptor: {tool_id, method, modifier_hint,
        rich_description (if available)}.

        NOTE: This module is part of V3 dormant path (V2_USE_V3_ROUTER=0
        in production).
        """
        entities = set(self._domain_picker.get_entities_for_domain(domain_id))
        if not entities:
            return []

        tools = []
        for t in (self._registry.tools if hasattr(self._registry, "tools") else []):
            tool_id = (
                t.tool_id if hasattr(t, "tool_id")
                else getattr(t, "operation_id", None) or t.get("operation_id")
            )
            if not tool_id:
                continue
            m = re.match(r"^(get|post|put|patch|delete)_([A-Z][A-Za-z0-9]+)", tool_id)
            if not m or m.group(2) not in entities:
                continue

            rich = self._rich_docs.get(tool_id, {})
            method = m.group(1).upper()
            tools.append({
                "tool_id": tool_id,
                "method": method,
                "entity": m.group(2),
                "modifier_hint": _extract_modifier(tool_id),
                "rich_description": rich.get("description", ""),
                "when_to_use": rich.get("when_to_use", ""),
                "when_NOT_to_use": rich.get("when_NOT_to_use", ""),
            })
        return tools

    def tkb_entry_for(self, tool_id: str) -> Optional[dict]:
        """Public accessor — returns TKB entry for a tool_id or None."""
        if not self._loaded:
            self.load()
        return self._tkb.get(tool_id)

    def _format_tkb_block(self, tool_id: str, method: str) -> Optional[str]:
        """Render a TKB entry as a compact, filter-safe prompt block.

        Format intentionally avoids quoted user phrases (Azure content
        filter trigger). Uses Croatian colons + abstract description.
        Returns None if tool not in TKB; caller should fall back to thin
        line.
        """
        entry = self._tkb.get(tool_id)
        if not entry:
            return None

        lines = [f"  • {tool_id} [{method}]"]
        if entry.get("intent_summary"):
            lines.append(f"      INTENT: {entry['intent_summary'][:140]}")
        # Returns: comma-list of field names + 1-line each
        returns = entry.get("returns") or {}
        if returns:
            field_summary = ", ".join(list(returns.keys())[:6])
            lines.append(f"      VRAĆA: {field_summary}")
        # Use cases — 2 most relevant short lines
        for use in (entry.get("use_when") or [])[:2]:
            lines.append(f"      KORISTI ZA: {use[:90]}")
        # Disambiguation — top 1 most relevant comparison
        for d in (entry.get("do_not_use_when") or [])[:1]:
            if isinstance(d, dict):
                alt = d.get("alt_tool", "")
                why = d.get("razlog", "")[:100]
                lines.append(f"      NIJE KAD: {alt} — {why}")
        return "\n".join(lines)

    def _build_prompt(
        self, query: str, domain_id: str,
        recent_turns: Optional[list[dict]] = None,
    ) -> tuple[str, str]:
        domain_meta = self._domain_picker.get_domain_by_id(domain_id) or {}
        tools = self._tools_in_domain(domain_id)

        # Compact framing — heavier prompts trigger Azure content filter
        # (root cause + fix archived in git history 2026-05-08). Keep
        # instruction terse, let TKB blocks carry the semantics.
        system_lines = [
            f"Ti si Stage-2 router u domeni: {domain_id}.",
            f"Imaš {len(tools)} alata. Odaberi PRAVI za upit korisnika.",
            "",
            "ALATI:",
        ]

        # Selective TKB load: only TKB-enriched tools get rich blocks.
        # Tools without TKB get a thin one-liner with rich_doc fallback.
        # Empirically: for 5-15 tools per prompt, total stays ~1.5-3K
        # tokens — well under Azure filter trigger threshold.
        tkb_count = 0
        for t in tools[:80]:
            block = self._format_tkb_block(t["tool_id"], t["method"])
            if block:
                system_lines.append(block)
                tkb_count += 1
            else:
                # Thin fallback for tools without TKB
                line = f"  • {t['tool_id']} [{t['method']}] — {t['modifier_hint']}"
                if t["rich_description"]:
                    line += f" | {t['rich_description'][:120]}"
                system_lines.append(line)
                if t["when_to_use"]:
                    system_lines.append(f"      KORISTI: {t['when_to_use'][:100]}")

        system_lines.extend([
            "",
            "PRAVILA:",
            "1. Vrati BAREM 1 pick. Match query intent na examples / "
            "intent_summary / returns iz TKB.",
            "2. Confidence: 0.95+ ako jednoznačno; 0.6-0.85 ako 2-3 alata "
            "pristaju; <0.5 ako ništa ne pristaje.",
            "3. Ako fali info (datumi, ID) — needs_clarify=true.",
            "4. NIKAD ne izmišljaj tool_id van liste.",
            "5. is_mutating=true ako method nije GET.",
            "",
            "OUTPUT JSON:",
            '{"top_picks":[{"tool_id":"...","confidence":0.0,"field_hint":"...",'
            '"reasoning":"..."}],"needs_clarify":false,"clarify_question":null,'
            '"clarify_options":[],"missing_info":[],"is_mutating":false}',
        ])

        history_block = ""
        if recent_turns:
            hist_lines = ["RAZGOVOR (zadnje par poruka, za context):"]
            for t in recent_turns[-3:]:
                u = str(t.get("user") or t.get("query") or "")[:120]
                b = str(t.get("bot") or t.get("bot_action") or t.get("response") or "")[:120]
                if u:
                    hist_lines.append(f"  USER: {u}")
                if b:
                    hist_lines.append(f"  BOT: {b}")
            history_block = "\n".join(hist_lines) + "\n\n"

        user = (
            f"{history_block}"
            f"USER QUERY (najnovija poruka): {query}\n\n"
            "Vrati JSON. Ne dodavaj prose."
        )

        return "\n".join(system_lines), user

    async def pick(
        self,
        query: str,
        domain_id: str,
        recent_turns: Optional[list[dict]] = None,
    ) -> ScopedDecision:
        if not query or not query.strip():
            return ScopedDecision(error="empty_query")
        if not domain_id:
            return ScopedDecision(error="missing_domain")
        if not self._loaded:
            self.load()

        # Shortcut: if the domain has only 1 tool (e.g. vehicle_info → MasterData),
        # there is nothing to disambiguate — return it directly with high confidence.
        # This was empirically critical: vehicle_info has only get_MasterData,
        # but the LLM was returning empty top_picks because the prompt asked for
        # "top 2 picks" and the model couldn't satisfy that with 1-tool catalog.
        tools = self._tools_in_domain(domain_id)
        if len(tools) == 1:
            t = tools[0]
            return ScopedDecision(
                top_picks=[ScopedToolHit(
                    tool_id=t["tool_id"],
                    method=t["method"],
                    confidence=0.95,
                    reasoning=f"Only tool in domain {domain_id}; no disambiguation needed.",
                )],
                is_mutating=_is_mutating(t["tool_id"]),
            )

        system, user = self._build_prompt(query, domain_id, recent_turns)

        # Same Azure system-vs-user filter asymmetry as DomainPicker.
        # Pack everything into the user role.
        combined = f"{system}\n\n---\n\n{user}"
        last_err: Optional[str] = None
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._llm.chat.completions.create(
                    model=self._deployment,
                    messages=[
                        {"role": "user", "content": combined},
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
                        await asyncio.sleep(0.5 * (2 ** attempt))
                        continue
                    return ScopedDecision(error=last_err)
                return self._normalize(parsed)
            except Exception as e:  # noqa: BLE001
                # Same Azure transient pattern as DomainPicker — retry with
                # exponential backoff. See DomainPicker.pick() for rationale.
                err_name = type(e).__name__
                last_err = f"llm_error:{err_name}"
                logger.debug(
                    "ScopedPicker LLM call failed attempt=%d err=%s msg=%s",
                    attempt, err_name, str(e)[:160],
                )
                if attempt < self._max_retries:
                    await asyncio.sleep(0.5 * (2 ** attempt))
                    continue
                return ScopedDecision(error=last_err)
        return ScopedDecision(error=last_err or "exhausted_retries")

    def _normalize(self, parsed: dict) -> ScopedDecision:
        picks: list[ScopedToolHit] = []
        for p in parsed.get("top_picks", []) or []:
            if not isinstance(p, dict):
                continue
            tid = str(p.get("tool_id", "")).strip()
            if not tid:
                continue
            # Verify tool exists in registry — defense against LLM hallucination
            if hasattr(self._registry, "has_tool") and not self._registry.has_tool(tid):
                logger.warning("LLM hallucinated tool_id %s — dropping", tid)
                continue
            try:
                conf = float(p.get("confidence", 0.0))
            except (TypeError, ValueError):
                conf = 0.0
            picks.append(ScopedToolHit(
                tool_id=tid,
                method=_extract_method(tid),
                confidence=max(0.0, min(1.0, conf)),
                field_hint=p.get("field_hint") or None,
                reasoning=str(p.get("reasoning", "")),
            ))

        is_mutating = bool(parsed.get("is_mutating", False))
        # Defense-in-depth: also check method
        if picks and _is_mutating(picks[0].tool_id):
            is_mutating = True

        return ScopedDecision(
            top_picks=picks,
            needs_clarify=bool(parsed.get("needs_clarify", False)),
            clarify_question=parsed.get("clarify_question") or None,
            clarify_options=list(parsed.get("clarify_options") or []),
            missing_info=list(parsed.get("missing_info") or []),
            is_mutating=is_mutating,
            error=None,
        )
