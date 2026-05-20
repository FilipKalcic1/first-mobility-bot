"""LLM-generated Croatian labels for tool parameters.

Replaces the curated `PARAM_LABELS` dict (max ~33 entries, 53.6% coverage)
with on-demand LLM labeling that works for ANY param name in any of the
950 tools. Filip 2026-05-17 rejected the curated dict as "glup koncept"
that limits the bot to manually-tracked params.

Three-tier resolution (fastest first):
  1. Pre-generated dict (`config/param_labels_hr.json`) — zero cost. Built
     offline by `scripts/generate_param_labels.py` for top-N tool×param
     combinations.
  2. Redis cache (TTL 24h) — survives across processes. ~5ms hit.
  3. LLM call — ~300ms + tiny token cost. Result cached in Redis.

All three tiers fail silently to `None`. Caller (`param_ui.render_*`)
falls back to `humanize_param_name()` if label is None.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = (
    "Ti generiraš kratku hrvatsku oznaku (1-4 riječi) za API parametar koji "
    "WhatsApp bot pita korisnika. Vrati SAMO JSON: {\"label\": \"...\"} bez "
    "ničega drugog. Primjeri: "
    "param=Mileage tool=upis km → {\"label\":\"broj kilometara\"}; "
    "param=id tool=otkazivanje rezervacije → {\"label\":\"ID rezervacije\"}; "
    "param=Notes tool=prijava kvara → {\"label\":\"napomenu\"}. "
    "Label mora biti prirodna hrvatska fraza u akuzativu ako je objekt "
    "(\"Trebam {label}.\" mora čitati prirodno)."
)


_CACHE_TTL_SECONDS = 86400  # 24h
_REDIS_PREFIX = "param_label:"


def _redis_key(tool_id: str, param_name: str) -> str:
    return f"{_REDIS_PREFIX}{tool_id}::{param_name}"


def _preload_key(tool_id: str, param_name: str) -> str:
    """Key shape used by the pre-generated JSON file."""
    return f"{tool_id}::{param_name}"


class ParamLabeler:
    """Croatian label resolver. Mirror failsafe pattern of
    `OptionalParamExtractor` and `ApiErrorTranslator`."""

    def __init__(
        self,
        llm_client,
        deployment_name: str,
        redis_client=None,
        preloaded: Optional[dict] = None,
    ):
        self._llm = llm_client
        self._deployment = deployment_name
        self._redis = redis_client
        # tool_id::param_name → Croatian label (loaded once at factory time
        # from config/param_labels_hr.json). Empty dict = no preload.
        self._preloaded: dict = preloaded or {}

    async def label_for(
        self,
        param_name: str,
        param_def: Optional[dict] = None,
        tool_id: str = "",
        tool_intent: str = "",
    ) -> Optional[str]:
        """Resolve Croatian label for one tool's parameter.

        Returns: str (Croatian label) or None on miss. Caller falls back
        to `humanize_param_name()`.
        """
        if not param_name:
            return None

        # 1. Pre-generated dict (zero cost)
        pkey = _preload_key(tool_id, param_name)
        cached = self._preloaded.get(pkey)
        if cached:
            return cached
        # Try also without tool_id (some labels are tool-agnostic)
        cached = self._preloaded.get(f"::{param_name}")
        if cached:
            return cached

        # 2. Redis cache
        cached = await self._get_cached(tool_id, param_name)
        if cached:
            return cached

        # 3. LLM call
        label = await self._llm_generate(
            param_name=param_name,
            param_def=param_def or {},
            tool_intent=tool_intent,
        )
        if label:
            # Best-effort cache for next time
            await self._set_cached(tool_id, param_name, label)
        return label

    async def _get_cached(
        self, tool_id: str, param_name: str,
    ) -> Optional[str]:
        if self._redis is None:
            return None
        try:
            raw = await self._redis.get(_redis_key(tool_id, param_name))
            if not raw:
                return None
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            return raw
        except Exception as e:  # noqa: BLE001
            logger.debug("param_labeler cache get failed: %s", e)
            return None

    async def _set_cached(
        self, tool_id: str, param_name: str, label: str,
    ) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.setex(
                _redis_key(tool_id, param_name),
                _CACHE_TTL_SECONDS,
                label,
            )
        except Exception as e:  # noqa: BLE001
            logger.debug("param_labeler cache set failed: %s", e)

    async def _llm_generate(
        self,
        param_name: str,
        param_def: dict,
        tool_intent: str,
    ) -> Optional[str]:
        """Single LLM call. Never raises — returns None on any failure."""
        prompt_parts = [f"Parametar: {param_name}"]
        ptype = (param_def.get("param_type") or "").lower()
        if ptype:
            prompt_parts.append(f"Tip: {ptype}")
        desc = (param_def.get("description") or "").strip()
        if desc and len(desc) < 200:
            prompt_parts.append(f"API opis: {desc}")
        if tool_intent:
            prompt_parts.append(f"Kontekst alata: {tool_intent[:160]}")
        user_msg = "\n".join(prompt_parts)

        try:
            response = await self._llm.chat.completions.create(
                model=self._deployment,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0,
                max_tokens=40,
                response_format={"type": "json_object"},
            )
        except Exception as e:  # noqa: BLE001 — silent fallback
            logger.warning("param_labeler LLM error: %s", e)
            return None

        return _parse_label_response(response)


def _parse_label_response(response) -> Optional[str]:
    """Extract 'label' field from LLM JSON response. None on any
    malformation."""
    try:
        content = response.choices[0].message.content or ""
    except (AttributeError, IndexError, TypeError):
        return None
    content = content.strip()
    if not content:
        return None
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    label = data.get("label")
    if not isinstance(label, str):
        return None
    label = label.strip()
    if not label:
        return None
    # Hard cap for runaway LLM
    return label[:80]
