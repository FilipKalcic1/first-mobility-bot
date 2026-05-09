"""Test the V2Engine.process_message_chunked convenience method.

Avoids the full engine fixture: borrows the method off V2Engine and
binds it to a stub that overrides process_message. The method is a
thin wrapper, so behavior is fully captured by the chunker contract
plus the wrapping itself.
"""
import pytest

from services.v2.engine import V2Engine
from services.v2.latency_ux import WHATSAPP_SAFE_LIMIT


class _StubEngine:
    """Minimal duck-type with the same method shape as V2Engine."""
    process_message_chunked = V2Engine.process_message_chunked

    def __init__(self, fixed_response: str):
        self._fixed_response = fixed_response

    async def process_message(self, phone: str, query: str) -> str:
        return self._fixed_response


@pytest.mark.asyncio
async def test_short_response_returns_one_chunk():
    engine = _StubEngine("kratak odgovor")
    chunks = await engine.process_message_chunked("+385912345678", "kolika km")
    assert chunks == ["kratak odgovor"]


@pytest.mark.asyncio
async def test_long_response_splits_into_chunks():
    long_text = ("Recimo da je ovo jedan paragraf. " * 200) + \
                "\n\n" + ("Drugi paragraf. " * 200)
    engine = _StubEngine(long_text)
    chunks = await engine.process_message_chunked("+385912345678", "x")
    assert len(chunks) >= 2
    assert "(1/" in chunks[0]
    assert chunks[-1].rstrip().endswith(f"({len(chunks)}/{len(chunks)})")


@pytest.mark.asyncio
async def test_chunk_lengths_within_limit():
    text = "x" * (WHATSAPP_SAFE_LIMIT * 3 + 100)
    engine = _StubEngine(text)
    chunks = await engine.process_message_chunked("p", "q")
    # Each chunk (post-suffix) should be within WhatsApp 4096-char hard cap
    for c in chunks:
        assert len(c) <= 4096


@pytest.mark.asyncio
async def test_empty_response_yields_empty_chunk():
    engine = _StubEngine("")
    chunks = await engine.process_message_chunked("p", "q")
    assert chunks == [""]
