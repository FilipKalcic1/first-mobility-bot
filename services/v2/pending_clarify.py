"""Pending clarify-cards store.

When L5 confidence gate falls back with top-3 candidates, the engine sends
the user a Top-3 cards message ("1️⃣ X / 2️⃣ Y / 3️⃣ Z / ❌ Drugo"). The
user's NEXT message is interpreted as their pick (1/2/3) — but this only
works if we PERSIST the candidates between turns.

Mirrors `pending_mutation.py` design:
  - SETEX key with TTL (5 min — clarify is a fast user decision)
  - load() returns None if user takes too long → fall back to fresh recognition
  - clear() on resolution

Why this matters: Filip's real corpus showed bot freeze when stuck in
disambiguation chain. Anti-loop guard caps clarify count, but THIS store
makes clarify ACTUALLY useful instead of asking same question over.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from typing import Optional

logger = logging.getLogger(__name__)


REDIS_KEY_PREFIX = "v2_pending_clarify:"
DEFAULT_TTL_SECONDS = 300  # 5 minutes


STAGE_ACTION = "action"  # Step 1 (fallback context): user bira akciju iz set-a
                          # koji router-ova low-confidence top-K kandidata daje.
                          # Ovo je legacy fallback model.
STAGE_TOOL = "tool"      # Step 2 (or single-step legacy): user bira specifičan tool
STAGE_ACTION_GLOBAL = "action_global"
                          # Model A (Filip 2026-05-17): user uvijek bira akciju
                          # prvo PRIJE router-a. State drži original_query +
                          # identity context. Nakon pick-a, engine pokreće
                          # silent filters + L3 router scoped → STAGE_TOOL.


@dataclass
class PendingClarify:
    """One clarify-pending state per phone.

    Two-step clarify (Filip direktiva 2026-05-17):
      stage="action" → engine prikazao actions (1=POGLEDATI / 2=UNIJETI / ...),
                       čeka pick. Nakon pick-a, filter candidates i prelazi
                       u stage="tool".
      stage="tool"   → engine prikazao top-3 tools (originalni flow), čeka pick.

    Backward compat: stage defaults to "tool" (postojeći direktni clarify
    flow ne mijenja se).
    """
    phone: str
    candidates: list[dict] = field(default_factory=list)
    # candidates: list of {"tool_id": "...", "label": "...", "description": "...", "method": "GET|POST|..."}
    original_query: str = ""
    stage: str = STAGE_TOOL

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, s: str) -> "PendingClarify":
        d = json.loads(s)
        # Backward compat: legacy entries (pre 2026-05-17) lack `stage` field
        d.setdefault("stage", STAGE_TOOL)
        return cls(**d)


class PendingClarifyStore:
    """Redis-backed store, mirrors pending_mutation.PendingMutationStore."""

    def __init__(self, redis_client, ttl_seconds: int = DEFAULT_TTL_SECONDS):
        self._redis = redis_client
        self._ttl = ttl_seconds

    @staticmethod
    def _key(phone: str) -> str:
        return f"{REDIS_KEY_PREFIX}{phone}"

    async def save(
        self,
        phone: str,
        candidates: list[dict],
        original_query: str = "",
        stage: str = STAGE_TOOL,
    ) -> None:
        """Persist pending clarify. Caller assembled candidates in
        clarify_ui-compatible format ({tool_id, label, description}).

        ``stage`` selects 2-step flow position:
          - STAGE_ACTION: user just received action picker (Step 1)
          - STAGE_TOOL (default): user received tool picker (Step 2 or
            single-step direct clarify when all candidates share method)
        """
        if self._redis is None:
            return
        pc = PendingClarify(
            phone=phone, candidates=candidates,
            original_query=original_query, stage=stage,
        )
        try:
            await self._redis.setex(self._key(phone), self._ttl, pc.to_json())
        except Exception as e:  # noqa: BLE001
            logger.warning("PendingClarifyStore.save failed: %s", e)

    async def load(self, phone: str) -> Optional[PendingClarify]:
        if self._redis is None:
            return None
        try:
            raw = await self._redis.get(self._key(phone))
            if not raw:
                return None
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            return PendingClarify.from_json(raw)
        except Exception as e:  # noqa: BLE001
            logger.warning("PendingClarifyStore.load failed: %s", e)
            return None

    async def clear(self, phone: str) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.delete(self._key(phone))
        except Exception as e:  # noqa: BLE001
            logger.warning("PendingClarifyStore.clear failed: %s", e)
