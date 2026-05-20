"""Latency UX helpers — addresses critique #2.

WhatsApp doesn't support streaming, but we can:
1. Send "typing..." indicator immediately on webhook receipt
2. Split long-running queries: ack message ("Tražim...") → real response

Two integration patterns the caller can use:

A) Threshold-timer (recommended): start a watchdog task on entry that
   fires the typing-indicator callback only after a threshold (default
   300ms) elapses. If the engine returns sooner (quick-path, special
   intents, unknown-phone gate), the watchdog is cancelled before it
   fires — no spurious "typing..." flash on instant replies. Scales
   without per-path manual flagging.

   Use `make_typing_watchdog(callback)` for a managed watchdog that
   cleans up on async-context exit.

B) Hint-based (legacy): `hint_for_query(q)` returns a `LatencyHint` with
   `send_typing`/`send_ack_message`/`ack_text`. Caller decides up-front.
   Fragile when new fast paths are added — kept for callers that need
   pre-engine decisions without an async context.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LatencyHint:
    send_typing: bool
    send_ack_message: bool
    ack_text: Optional[str] = None


def hint_for_query(query: str) -> LatencyHint:
    """Decide whether to send typing indicator + ack message.

    Every query now goes through LLM router → typing always on; ack message
    only for queries long enough that the user would otherwise wonder
    whether the bot received them.
    """
    if not query:
        return LatencyHint(send_typing=False, send_ack_message=False)

    word_count = len(query.lower().strip().split())

    if word_count >= 8:
        return LatencyHint(
            send_typing=True,
            send_ack_message=True,
            ack_text="Tražim, sekunda...",
        )

    return LatencyHint(send_typing=True, send_ack_message=False)


# WhatsApp 4096-char limit (#10)
WHATSAPP_MAX_CHARS = 4096
WHATSAPP_SAFE_LIMIT = 3900  # leave headroom


def chunk_for_whatsapp(text: str, max_chars: int = WHATSAPP_SAFE_LIMIT) -> list[str]:
    """Split text into WhatsApp-deliverable chunks. Preserves paragraph
    boundaries when possible. Adds (1/N) suffix for multi-part.

    B1 note: worker.py has its own `_split_message` (paragraph→line→
    hard-split, no (1/N) suffix) that currently serves production v1
    traffic. The two implementations differ in user-visible output —
    converging now would silently change format for live users. The
    convergence happens at Faza E (v1 delete) when worker is rewired
    to call this function. Until then, BOTH are intentional.
    """
    if not text:
        return [""]
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= max_chars:
            chunks.append(remaining)
            break
        # Try to break at paragraph or sentence boundary
        candidate = remaining[:max_chars]
        # Search backward for paragraph break
        for sep in ("\n\n", "\n", ". ", " "):
            idx = candidate.rfind(sep)
            if idx > max_chars // 2:
                chunks.append(remaining[:idx + len(sep)])
                remaining = remaining[idx + len(sep):]
                break
        else:
            # Hard cut
            chunks.append(candidate)
            remaining = remaining[max_chars:]

    if len(chunks) > 1:
        total = len(chunks)
        chunks = [f"{c}\n\n({i+1}/{total})" for i, c in enumerate(chunks)]
    return chunks


# ---------------------------------------------------------------------------
# Threshold typing-indicator watchdog
# ---------------------------------------------------------------------------

DEFAULT_TYPING_THRESHOLD_MS = 300


@asynccontextmanager
async def typing_watchdog(
    callback: Callable[[], Awaitable[None]],
    *,
    threshold_ms: int = DEFAULT_TYPING_THRESHOLD_MS,
):
    """Async context manager that fires `callback()` if the wrapped block
    runs longer than `threshold_ms`. Synchronous-resolving paths (quick-
    path, special intents) finish well under the threshold and the
    callback never fires — no spurious "typing..." indicator.

    Usage:
        async def send_typing_to_whatsapp():
            await whatsapp.send_typing(phone)

        async with typing_watchdog(send_typing_to_whatsapp):
            response = await engine.process_message(phone, query)

    Callback failures are logged but never raised — the user query
    must continue regardless of typing-indicator delivery problems.
    """
    if threshold_ms <= 0:
        # Edge: zero/negative threshold disables the watchdog.
        yield
        return

    async def _fire_after_delay():
        try:
            await asyncio.sleep(threshold_ms / 1000.0)
            await callback()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("typing watchdog callback failed: %s", e)

    task = asyncio.create_task(_fire_after_delay())
    try:
        yield
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
