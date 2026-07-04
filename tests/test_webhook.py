"""
Test webhook_simple.py - Infobip message parsing.

Verifies that all Infobip payload formats are correctly parsed
and messages reach the Redis stream.
"""

import json
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

try:
    from fastapi.testclient import TestClient
except Exception:
    pytest.skip("fastapi.testclient not available", allow_module_level=True)


@pytest.fixture
def mock_redis_client():
    """Mock Redis for webhook tests."""
    redis = AsyncMock()
    redis.xadd = AsyncMock(return_value="1700000000000-0")
    return redis


@pytest.fixture
def client(mock_redis_client):
    """FastAPI test client with mocked Redis."""
    with patch("webhook_simple.get_redis", return_value=mock_redis_client):
        # Need to patch settings to avoid requiring env vars
        mock_settings = MagicMock()
        mock_settings.VERIFY_WHATSAPP_SIGNATURE = False
        mock_settings.REDIS_URL = "redis://localhost:6379/0"
        mock_settings.WHATSAPP_VERIFY_TOKEN = None

        with patch("webhook_simple.settings", mock_settings):
            from webhook_simple import router
            from fastapi import FastAPI

            app = FastAPI()
            app.include_router(router, prefix="/webhook")
            yield TestClient(app)


# ============================================================================
# INFOBIP FORMAT: "message" object (standard WhatsApp incoming)
# ============================================================================

class TestInfobipMessageFormat:
    """Standard Infobip WhatsApp format with "from" and "message" object."""

    def test_standard_text_message(self, client, mock_redis_client):
        """Infobip standard: from + message.text"""
        payload = {
            "results": [{
                "from": "385991234567",
                "to": "385916789012",
                "integrationType": "WHATSAPP",
                "receivedAt": "2024-01-01T12:00:00.000+0000",
                "messageId": "ABGGFlA5FIMlAgo-sKD87hgxPHMf",
                "message": {
                    "type": "TEXT",
                    "text": "Bok, trebam rezervirati vozilo"
                },
                "contact": {"name": "Test User"},
                "price": {"pricePerMessage": 0, "currency": "EUR"}
            }],
            "messageCount": 1,
            "pendingMessageCount": 0
        }

        response = client.post("/webhook/whatsapp", json=payload)

        assert response.status_code == 200
        mock_redis_client.xadd.assert_called_once()
        call_args = mock_redis_client.xadd.call_args
        stream_name = call_args[0][0]
        stream_data = call_args[0][1]

        assert stream_name == "whatsapp_stream_inbound"
        assert stream_data["sender"] == "385991234567"
        assert stream_data["text"] == "Bok, trebam rezervirati vozilo"
        assert stream_data["message_id"] == "ABGGFlA5FIMlAgo-sKD87hgxPHMf"

    def test_message_with_plus_prefix(self, client, mock_redis_client):
        """Infobip may send phone with + prefix."""
        payload = {
            "results": [{
                "from": "+385991234567",
                "messageId": "msg-001",
                "message": {"type": "TEXT", "text": "Test poruka"}
            }]
        }

        response = client.post("/webhook/whatsapp", json=payload)

        assert response.status_code == 200
        stream_data = mock_redis_client.xadd.call_args[0][1]
        assert stream_data["sender"] == "+385991234567"
        assert stream_data["text"] == "Test poruka"


# ============================================================================
# INFOBIP FORMAT: "content" array
# ============================================================================

class TestInfobipContentArrayFormat:
    """Infobip Messages API format with "content" as array."""

    def test_content_as_list(self, client, mock_redis_client):
        """Content is array of objects."""
        payload = {
            "results": [{
                "from": "385991234567",
                "messageId": "msg-002",
                "content": [
                    {"type": "TEXT", "text": "Poruka iz content arraya"}
                ]
            }]
        }

        response = client.post("/webhook/whatsapp", json=payload)

        assert response.status_code == 200
        stream_data = mock_redis_client.xadd.call_args[0][1]
        assert stream_data["text"] == "Poruka iz content arraya"

    def test_content_as_dict(self, client, mock_redis_client):
        """Content is single object (not array)."""
        payload = {
            "results": [{
                "from": "385991234567",
                "messageId": "msg-003",
                "content": {"type": "TEXT", "text": "Poruka iz content objekta"}
            }]
        }

        response = client.post("/webhook/whatsapp", json=payload)

        assert response.status_code == 200
        stream_data = mock_redis_client.xadd.call_args[0][1]
        assert stream_data["text"] == "Poruka iz content objekta"


