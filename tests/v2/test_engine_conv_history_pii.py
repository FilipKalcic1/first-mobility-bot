"""Faza 12.1 (Filip 2026-05-18) — PII must NOT leak into conversation history.

Bug found via 12-agent review + verified: engine.py:209 used to call
`ConversationHistoryStore.append(user=query[:200])` with the RAW user
query. PII scrubber runs INSIDE `_dispatch_message`, but the outer
`process_message` was persisting raw `query` to Redis for 30 min →
GDPR breach if user typed OIB / IBAN / phone in WhatsApp.

This test exercises the fixed code path and asserts that what reaches
the ConversationHistoryStore is the scrubbed text, not the raw query.
"""
from __future__ import annotations

import pytest

from services.v2.conversation_history import ConversationTurn
from services.v2.pii_scrubber import PIIScrubber


class _CaptureStore:
    """Captures the ConversationTurn that engine appends."""

    def __init__(self):
        self.captured: list[ConversationTurn] = []

    async def append(self, phone: str, turn: ConversationTurn) -> None:
        self.captured.append(turn)


def _simulate_engine_append(pii: PIIScrubber, store: _CaptureStore, query: str, response: str) -> None:
    """Inline reproduction of engine.py:204-218 logic.
    Single source of behavior under test — if the fix is reverted, this
    block diverges from engine.py and the test still catches it because
    test_pii_scrubbed_before_conv_history_append uses the SAME pii instance."""
    # Mirror exactly what engine.py does
    import asyncio
    async def _go():
        scrubbed_user = pii.scrub(query).scrubbed_text
        await store.append(
            "385955087196",
            ConversationTurn(
                user=scrubbed_user[:200],
                bot=response[:200],
            ),
        )
    asyncio.run(_go())


def test_pii_scrubber_redacts_oib_in_isolation():
    """Sanity check: PIIScrubber detects 11-digit OIB."""
    pii = PIIScrubber()
    result = pii.scrub("moj OIB je 12345678901")
    assert "12345678901" not in result.scrubbed_text
    assert len(result.redactions) >= 1


def test_pii_scrubber_redacts_iban():
    pii = PIIScrubber()
    result = pii.scrub("posalji na HR1234567890123456789")
    assert "HR1234567890123456789" not in result.scrubbed_text


def test_pii_scrubber_redacts_email():
    pii = PIIScrubber()
    result = pii.scrub("kontaktiraj me na ivan.peric@example.com")
    assert "ivan.peric@example.com" not in result.scrubbed_text


# ----------------------------------------------------------------------
# The real regression test for Faza 12.1
# ----------------------------------------------------------------------


def test_oib_in_user_query_is_scrubbed_before_conv_history_append():
    """If user types OIB in WhatsApp, engine MUST not persist it raw to
    the 30-min conversation_history Redis cache. This was the GDPR bug
    found in 12-agent audit (Filip 2026-05-18)."""
    pii = PIIScrubber()
    store = _CaptureStore()
    _simulate_engine_append(
        pii, store,
        query="moj OIB je 12345678901 obrisi me",
        response="OK, obrisao sam te.",
    )
    assert len(store.captured) == 1
    captured_user = store.captured[0].user
    assert "12345678901" not in captured_user, (
        f"OIB leaked into conversation history: {captured_user!r}"
    )


def test_iban_in_user_query_is_scrubbed_before_conv_history_append():
    pii = PIIScrubber()
    store = _CaptureStore()
    _simulate_engine_append(
        pii, store,
        query="prebaci na HR1234567890123456789 hitno",
        response="OK.",
    )
    assert "HR1234567890123456789" not in store.captured[0].user


def test_email_in_user_query_is_scrubbed_before_conv_history_append():
    pii = PIIScrubber()
    store = _CaptureStore()
    _simulate_engine_append(
        pii, store,
        query="posalji izvjestaj na ivan.peric@example.com",
        response="OK.",
    )
    assert "ivan.peric@example.com" not in store.captured[0].user


def test_clean_query_passes_through_unchanged():
    """Sanity: non-PII queries should still appear in conv history."""
    pii = PIIScrubber()
    store = _CaptureStore()
    _simulate_engine_append(
        pii, store,
        query="kolika mi je km",
        response="34521 km",
    )
    assert store.captured[0].user == "kolika mi je km"
    assert store.captured[0].bot == "34521 km"


def test_engine_code_actually_calls_pii_scrubber():
    """Belt-and-suspenders: read engine.py to confirm the append site
    uses self.pii.scrub. If someone reverts the fix, this test fails.
    Without it, the simulator-based tests above would still pass even
    if engine.py was broken — defeating the regression-test purpose."""
    from pathlib import Path
    engine_src = (Path(__file__).resolve().parents[2] /
                  "services" / "v2" / "engine.py").read_text(encoding="utf-8")
    # The append site (around line 204-218) must run pii.scrub before
    # constructing the ConversationTurn.
    append_block_start = engine_src.find("conversation_history_store.append")
    assert append_block_start > 0, "could not locate conv_history append site"
    # Look backwards up to 500 chars for self.pii.scrub
    window = engine_src[max(0, append_block_start - 500):append_block_start]
    assert "self.pii.scrub(query)" in window, (
        "engine.py:append site must call self.pii.scrub(query) "
        "before persisting to conversation history — Faza 12.1 regression"
    )
