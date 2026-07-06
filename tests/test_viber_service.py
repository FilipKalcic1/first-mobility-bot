"""Tests for ViberService (services/viber_service.py) — Faza 0 multichannel.

Pina Viber-specifični ugovor (endpoint, messages[] payload, sender=IME,
1000-znakovni limit) i KRITIČNO: da success-path parsira Viberov response
bez KeyError-a — prije hook ekstrakcije bi `payload['to']` u success logu
bacio KeyError NAKON isporuke i retry petlja bi slala duplikate.
Retry/backoff/error-mapping ponašanje je naslijeđeno i već pinano u
tests/test_whatsapp_service.py.
"""

import json
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from services.viber_service import ViberService
from services.whatsapp_service import WhatsAppService


# ---------------------------------------------------------------------------
# Fixtures (isti pattern kao test_whatsapp_service.py)
# ---------------------------------------------------------------------------

@pytest.fixture
def viber_service():
    """ViberService s test konfiguracijom (bez pravih Infobip poziva)."""
    with patch("services.whatsapp_service._get_settings", return_value=MagicMock(
        INFOBIP_API_KEY="test-api-key-1234567890",
        INFOBIP_BASE_URL="api.infobip.com",
        INFOBIP_SENDER_NUMBER="385991234567",
    )):
        svc = ViberService(
            api_key="test-api-key-1234567890",
            base_url="api.infobip.com",
            sender_name="MobilityOne",
        )
    return svc


def _mock_response(status_code: int, json_data: dict = None, headers: dict = None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.text = json.dumps(json_data or {})
    resp.headers = headers or {}
    return resp


def _viber_ok_response():
    """Stvarni oblik Infobip /viber/2/messages odgovora (messages[] wrapper)."""
    return _mock_response(200, {
        "messages": [{
            "messageId": "viber-msg-001",
            "destination": "385991234567",
            "status": {"groupId": 1, "groupName": "PENDING",
                       "id": 7, "name": "PENDING_ENROUTE"},
        }]
    })


# ===========================================================================
# Viber ugovor: URL, payload, auth
# ===========================================================================

class TestViberContract:

    @pytest.mark.asyncio
    async def test_send_uses_viber_endpoint_and_app_auth(self, viber_service):
        mock_client = AsyncMock()
        mock_client.post.return_value = _viber_ok_response()
        viber_service._client = mock_client

        await viber_service.send("385991234567", "Pozdrav!")

        call_args = mock_client.post.call_args
        url = call_args.args[0] if call_args.args else call_args.kwargs.get("url")
        headers = call_args.kwargs.get("headers") or call_args[1].get("headers")

        assert url == "https://api.infobip.com/viber/2/messages"
        assert headers["Authorization"] == "App test-api-key-1234567890"
        assert headers["Content-Type"] == "application/json"

    @pytest.mark.asyncio
    async def test_send_builds_messages_wrapper_payload(self, viber_service):
        """Viber BM payload: messages[] sa sender IMENOM, destinations[].to,
        content.type=TEXT — potpuno drugačiji oblik od WA single-message."""
        mock_client = AsyncMock()
        mock_client.post.return_value = _viber_ok_response()
        viber_service._client = mock_client

        await viber_service.send("385991234567", "Test poruka")

        call_args = mock_client.post.call_args
        sent = call_args.kwargs.get("json") or call_args[1].get("json")

        assert sent == {"messages": [{
            "sender": "MobilityOne",
            "destinations": [{"to": "385991234567"}],
            "content": {"text": "Test poruka", "type": "TEXT"},
        }]}

    @pytest.mark.asyncio
    async def test_success_path_survives_viber_payload_shape(self, viber_service):
        """REGRESIJA (mina iz seam analize): success log čita primatelja iz
        payloada — s WA hookom (`payload['to']`) bi Viber send nakon isporuke
        bacio KeyError, progutan u retry petlji → DUPLICIRANE poruke.
        Uspjeh mora vratiti success=True s messageId iz messages[] oblika."""
        mock_client = AsyncMock()
        mock_client.post.return_value = _viber_ok_response()
        viber_service._client = mock_client

        result = await viber_service.send("385991234567", "Bok")

        assert result.success is True
        assert result.message_id == "viber-msg-001"
        assert viber_service._messages_sent == 1
        assert mock_client.post.call_count == 1  # NE retry nakon uspjeha

    def test_payload_recipient_hook(self, viber_service):
        payload = viber_service.build_payload("385998877", "x")
        assert viber_service._payload_recipient(payload) == "385998877"

    def test_message_length_limit_is_viber_1000(self, viber_service):
        assert ViberService.MAX_MESSAGE_LENGTH == 1000
        assert WhatsAppService.MAX_MESSAGE_LENGTH == 4096  # WA netaknut

    @pytest.mark.asyncio
    async def test_long_text_truncated_to_viber_limit(self, viber_service):
        mock_client = AsyncMock()
        mock_client.post.return_value = _viber_ok_response()
        viber_service._client = mock_client

        await viber_service.send("385991234567", "x" * 5000)

        sent = mock_client.post.call_args.kwargs.get("json")
        text = sent["messages"][0]["content"]["text"]
        assert len(text) == 1000
        assert text.endswith("...")


# ===========================================================================
# Sender iz settingsa (VIBER_SENDER, ne INFOBIP_SENDER_NUMBER)
# ===========================================================================

class TestSenderSource:

    def test_default_sender_reads_viber_sender_setting(self):
        with patch("services.viber_service._get_settings", return_value=MagicMock(
            VIBER_SENDER="FleetBot",
        )), patch("services.whatsapp_service._get_settings", return_value=MagicMock(
            INFOBIP_API_KEY="k-1234567890",
            INFOBIP_BASE_URL="api.infobip.com",
            INFOBIP_SENDER_NUMBER="385000000000",  # NE smije završiti u payloadu
        )):
            svc = ViberService()
        assert svc.sender_number == "FleetBot"
        payload = svc.build_payload("385991111", "hej")
        assert payload["messages"][0]["sender"] == "FleetBot"

    def test_explicit_sender_name_wins(self, viber_service):
        assert viber_service.sender_number == "MobilityOne"


# ===========================================================================
# Naslijeđeno ponašanje (smoke — puni retry ugovor pinan u WA testovima)
# ===========================================================================

class TestInheritedBehavior:

    @pytest.mark.asyncio
    async def test_4xx_maps_to_error_code_without_retry(self, viber_service):
        mock_client = AsyncMock()
        mock_client.post.return_value = _mock_response(400, {
            "requestError": {"serviceException": {"text": "Invalid sender"}}
        })
        viber_service._client = mock_client

        result = await viber_service.send("385991234567", "Bok")

        assert result.success is False
        assert result.status_code == 400
        assert mock_client.post.call_count == 1  # 4xx se ne retrya
        assert "Invalid sender" in (result.error_message or "")

    def test_phone_validation_inherited(self, viber_service):
        ok, _, _ = viber_service.validate_phone_number("385991234567")
        assert ok is True
        bad, _, err = viber_service.validate_phone_number(
            "d9428888-122b-11e1-b85c-61cd3cbb3210")
        assert bad is False and err