# ============================================================================
# INFOBIP FORMAT: "sender" field (older API)
# ============================================================================

class TestInfobipSenderFallback:
    """Older Infobip format using "sender" instead of "from"."""

    def test_sender_field_fallback(self, client, mock_redis_client):
        """Falls back to "sender" when "from" is missing."""
        payload = {
            "results": [{
                "sender": "385991234567",
                "messageId": "msg-004",
                "message": {"type": "TEXT", "text": "Stariji format"}
            }]
        }

        response = client.post("/webhook/whatsapp", json=payload)

        assert response.status_code == 200
        stream_data = mock_redis_client.xadd.call_args[0][1]
        assert stream_data["sender"] == "385991234567"
        assert stream_data["text"] == "Stariji format"


# ============================================================================
# EDGE CASES: delivery reports, non-text, missing fields
# ============================================================================

class TestEdgeCases:
    """Edge cases that should be handled gracefully."""

    def test_delivery_report_no_sender(self, client, mock_redis_client):
        """Delivery reports have no 'from' - should be skipped."""
        payload = {
            "results": [{
                "messageId": "msg-005",
                "to": "385991234567",
                "status": {"groupName": "DELIVERED"}
            }]
        }

        response = client.post("/webhook/whatsapp", json=payload)

        assert response.status_code == 200
        mock_redis_client.xadd.assert_not_called()

    def test_image_message_forwarded_as_non_text(self, client, mock_redis_client):
        """Image messages are forwarded to Redis with [NON_TEXT:IMAGE] marker."""
        payload = {
            "results": [{
                "from": "385991234567",
                "messageId": "msg-006",
                "message": {"type": "IMAGE", "url": "https://example.com/img.jpg"}
            }]
        }

        response = client.post("/webhook/whatsapp", json=payload)

        assert response.status_code == 200
        # Non-text messages ARE pushed to Redis (worker responds with "text only" message)
        mock_redis_client.xadd.assert_called_once()
        stream_data = mock_redis_client.xadd.call_args[0][1]
        assert stream_data["text"] == "[NON_TEXT:IMAGE]"
        assert stream_data["sender"] == "385991234567"
        assert stream_data["original_type"] == "IMAGE"

    def test_empty_results(self, client, mock_redis_client):
        """Empty results array."""
        payload = {"results": []}

        response = client.post("/webhook/whatsapp", json=payload)

        assert response.status_code == 200
        mock_redis_client.xadd.assert_not_called()

    def test_no_results_key(self, client, mock_redis_client):
        """No results key in body."""
        payload = {"something": "else"}

        response = client.post("/webhook/whatsapp", json=payload)

        assert response.status_code == 200
        mock_redis_client.xadd.assert_not_called()

    def test_multiple_messages(self, client, mock_redis_client):
        """Multiple messages in single webhook."""
        payload = {
            "results": [
                {
                    "from": "385991111111",
                    "messageId": "msg-a",
                    "message": {"type": "TEXT", "text": "Prva poruka"}
                },
                {
                    "from": "385992222222",
                    "messageId": "msg-b",
                    "message": {"type": "TEXT", "text": "Druga poruka"}
                }
            ]
        }

        response = client.post("/webhook/whatsapp", json=payload)

        assert response.status_code == 200
        assert mock_redis_client.xadd.call_count == 2

    def test_direct_text_field(self, client, mock_redis_client):
        """Some integrations put text directly on result."""
        payload = {
            "results": [{
                "from": "385991234567",
                "messageId": "msg-007",
                "text": "Direktni text field"
            }]
        }

        response = client.post("/webhook/whatsapp", json=payload)

        assert response.status_code == 200
        stream_data = mock_redis_client.xadd.call_args[0][1]
        assert stream_data["text"] == "Direktni text field"


