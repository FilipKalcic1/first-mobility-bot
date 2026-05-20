"""Pending mutation state — survives across user turns.

When L6 mutation gate returns CONFIRM (single Da/Ne), the engine sends a
confirm prompt and the user's next message must trigger execution.
Without persistence, "Da" gets re-classified by L2a/L3 and the original
mutation is lost. That's a 0-error tolerance violation — the user thinks
they confirmed an action that never executes.

This store fixes it: the pending mutation (tool_id + params) is saved to
Redis with a short TTL (default 5 min). The engine consults it BEFORE
any other classification on the next turn.

Single-confirm policy (Filip 2026-05-16): only STAGE_SINGLE exists.
Previous DOUBLE-confirm stages were dropped as infeasible UX (3 turns
for 1 delete; user closes WhatsApp mid-flow → bot stuck).

Storage shape (JSON):
    {
        "tool_id": "delete_VehicleCalendar_id",
        "params":  {"id": "x"},
        "stage":   "single",     # only valid value now
        "created_at": 1714000000.0,
    }
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from typing import Optional

logger = logging.getLogger(__name__)


_REDIS_PREFIX = "v2:pending_mut:"
_DEFAULT_TTL_SECONDS = 300  # 5 minutes — short, safety against stale Da

# Stage — only one exists post single-confirm policy.
STAGE_SINGLE = "single"


@dataclass
class PendingMutation:
    tool_id: str
    params: dict
    stage: str
    created_at: float

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> "PendingMutation":
        d = json.loads(raw)
        return cls(
            tool_id=d["tool_id"],
            params=d.get("params") or {},
            stage=d.get("stage") or STAGE_SINGLE,
            created_at=float(d.get("created_at") or 0.0),
        )


class PendingMutationStore:
    """Tiny Redis wrapper. Same fault-tolerance contract as FlowStateStore:
    never raises on transient Redis failure."""

    def __init__(self, redis_client, ttl_seconds: int = _DEFAULT_TTL_SECONDS):
        self._redis = redis_client
        self._ttl = ttl_seconds

    @staticmethod
    def _key(phone: str) -> str:
        return _REDIS_PREFIX + phone.strip()

    async def load(self, phone: str) -> Optional[PendingMutation]:
        try:
            raw = await self._redis.get(self._key(phone))
        except Exception as e:  # noqa: BLE001
            logger.warning("pending mutation load failed: %s", e)
            return None
        if not raw:
            return None
        try:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            return PendingMutation.from_json(raw)
        except (ValueError, TypeError, KeyError) as e:
            logger.warning("pending mutation corrupt for %s: %s", phone[-4:], e)
            return None

    async def save(
        self,
        phone: str,
        tool_id: str,
        params: dict,
        stage: str,
    ) -> None:
        pending = PendingMutation(
            tool_id=tool_id,
            params=params or {},
            stage=stage,
            created_at=time.time(),
        )
        try:
            await self._redis.setex(
                self._key(phone), self._ttl, pending.to_json()
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("pending mutation save failed: %s", e)

    async def clear(self, phone: str) -> None:
        try:
            await self._redis.delete(self._key(phone))
        except Exception as e:  # noqa: BLE001
            logger.warning("pending mutation clear failed: %s", e)

    # ---- Execution lock (anti-replay during in-flight mutation) ----
    # Prevents the clear-then-execute race: if pending is cleared first
    # then execute crashes, the mutation is lost; if execute runs first
    # then clear fails on Redis transient, the next "Da" double-executes.
    # Solution: atomic SET-NX lock around execute(). Concurrent "Da"
    # within the lock window gets a friendly "u tijeku" message instead
    # of triggering a second execute.

    @staticmethod
    def _exec_lock_key(phone: str) -> str:
        return "v2:pending_mut_exec:" + phone.strip()

    async def try_acquire_execution(
        self, phone: str, ttl_seconds: int = 30,
    ) -> bool:
        """Atomic check-and-set. Returns True if THIS caller owns the
        execution slot for `phone` for the next `ttl_seconds`. Returns
        False if another in-flight execution holds the lock. On Redis
        outage falls open (returns True) — losing replay protection
        during outage is preferable to blocking all mutations.
        """
        try:
            # SET NX EX = atomic create-if-absent with TTL
            ok = await self._redis.set(
                self._exec_lock_key(phone), "1", nx=True, ex=ttl_seconds,
            )
            return bool(ok)
        except Exception as e:  # noqa: BLE001
            logger.warning("exec lock acquire failed (fail-open): %s", e)
            return True

    async def release_execution(self, phone: str) -> None:
        try:
            await self._redis.delete(self._exec_lock_key(phone))
        except Exception as e:  # noqa: BLE001
            logger.warning("exec lock release failed (TTL will expire): %s", e)


# --------------------------------------------------------------------------
# Reply parsing — what does "Da" / "Ne" / "TRAJNO" / arbitrary mean?
# --------------------------------------------------------------------------


# EXECUTE whitelist — STRICT exact match (after trimming punctuation). The
# old word-split match (`any(a in norm.split() ...)`) caused false positives
# like "može biti" → execute because "može" was in the affirmative set. For a
# destructive confirm, prefer re-asking to silently running on a hesitant reply.
# Filip 2026-05-17.
_AFFIRMATIVE = {"da", "yes", "y", "ok", "okej", "može", "moze",
                "potvrdjujem", "potvrđujem"}
# CANCEL set stays lenient (word-split fallback) because false negatives only
# mean the bot re-asks — safer to over-cancel than under-cancel.
_NEGATIVE = {"ne", "no", "n", "odustani", "prekini", "stani", "cancel",
             "otkaži", "otkazi"}

# Trailing punctuation to strip before matching ("Da." / "Da!" / "ok?").
_TRAILING_PUNCT = ".!?,;"


def parse_reply(text: str, stage: str) -> str:
    """Classify user's reply to a pending confirm prompt.

    Returns one of:
        "execute"   → run the saved mutation
        "cancel"    → user said no
        "ambiguous" → unclear; re-prompt or fall through (caller decides)

    EXECUTE requires EXACT match against `_AFFIRMATIVE` (post-trim) — multi-
    word replies like "može biti" / "ok ali..." / "da možda" are ambiguous,
    not execute. CANCEL allows word-split fallback so phrases like "ne hvala"
    still cancel safely.

    Unknown stage strings (typo / corrupt cache) are treated as ambiguous
    so the engine re-prompts instead of executing.
    """
    if stage != STAGE_SINGLE:
        logger.warning("parse_reply got unknown stage=%r", stage)
        return "ambiguous"

    norm = (text or "").strip().lower().rstrip(_TRAILING_PUNCT)
    if not norm:
        return "ambiguous"

    # Cancel — lenient (exact OR any word match).
    if norm in _NEGATIVE or any(n in norm.split() for n in _NEGATIVE):
        return "cancel"

    # Execute — STRICT (exact match only).
    if norm in _AFFIRMATIVE:
        return "execute"

    return "ambiguous"
