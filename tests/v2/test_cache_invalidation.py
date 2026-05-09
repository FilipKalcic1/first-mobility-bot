"""Tests for cache_invalidation."""
import json
import pytest

from services.v2.cache_invalidation import (
    InvalidationEvent,
    InvalidationResult,
    verify_signature,
    handle_invalidation_event,
)


class _FakeStore:
    def __init__(self, raise_on_clear: bool = False):
        self.cleared: list[str] = []
        self.raise_on_clear = raise_on_clear

    async def clear(self, phone: str) -> None:
        if self.raise_on_clear:
            raise RuntimeError("boom")
        self.cleared.append(phone)


def test_verify_signature_valid():
    secret = "shared-secret"
    payload = b'{"phone":"+385912345678","reasons":["vehicle_change"]}'
    import hashlib, hmac
    sig = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    assert verify_signature(payload, sig, secret) is True


def test_verify_signature_invalid():
    payload = b'{"phone":"+385912345678"}'
    assert verify_signature(payload, "wrong-sig", "secret") is False


def test_verify_signature_empty_inputs():
    assert verify_signature(b"x", "", "secret") is False
    assert verify_signature(b"x", "sig", "") is False


@pytest.mark.asyncio
async def test_handle_event_clears_identity():
    identity = _FakeStore()
    event = InvalidationEvent(
        phone="+385912345678",
        reasons=["vehicle_change"],
        timestamp=0.0,
    )
    result = await handle_invalidation_event(
        event, identity_context=identity,
    )
    assert result.success is True
    assert "identity_cache" in result.cleared_stores
    assert identity.cleared == ["+385912345678"]


@pytest.mark.asyncio
async def test_handle_event_keeps_conversation_on_vehicle_change():
    conv = _FakeStore()
    event = InvalidationEvent(
        phone="+385912345678",
        reasons=["vehicle_change"],
        timestamp=0.0,
    )
    result = await handle_invalidation_event(
        event, conversation_history_store=conv,
    )
    assert "conversation_history" not in result.cleared_stores
    assert conv.cleared == []


@pytest.mark.asyncio
async def test_handle_event_clears_conversation_on_role_change():
    conv = _FakeStore()
    event = InvalidationEvent(
        phone="+385912345678",
        reasons=["role_change"],
        timestamp=0.0,
    )
    result = await handle_invalidation_event(
        event, conversation_history_store=conv,
    )
    assert "conversation_history" in result.cleared_stores
    assert conv.cleared == ["+385912345678"]


@pytest.mark.asyncio
async def test_handle_event_clears_conversation_on_termination():
    conv = _FakeStore()
    event = InvalidationEvent(
        phone="+385912345678",
        reasons=["termination"],
        timestamp=0.0,
    )
    result = await handle_invalidation_event(
        event, conversation_history_store=conv,
    )
    assert "conversation_history" in result.cleared_stores


@pytest.mark.asyncio
async def test_handle_event_always_clears_pending_stores():
    pmut = _FakeStore()
    pclar = _FakeStore()
    fact = _FakeStore()
    event = InvalidationEvent(
        phone="+385912345678",
        reasons=["vehicle_change"],
        timestamp=0.0,
    )
    result = await handle_invalidation_event(
        event,
        pending_mut_store=pmut,
        pending_clarify_store=pclar,
        fact_snapshot_store=fact,
    )
    assert "pending_mutation" in result.cleared_stores
    assert "pending_clarify" in result.cleared_stores
    assert "fact_snapshot" in result.cleared_stores


@pytest.mark.asyncio
async def test_handle_event_collects_errors():
    bad = _FakeStore(raise_on_clear=True)
    event = InvalidationEvent(
        phone="+385912345678",
        reasons=["vehicle_change"],
        timestamp=0.0,
    )
    result = await handle_invalidation_event(
        event, pending_mut_store=bad,
    )
    assert result.success is False
    assert any("pending_mutation" in e for e in result.errors)


@pytest.mark.asyncio
async def test_handle_event_no_stores_wired():
    event = InvalidationEvent(
        phone="+385912345678",
        reasons=["vehicle_change"],
        timestamp=0.0,
    )
    result = await handle_invalidation_event(event)
    assert result.success is True
    assert result.cleared_stores == []


def test_invalidation_event_is_frozen():
    event = InvalidationEvent(
        phone="+385912345678",
        reasons=["vehicle_change"],
        timestamp=0.0,
    )
    with pytest.raises(Exception):
        event.phone = "other"  # type: ignore[misc]
