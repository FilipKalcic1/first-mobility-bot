"""Conversation history store — last N user/bot turns per phone.

Enables multi-turn context for the V3 router:
- "kolika km" → bot answers "34521 km"
- next: "a prošli mjesec" → router needs context of previous turn

Pickers (DomainPicker, DomainScopedToolPicker) already accept
`recent_turns: list[dict]` parameter. This store provides the data.

Redis-backed list with TTL (30min sliding). Idempotent + safe — never
blocks user request if Redis is down.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict

logger = logging.getLogger(__name__)

REDIS_KEY_PREFIX = "v2_conv_history:"
DEFAULT_TTL_SECONDS = 1800  # 30 min sliding session
DEFAULT_MAX_TURNS = 5


@dataclass
class ConversationTurn:
    """One user/bot exchange."""
    user: str
    bot: str = ""
    bot_action: str = ""  # tool_id or "fallback" or "clarify_top3"

    def to_dict(self) -> dict:
        return asdict(self)


class ConversationHistoryStore:
    """Redis list per phone, FIFO bounded to max_turns.

    Methods:
      append(phone, turn)  — push to right, trim to max_turns
      load(phone)          — return list of recent turns (oldest first)
      clear(phone)         — wipe state (e.g. on session reset)
    """

    def __init__(
        self,
        redis_client,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        max_turns: int = DEFAULT_MAX_TURNS,
    ):
        self._redis = redis_client
        self._ttl = ttl_seconds
        self._max_turns = max_turns

    @staticmethod
    def _key(phone: str) -> str:
        return f"{REDIS_KEY_PREFIX}{phone}"

    async def append(self, phone: str, turn: ConversationTurn) -> None:
        if self._redis is None:
            return
        key = self._key(phone)
        try:
            payload = json.dumps(turn.to_dict(), ensure_ascii=False)
            await self._redis.rpush(key, payload)
            await self._redis.ltrim(key, -self._max_turns, -1)
            await self._redis.expire(key, self._ttl)
        except Exception as e:  # noqa: BLE001
            logger.warning("ConversationHistoryStore.append failed: %s", e)

    async def load(self, phone: str) -> list[dict]:
        if self._redis is None:
            return []
        try:
            raw_list = await self._redis.lrange(self._key(phone), 0, -1)
            if not raw_list:
                return []
            out = []
            for raw in raw_list:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                try:
                    out.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue
            return out
        except Exception as e:  # noqa: BLE001
            logger.warning("ConversationHistoryStore.load failed: %s", e)
            return []

    async def clear(self, phone: str) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.delete(self._key(phone))
        except Exception as e:  # noqa: BLE001
            logger.warning("ConversationHistoryStore.clear failed: %s", e)
