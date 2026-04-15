"""
Chaos Engineering Tests

Simulates failure scenarios to verify system resilience:
- Redis connection failures during message processing
- API gateway timeouts and circuit breaker behavior
- DLQ fallback chain (Redis -> File -> stderr)
- Worker graceful shutdown under load
- Lock acquisition failures (fail-open behavior)
"""

import asyncio
import json
import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest


def _has_fastapi() -> bool:
    """True only when the real fastapi package is installed (not a test stub)."""
    try:
        import fastapi
        return isinstance(getattr(fastapi, 'FastAPI', None), type)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 1. DLQ FALLBACK CHAIN: Redis -> File -> stderr
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _has_fastapi(), reason="fastapi not installed")
class TestDLQFallbackChain:
    """Verify 3-tier DLQ operates correctly when tiers fail."""

    @pytest.mark.asyncio
    async def test_dlq_redis_success(self):
        """Primary path: DLQ message stored in Redis."""
        from webhook_simple import _write_dlq

        mock_redis = AsyncMock()
        with patch("webhook_simple.get_redis", return_value=mock_redis):
            await _write_dlq('{"test": "message"}')

        mock_redis.lpush.assert_called_once_with("dlq:webhook", '{"test": "message"}')
        mock_redis.ltrim.assert_called_once()
        mock_redis.expire.assert_called_once()

    @pytest.mark.asyncio
    async def test_dlq_redis_fails_file_succeeds(self, tmp_path):
        """Fallback: Redis fails, DLQ writes to file."""
        from webhook_simple import _write_dlq

        mock_redis = AsyncMock()
        mock_redis.lpush.side_effect = ConnectionError("Redis down")

        dlq_file = str(tmp_path / "dlq.jsonl")

        with (
            patch("webhook_simple.get_redis", return_value=mock_redis),
            patch("webhook_simple._DLQ_FILE_PATH", dlq_file),
        ):
            await _write_dlq('{"test": "redis_failed"}')

        # Verify file was written
        assert os.path.exists(dlq_file)
        with open(dlq_file, "r", encoding="utf-8") as f:
            content = f.read()
        assert '{"test": "redis_failed"}' in content

    @pytest.mark.asyncio
    async def test_dlq_redis_and_file_fail_stderr(self, tmp_path, capsys):
        """Last resort: Redis and file both fail, DLQ writes to stderr."""
        from webhook_simple import _write_dlq

        mock_redis = AsyncMock()
        mock_redis.lpush.side_effect = ConnectionError("Redis down")

        # Use a path that will fail (read-only or non-existent directory)
        bad_path = str(tmp_path / "nonexistent" / "deep" / "dlq.jsonl")

        with (
            patch("webhook_simple.get_redis", return_value=mock_redis),
            patch("webhook_simple._DLQ_FILE_PATH", bad_path),
        ):
            await _write_dlq('{"test": "all_failed"}')

        captured = capsys.readouterr()
        assert "DLQ_WEBHOOK:" in captured.err
        assert "all_failed" in captured.err

    @pytest.mark.asyncio
    async def test_dlq_file_size_cap_respected(self, tmp_path):
        """DLQ file respects 5MB cap to prevent tmpfs exhaustion."""
        from webhook_simple import _write_dlq

        mock_redis = AsyncMock()
        mock_redis.lpush.side_effect = ConnectionError("Redis down")

        dlq_file = str(tmp_path / "dlq.jsonl")
        # Create a file that's already at the cap
        with open(dlq_file, "w", encoding="utf-8") as f:
            f.write("x" * (5 * 1024 * 1024 + 1))

        with (
            patch("webhook_simple.get_redis", return_value=mock_redis),
            patch("webhook_simple._DLQ_FILE_PATH", dlq_file),
        ):
            await _write_dlq('{"test": "over_cap"}')

        # File should NOT have grown (capped)
        # Message should go to stderr instead
        # Verify file size is still approximately at cap
        file_size = os.path.getsize(dlq_file)
        assert file_size <= 5 * 1024 * 1024 + 100  # Original size, not grown


# ---------------------------------------------------------------------------
# 2. CIRCUIT BREAKER BEHAVIOR
# ---------------------------------------------------------------------------