# ============================================================================
# CHANNEL TAG (Faza 0 multichannel: WhatsApp + Viber)
# ============================================================================

class TestChannelTag:
    """Svaki stream entry nosi channel tag — worker po njemu bira outbound
    servis. Kanal: integrationType iz payloada kad postoji, inače default
    rute (/whatsapp → whatsapp, /viber → viber)."""

    def test_whatsapp_route_tags_whatsapp(self, client, mock_redis_client):
        payload = {"results": [{
            "from": "385991234567", "messageId": "ch-1",
            "integrationType": "WHATSAPP",
            "message": {"type": "TEXT", "text": "Bok"},
        }]}
        assert client.post("/webhook/whatsapp", json=payload).status_code == 200
        assert mock_redis_client.xadd.call_args[0][1]["channel"] == "whatsapp"

    def test_viber_route_tags_viber_without_integration_type(
        self, client, mock_redis_client,
    ):
        """Alias ruta /viber: kanal determinističan i ako Infobip Viber
        payload ne nosi integrationType. Isti stream, isti parser."""
        payload = {"results": [{
            "from": "385991234567", "messageId": "ch-2",
            "message": {"type": "TEXT", "text": "Bok s Vibera"},
        }]}
        assert client.post("/webhook/viber", json=payload).status_code == 200
        assert mock_redis_client.xadd.call_args[0][0] == "whatsapp_stream_inbound"
        stream_data = mock_redis_client.xadd.call_args[0][1]
        assert stream_data["channel"] == "viber"
        assert stream_data["text"] == "Bok s Vibera"

    def test_integration_type_wins_over_route_default(
        self, client, mock_redis_client,
    ):
        """Payload je izvor istine: VIBER integrationType i na /whatsapp
        ruti daje channel viber (pogrešno usmjerena subscription)."""
        payload = {"results": [{
            "from": "385991234567", "messageId": "ch-3",
            "integrationType": "VIBER",
            "message": {"type": "TEXT", "text": "Cross"},
        }]}
        client.post("/webhook/whatsapp", json=payload)
        assert mock_redis_client.xadd.call_args[0][1]["channel"] == "viber"

    def test_unknown_integration_falls_back_to_route_default(
        self, client, mock_redis_client,
    ):
        payload = {"results": [{
            "from": "385991234567", "messageId": "ch-4",
            "integrationType": "SMS",
            "message": {"type": "TEXT", "text": "X"},
        }]}
        client.post("/webhook/whatsapp", json=payload)
        assert mock_redis_client.xadd.call_args[0][1]["channel"] == "whatsapp"

    def test_non_text_viber_also_tagged(self, client, mock_redis_client):
        """Non-text put gradi ZASEBAN stream_data — i on mora nositi kanal
        (worker vraća 'samo tekst' poruku kroz pravi kanal)."""
        payload = {"results": [{
            "from": "385991234567", "messageId": "ch-5",
            "message": {"type": "IMAGE", "url": "https://x/i.jpg"},
        }]}
        client.post("/webhook/viber", json=payload)
        stream_data = mock_redis_client.xadd.call_args[0][1]
        assert stream_data["channel"] == "viber"
        assert stream_data["text"] == "[NON_TEXT:IMAGE]"


# ============================================================================
# WEBHOOK VERIFICATION
# ============================================================================

class TestWebhookVerification:
    """GET /webhook/whatsapp verification endpoint."""

    def test_verification_no_token_configured(self, client):
        """Without WHATSAPP_VERIFY_TOKEN, returns OK (Infobip reachability check)."""
        response = client.get("/webhook/whatsapp")

        assert response.status_code == 200
        assert response.text == "ok"

    def test_verification_without_mode_returns_active(self, client):
        """Query params are ignored — endpoint is a plain reachability check."""
        response = client.get("/webhook/whatsapp?hub.challenge=123456")

        assert response.status_code == 200
        assert response.text == "ok"
