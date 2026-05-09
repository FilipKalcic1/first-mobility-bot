"""Tests for latency_ux."""
import asyncio

import pytest

from services.v2.latency_ux import (
    hint_for_query, chunk_for_whatsapp, LatencyHint,
    WHATSAPP_MAX_CHARS, WHATSAPP_SAFE_LIMIT,
    typing_watchdog, DEFAULT_TYPING_THRESHOLD_MS,
)


def test_short_query_typing_no_ack():
    out = hint_for_query("kolika km")
    assert out.send_typing is True
    assert out.send_ack_message is False


def test_long_query_sends_ack():
    long_q = "možeš mi prikazati svu povijest kilometraže za moj auto"
    out = hint_for_query(long_q)
    assert out.send_typing is True
    assert out.send_ack_message is True
    assert "sekunda" in (out.ack_text or "").lower() or "tražim" in (out.ack_text or "").lower()


def test_empty_query_no_typing():
    out = hint_for_query("")
    assert out.send_typing is False


def test_chunk_short_message():
    chunks = chunk_for_whatsapp("kratka poruka")
    assert chunks == ["kratka poruka"]


def test_chunk_at_limit():
    text = "x" * WHATSAPP_SAFE_LIMIT
    chunks = chunk_for_whatsapp(text)
    assert len(chunks) == 1


def test_chunk_long_message_paragraph_break():
    para1 = "Prvi paragraf. " * 200
    para2 = "Drugi paragraf. " * 200
    text = para1 + "\n\n" + para2
    chunks = chunk_for_whatsapp(text)
    assert len(chunks) >= 2
    assert "(1/" in chunks[0]


def test_chunk_no_break_hard_cuts():
    text = "x" * (WHATSAPP_SAFE_LIMIT * 2)
    chunks = chunk_for_whatsapp(text)
    assert len(chunks) >= 2


def test_chunk_includes_part_indicator():
    text = "Sentence one. " * 1000  # forces split
    chunks = chunk_for_whatsapp(text)
    if len(chunks) > 1:
        assert "(1/" in chunks[0]
        assert f"({len(chunks)}/" in chunks[-1]


def test_dataclass_default():
    h = LatencyHint(send_typing=False, send_ack_message=False)
    assert h.ack_text is None


def test_whatsapp_constants():
    assert WHATSAPP_MAX_CHARS == 4096
    assert WHATSAPP_SAFE_LIMIT < WHATSAPP_MAX_CHARS


# ---------------------------------------------------------------------------
# typing_watchdog — threshold-based typing-indicator gate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_watchdog_fires_after_threshold():
    fired = asyncio.Event()

    async def cb():
        fired.set()

    async with typing_watchdog(cb, threshold_ms=20):
        await asyncio.sleep(0.1)  # well past threshold

    assert fired.is_set()


@pytest.mark.asyncio
async def test_watchdog_skipped_for_fast_path():
    """If the wrapped block finishes before threshold, callback never fires."""
    fired = asyncio.Event()

    async def cb():
        fired.set()

    async with typing_watchdog(cb, threshold_ms=200):
        # Synchronous-ish path — finishes immediately
        pass

    # Wait a bit longer than threshold to be sure
    await asyncio.sleep(0.25)
    assert not fired.is_set()


@pytest.mark.asyncio
async def test_watchdog_zero_threshold_disabled():
    fired = asyncio.Event()

    async def cb():
        fired.set()

    async with typing_watchdog(cb, threshold_ms=0):
        await asyncio.sleep(0.05)

    assert not fired.is_set()


@pytest.mark.asyncio
async def test_watchdog_swallows_callback_exception():
    """A failing typing-indicator must not break the user query."""
    async def cb():
        raise RuntimeError("whatsapp api unreachable")

    # Should not raise
    async with typing_watchdog(cb, threshold_ms=10):
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_watchdog_propagates_inner_exception():
    """An exception inside the wrapped block is not swallowed."""
    async def cb():
        pass

    with pytest.raises(ValueError):
        async with typing_watchdog(cb, threshold_ms=200):
            raise ValueError("downstream error")


def test_default_threshold_ms():
    # Sanity: the default matches the agreed 300ms design.
    assert DEFAULT_TYPING_THRESHOLD_MS == 300