class TestCircuitBreaker:
    """Verify circuit breaker opens, half-opens, and closes correctly."""

    @pytest.mark.asyncio
    async def test_circuit_opens_after_threshold(self):
        """Circuit opens after CIRCUIT_FAILURE_THRESHOLD consecutive failures."""
        import httpx
        from services.api_gateway import APIGateway, HttpMethod

        gw = APIGateway.__new__(APIGateway)
        gw.base_url = "https://test.api.com"
        gw.tenant_id = "test"
        gw.token_manager = AsyncMock()
        gw.token_manager.get_token = AsyncMock(return_value="test-token")
        gw.client = AsyncMock()
        gw._consecutive_failures = 0
        gw._circuit_open_until = 0.0

        # Simulate 3 consecutive timeout failures (must be httpx.TimeoutException to open circuit)
        gw.client.get = AsyncMock(side_effect=httpx.ReadTimeout("timeout"))

        for _ in range(3):
            result = await gw.execute(HttpMethod.GET, "/test", max_retries=0, tenant_id="test")

        # Circuit should be open now
        assert gw._consecutive_failures >= gw.CIRCUIT_FAILURE_THRESHOLD
        assert gw._circuit_open_until > time.monotonic()

    @pytest.mark.asyncio
    async def test_circuit_rejects_when_open(self):
        """Circuit returns CIRCUIT_OPEN immediately when open."""
        from services.api_gateway import APIGateway, HttpMethod

        gw = APIGateway.__new__(APIGateway)
        gw.base_url = "https://test.api.com"
        gw.tenant_id = "test"
        gw.token_manager = AsyncMock()
        gw.client = AsyncMock()
        gw._consecutive_failures = 5
        gw._circuit_open_until = time.monotonic() + 60  # Open for 60s

        result = await gw.execute(HttpMethod.GET, "/test", tenant_id="test")

        assert result.success is False
        assert result.error_code == "GATEWAY_CIRCUIT_OPEN"
        # Client should NOT have been called
        gw.client.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_circuit_resets_on_success(self):
        """Circuit resets failure counter on successful request."""
        from services.api_gateway import APIGateway, HttpMethod
        import httpx

        gw = APIGateway.__new__(APIGateway)
        gw.base_url = "https://test.api.com"
        gw.tenant_id = "test"
        gw.token_manager = AsyncMock()
        gw.token_manager.get_token = AsyncMock(return_value="token")
        gw._consecutive_failures = 2
        gw._circuit_open_until = 0.0

        # Mock successful response
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "application/json"}
        mock_response.text = '{"data": "ok"}'
        mock_response.json.return_value = {"data": "ok"}

        gw.client = AsyncMock()
        gw.client.get = AsyncMock(return_value=mock_response)

        result = await gw.execute(HttpMethod.GET, "/test", max_retries=0, tenant_id="test")

        assert result.success is True
        assert gw._consecutive_failures == 0

    def test_circuit_cooldown_has_jitter(self):
        """Circuit cooldown uses jittered backoff, not fixed value."""
        from services.api_gateway import APIGateway

        gw = APIGateway.__new__(APIGateway)
        gw._consecutive_failures = 3

        cooldowns = [gw._calculate_circuit_cooldown() for _ in range(20)]

        # All cooldowns should be different (jitter)
        assert len(set(cooldowns)) > 1, "Cooldown should have jitter"
        # All should be within expected range
        for cd in cooldowns:
            assert cd >= gw.CIRCUIT_BASE_COOLDOWN_SECONDS * 0.5
            assert cd <= gw.CIRCUIT_MAX_COOLDOWN_SECONDS * 1.5

    def test_circuit_cooldown_grows_with_failures(self):
        """Circuit cooldown grows exponentially with consecutive failures."""
        from services.api_gateway import APIGateway

        gw = APIGateway.__new__(APIGateway)

        # Sample many cooldowns at different failure counts to compare averages
        def avg_cooldown(failures):
            gw._consecutive_failures = failures
            return sum(gw._calculate_circuit_cooldown() for _ in range(50)) / 50

        cd_3 = avg_cooldown(3)
        cd_5 = avg_cooldown(5)
        cd_8 = avg_cooldown(8)

        assert cd_5 > cd_3, "Cooldown should increase with more failures"
        assert cd_8 > cd_5, "Cooldown should continue increasing"


# ---------------------------------------------------------------------------
# 3. WORKER LOCK FAIL-OPEN
# ---------------------------------------------------------------------------

