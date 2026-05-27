"""Pending param-collection store.

Mirrors `pending_clarify.py` / `pending_mutation.py` shape. Holds state
between turns while the bot asks the user for one or more required (or
optional) parameters for a chosen tool.

Lifecycle:
  1. Turn N (user picks tool from Model A picker, or router lands directly):
       engine detects route_result.missing_required is non-empty →
       save(PendingParams(tool_id, collected, required_remaining=[..],
                          optional_remaining=[..], optional_offered=False))
       render first question
  2. Turn N+1 (user answers): load → parse → store value in collected →
       remove from required_remaining → if more required, ask next; else
       if optional_remaining, offer them ("hoćeš dodati?")
  3. Turn N+2 (user answers optional offer): "ne" → clear required path,
       proceed to mutation_gate; "da" → iterate optional_remaining like
       required
  4. Done → clear pending_params → mutation_gate → execute

Why Filip wanted this:
  Model A cascade always ends at execute, but LLM router can't always fill
  required params from a one-shot query. Without this, "obriši rezervaciju"
  (no ID) ends at API 400. With this, bot asks "Trebam ID zapisa." and
  collects it cleanly.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


REDIS_KEY_PREFIX = "v2_pending_params:"
DEFAULT_TTL_SECONDS = 300  # 5 minutes, mirror pending_clarify


@dataclass
class PendingParams:
    """One param-collection state per phone."""
    phone: str
    tool_id: str
    collected: dict = field(default_factory=dict)
    required_remaining: list = field(default_factory=list)
    optional_remaining: list = field(default_factory=list)
    optional_offered: bool = False
    original_query: str = ""
    # *TypeId resolver (Filip 2026-05-27): fetched {param: [[id, name], …]} so the
    # ask-with-list render + answer-match don't re-fetch /…Types each turn.
    type_options: dict = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, s: str) -> "PendingParams":
        d = json.loads(s)
        return cls(**d)


class PendingParamsStore:
    """Redis-backed store. Same interface shape as PendingClarifyStore."""

    def __init__(self, redis_client, ttl_seconds: int = DEFAULT_TTL_SECONDS):
        self._redis = redis_client
        self._ttl = ttl_seconds

    @staticmethod
    def _key(phone: str) -> str:
        return f"{REDIS_KEY_PREFIX}{phone}"

    async def save(self, phone: str, state: PendingParams) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.setex(self._key(phone), self._ttl, state.to_json())
        except Exception as e:  # noqa: BLE001
            logger.warning("PendingParamsStore.save failed: %s", e)

    async def load(self, phone: str) -> Optional[PendingParams]:
        if self._redis is None:
            return None
        try:
            raw = await self._redis.get(self._key(phone))
            if not raw:
                return None
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            return PendingParams.from_json(raw)
        except Exception as e:  # noqa: BLE001
            logger.warning("PendingParamsStore.load failed: %s", e)
            return None

    async def clear(self, phone: str) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.delete(self._key(phone))
        except Exception as e:  # noqa: BLE001
            logger.warning("PendingParamsStore.clear failed: %s", e)
