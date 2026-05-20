"""Unit tests for the framework-agnostic /admin/cache-invalidate core.

Targets `services.v2.cache_invalidation.process_request` — verifies HMAC,
payload-cap, IP rate-limit, and JSON-shape paths. The FastAPI binding
in main.py is a thin wrapper over this and inherits the same behavior.
"""
import hashlib
import hmac
import json
import time

import pytest

from services.v2 import cache_invalidation as ci


@pytest.fixture(autouse=True)
def _reset_ip_bucket():
    ci.reset_ip_bucket()
    yield
    ci.reset_ip_bucket()


def _sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.mark.asyncio
async def test_valid_event_clears_stores():
    cleared = []

    class _Store:
        async def clear(self, phone):
            cleared.append(phone)

    body = json.dumps({
        "phone": "+385912345678",
        "reasons": ["vehicle_change"],
        "timestamp": time.time(),
    }).encode()
    secret = "shared-test-secret"
    sig = _sign(body, secret)

    status, payload = await ci.process_request(
        body=body,
        signature=sig,
        declared_content_length=str(len(body)),
        client_ip="10.0.0.1",
        secret=secret,
        stores={"identity_context": _Store()},
    )
    assert status == 200
    assert payload["ok"] is True
    assert "identity_cache" in payload["cleared"]
    assert cleared == ["+385912345678"]


@pytest.mark.asyncio
async def test_invalid_signature_rejected_401():
    body = json.dumps({"phone": "+385912345678", "reasons": ["x"]}).encode()
    status, _ = await ci.process_request(
        body=body,
        signature="deadbeef",
        declared_content_length=str(len(body)),
        client_ip="10.0.0.2",
        secret="shared-test-secret",
        stores={},
    )
    assert status == 401


@pytest.mark.asyncio
async def test_oversized_declared_length_rejected_413():
    status, _ = await ci.process_request(
        body=b"{}",
        signature="x",
        declared_content_length="2048",
        client_ip="10.0.0.3",
        secret="s",
        stores={},
    )
    assert status == 413


@pytest.mark.asyncio
async def test_oversized_actual_body_rejected_before_hmac():
    big = b"x" * 2048
    status, _ = await ci.process_request(
        body=big,
        signature="x",
        declared_content_length=None,
        client_ip="10.0.0.4",
        secret="s",
        stores={},
    )
    assert status == 413


@pytest.mark.asyncio
async def test_missing_secret_returns_503():
    status, _ = await ci.process_request(
        body=b"{}",
        signature="x",
        declared_content_length=None,
        client_ip="10.0.0.5",
        secret="",
        stores={},
    )
    assert status == 503


@pytest.mark.asyncio
async def test_invalid_content_length_header_rejected_400():
    status, _ = await ci.process_request(
        body=b"{}",
        signature="x",
        declared_content_length="not-a-number",
        client_ip="10.0.0.6",
        secret="s",
        stores={},
    )
    assert status == 400


@pytest.mark.asyncio
async def test_malformed_json_rejected_400():
    secret = "s"
    body = b"{not json}"
    status, _ = await ci.process_request(
        body=body,
        signature=_sign(body, secret),
        declared_content_length=str(len(body)),
        client_ip="10.0.0.7",
        secret=secret,
        stores={},
    )
    assert status == 400


@pytest.mark.asyncio
async def test_missing_phone_rejected_400():
    secret = "s"
    body = json.dumps({"reasons": ["x"]}).encode()
    status, _ = await ci.process_request(
        body=body,
        signature=_sign(body, secret),
        declared_content_length=str(len(body)),
        client_ip="10.0.0.8",
        secret=secret,
        stores={},
    )
    assert status == 400


@pytest.mark.asyncio
async def test_ip_rate_limit_blocks_after_n():
    secret = "s"
    body = json.dumps({"phone": "+385900000000", "reasons": ["r"]}).encode()
    sig = _sign(body, secret)
    args = dict(
        body=body,
        signature=sig,
        declared_content_length=str(len(body)),
        client_ip="10.0.0.99",
        secret=secret,
        stores={},
    )
    for _ in range(ci.IP_RATE_LIMIT):
        status, _payload = await ci.process_request(**args)
        assert status == 200

    status, _ = await ci.process_request(**args)
    assert status == 429


@pytest.mark.asyncio
async def test_ip_rate_limit_isolates_per_ip():
    secret = "s"
    body = json.dumps({"phone": "+385900000000", "reasons": ["r"]}).encode()
    sig = _sign(body, secret)

    for _ in range(ci.IP_RATE_LIMIT):
        await ci.process_request(
            body=body, signature=sig,
            declared_content_length=str(len(body)),
            client_ip="10.0.0.A", secret=secret, stores={},
        )
    status, _ = await ci.process_request(
        body=body, signature=sig,
        declared_content_length=str(len(body)),
        client_ip="10.0.0.B", secret=secret, stores={},
    )
    assert status == 200


@pytest.mark.asyncio
async def test_reasons_truncated_to_10(monkeypatch):
    captured: dict = {}

    orig = ci.handle_invalidation_event

    async def spy(event, **kwargs):
        captured["event"] = event
        return await orig(event, **kwargs)

    monkeypatch.setattr(ci, "handle_invalidation_event", spy)

    secret = "s"
    body = json.dumps({
        "phone": "+385912345678",
        "reasons": [f"r{i}" for i in range(20)],
    }).encode()
    sig = _sign(body, secret)

    status, _ = await ci.process_request(
        body=body, signature=sig,
        declared_content_length=str(len(body)),
        client_ip="10.0.0.10", secret=secret, stores={},
    )
    assert status == 200
    assert len(captured["event"].reasons) == 10


@pytest.mark.asyncio
async def test_ip_check_window_expires():
    """After IP_RATE_WINDOW_S, prior requests roll out of the window."""
    now = time.time()
    # Pre-fill bucket with timestamps just outside the window
    old = now - (ci.IP_RATE_WINDOW_S + 1)
    for _ in range(ci.IP_RATE_LIMIT):
        ci._check_ip("10.0.0.W", now=old)

    # New request now should still be allowed (old entries expired)
    assert ci._check_ip("10.0.0.W", now=now) is True