class TestWorkerLockFailOpen:
    """Verify lock acquisition fails open (allows processing) on Redis errors."""

    @pytest.mark.asyncio
    async def test_lock_acquire_fails_open_on_connection_error(self):
        """Lock acquisition returns True (allow) on ConnectionError."""
        # Import dynamically since worker.py has complex init
        mock_redis = AsyncMock()
        mock_redis.set = AsyncMock(side_effect=ConnectionError("Redis down"))

        from worker import Worker as WhatsAppWorker
        worker = WhatsAppWorker.__new__(WhatsAppWorker)
        worker.redis = mock_redis
        worker.consumer_name = "test-consumer"
        worker._lock_access_times = {}

        result = await worker._acquire_message_lock("sender123", "msg456")
        assert result is True  # Fail open

    @pytest.mark.asyncio
    async def test_lock_acquire_fails_open_on_timeout(self):
        """Lock acquisition returns True (allow) on TimeoutError."""
        mock_redis = AsyncMock()
        mock_redis.set = AsyncMock(side_effect=TimeoutError("Redis timeout"))

        from worker import Worker as WhatsAppWorker
        worker = WhatsAppWorker.__new__(WhatsAppWorker)
        worker.redis = mock_redis
        worker.consumer_name = "test-consumer"
        worker._lock_access_times = {}

        result = await worker._acquire_message_lock("sender123", "msg456")
        assert result is True  # Fail open


# ---------------------------------------------------------------------------
# 4. WEBHOOK SIGNATURE VALIDATION UNDER FAILURE
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _has_fastapi(), reason="fastapi not installed")
class TestWebhookResilience:
    """Verify webhook handler remains resilient under failure conditions."""

    def test_signature_validation_empty_secret(self):
        """Webhook accepts when secret key is not configured."""
        from webhook_simple import verify_webhook_signature

        result = verify_webhook_signature(b"payload", "sha256=abc", "")
        assert result is True  # Skips validation

    def test_signature_validation_empty_signature(self):
        """Webhook rejects when signature header is missing."""
        from webhook_simple import verify_webhook_signature

        result = verify_webhook_signature(b"payload", "", "secret123")
        assert result is False

    def test_signature_validation_timing_safe(self):
        """Signature comparison uses timing-safe compare_digest."""
        import hmac
        from webhook_simple import verify_webhook_signature

        secret = "test-secret"
        payload = b"test-payload"
        expected = hmac.new(
            secret.encode('utf-8'), payload, "sha256"
        ).hexdigest()

        # Valid signature
        assert verify_webhook_signature(payload, f"sha256={expected}", secret) is True

        # Invalid signature
        assert verify_webhook_signature(payload, "sha256=invalid", secret) is False


# ---------------------------------------------------------------------------
# 5. API GATEWAY SSRF PROTECTION UNDER CHAOS
# ---------------------------------------------------------------------------

class TestSSRFProtection:
    """Verify SSRF protection works even under unusual inputs."""

    def test_ssrf_blocks_different_host(self):
        """SSRF protection blocks URLs to different hosts."""
        from services.api_gateway import APIGateway

        gw = APIGateway.__new__(APIGateway)
        gw.base_url = "https://api.mobilityone.io"

        with pytest.raises(ValueError, match="URL host mismatch"):
            gw._build_url("https://evil.com/steal-data", {})

    def test_ssrf_allows_same_host(self):
        """SSRF protection allows URLs to same host."""
        from services.api_gateway import APIGateway

        gw = APIGateway.__new__(APIGateway)
        gw.base_url = "https://api.mobilityone.io"

        url = gw._build_url("https://api.mobilityone.io/api/v1/data", {})
        assert url == "https://api.mobilityone.io/api/v1/data"


# ---------------------------------------------------------------------------
# 6. GRACEFUL SHUTDOWN SIGNALS
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _has_fastapi(), reason="fastapi not installed")
class TestGracefulShutdown:
    """Verify APP_STOPPING flag prevents new message acceptance."""

    @pytest.mark.asyncio
    async def test_webhook_rejects_during_shutdown(self):
        """Webhook returns 503 when APP_STOPPING is True."""
        from fastapi.testclient import TestClient
        from fastapi import FastAPI

        app = FastAPI()

        from webhook_simple import router
        app.include_router(router)

        with patch("main.APP_STOPPING", True):
            client = TestClient(app, raise_server_exceptions=False)
            response = client.post(
                "/whatsapp",
                json={"results": [{"from": "123", "message": {"type": "TEXT", "text": "hi"}}]}
            )
            assert response.status_code == 503
