"""LLM translator for API error responses → Croatian WhatsApp messages.

When the executor receives a 4xx response from MobilityOne, the bot used
to reply with a generic "Tehnički problem. Pokušaj ponovo." That left the
user with no idea why the action failed (missing field? wrong permission?
non-existent resource?).

This translator wraps a single LLM call that converts the raw API error
body + status code into a short Croatian WhatsApp message (≤200 chars)
that says WHAT happened and HOW to fix it.

Scope:
  - 4xx only (user-actionable). 5xx server errors stay generic — user
    can't fix them anyway.
  - Silent fallback: any LLM error / cache miss returns None, caller
    falls back to the generic message.
  - Optional Redis cache: identical (status, body) responses skip the
    LLM call. SETEX 1h.

Filip 2026-05-17 — random-pick discovery on post_Partners_id_documents.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = (
    "Ti si prevoditelj API grešaka za fleet management WhatsApp bot. "
    "Korisnik je pokušao izvršiti akciju i API je vratio grešku. "
    "Tvoj zadatak: napiši KRATKU hrvatsku poruku (max 200 znakova) koja "
    "kaže ŠTO je krenulo krivo i KAKO popraviti. Bez tehničkih izraza "
    "(ne spominji status code, JSON, endpoint, URL). Bez engleskog. "
    'Vrati SAMO JSON: {"message": "..."} bez ničega drugog.'
)


# Status codes worth translating (user-actionable). 5xx is server's
# problem, not user's — they get the generic "Tehnički problem" instead.
_TRANSLATABLE_STATUS = frozenset({400, 401, 403, 404, 409, 410, 422, 429})

_CACHE_TTL_SECONDS = 3600  # 1h — same error body usually means same root cause


def _cache_key(status_code: int, body_text: str, tool_id: str) -> str:
    """Stable cache key. Includes tool_id because the same 400 from
    different tools can mean different things ('id' missing vs 'name'
    missing)."""
    h = hashlib.sha1(
        f"{status_code}|{tool_id}|{body_text[:300]}".encode("utf-8"),
        usedforsecurity=False,
    ).hexdigest()[:16]
    return f"api_err_translate:{h}"


class ApiErrorTranslator:
    """Tanki LLM wrapper. Mirror pattern of OptionalParamExtractor."""

    def __init__(
        self,
        llm_client,
        deployment_name: str,
        redis_client=None,
    ):
        self._llm = llm_client
        self._deployment = deployment_name
        self._redis = redis_client

    async def translate(
        self,
        status_code: int,
        response_body: object,
        tool_id: str = "",
        tool_intent: str = "",
    ) -> Optional[str]:
        """Return Croatian explanation, or None on any failure.

        Args:
          status_code: HTTP status from API response.
          response_body: error payload (dict, str, or None).
          tool_id:    operation_id of the tool that failed (for cache key).
          tool_intent: intent_summary from registry (LLM context hint).

        Returns: str (≤200 chars) or None.
        """
        if status_code not in _TRANSLATABLE_STATUS:
            return None

        body_text = _stringify_body(response_body)
        if not body_text:
            # No body to translate — generic message is fine.
            return None

        # ---- Cache hit shortcut ----
        cache_key = _cache_key(status_code, body_text, tool_id)
        cached = await self._get_cached(cache_key)
        if cached is not None:
            return cached

        # ---- LLM call ----
        try:
            response = await self._llm.chat.completions.create(
                model=self._deployment,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": _build_user_prompt(
                            status_code, body_text, tool_intent,
                        ),
                    },
                ],
                temperature=0,
                max_tokens=120,
                response_format={"type": "json_object"},
            )
        except Exception as e:  # noqa: BLE001 — silent fallback
            logger.warning("api_error_translator LLM error: %s", e)
            return None

        message = _parse_response(response)
        if message is None:
            return None

        # ---- Cache for next time ----
        await self._set_cached(cache_key, message)
        return message

    async def _get_cached(self, key: str) -> Optional[str]:
        if self._redis is None:
            return None
        try:
            raw = await self._redis.get(key)
            if not raw:
                return None
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            return raw
        except Exception as e:  # noqa: BLE001
            logger.debug("error translator cache get failed: %s", e)
            return None

    async def _set_cached(self, key: str, value: str) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.setex(key, _CACHE_TTL_SECONDS, value)
        except Exception as e:  # noqa: BLE001
            logger.debug("error translator cache set failed: %s", e)


def _stringify_body(body: object) -> str:
    """Convert response body (dict / list / str / None) to a single
    string suitable for the LLM prompt. Truncate at 800 chars."""
    if body is None:
        return ""
    if isinstance(body, (dict, list)):
        try:
            text = json.dumps(body, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(body)
    else:
        text = str(body)
    text = text.strip()
    if len(text) > 800:
        text = text[:800] + "..."
    return text


def _build_user_prompt(
    status_code: int, body_text: str, tool_intent: str,
) -> str:
    parts = [f"HTTP status: {status_code}"]
    if tool_intent:
        parts.append(f"Akcija koju je korisnik pokušao: {tool_intent}")
    parts.append(f"API odgovor: {body_text}")
    return "\n".join(parts)


def _parse_response(response) -> Optional[str]:
    """Extract 'message' field from LLM JSON response. None on any
    malformation (caller falls back to generic message)."""
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
    msg = data.get("message")
    if not isinstance(msg, str):
        return None
    msg = msg.strip()
    if not msg:
        return None
    # Hard cap (in case LLM ignored the prompt)
    return msg[:300]
