"""Viber Business Messages servis (Infobip) — Faza 0 multichannel.

Nasljeđuje WhatsAppService: retry/backoff (429 Retry-After, 5xx, timeout),
UTF-8/type guarde, ErrorCode mapiranje, klijent lifecycle i brojače — sve je
Infobip-platformski i kanal-neutralno. Ovaj modul mijenja SAMO ono što je
Viber-specifično:

  - endpoint:  POST https://{INFOBIP_BASE_URL}/viber/2/messages
  - payload:   {"messages": [{"sender": <IME>, "destinations": [{"to": ...}],
                "content": {"text": ..., "type": "TEXT"}}]}
  - sender:    settings.VIBER_SENDER — registrirano IME sendera kod Infobipa,
               ne telefonski broj (zato zaseban setting, ne INFOBIP_SENDER_NUMBER)
  - limit:     1000 znakova po tekstualnoj poruci (WhatsApp: 4096)

Response parsing je zajednički: uspjeh se određuje HTTP statusom (200/201),
a messageId ekstrakcija u bazi već pokriva Viberov messages[] oblik.
Auth je identičan ("App {INFOBIP_API_KEY}").

Živa verifikacija payload oblika čeka registraciju Viber sendera (Infobip
sandbox) — do tada je ugovor pinan testovima u tests/test_viber_service.py.
"""

from typing import Any, Dict, Optional

from config import get_settings
from services.whatsapp_service import SendResult, WhatsAppService  # noqa: F401 — SendResult re-export za pozivatelje


def _get_settings():
    # Lokalna indirekcija (isti pattern kao whatsapp_service) da testovi
    # mogu patchati services.viber_service._get_settings.
    return get_settings()


class ViberService(WhatsAppService):
    """Infobip Viber Business Messages — TEXT poruke."""

    MAX_MESSAGE_LENGTH = 1000              # Viber BM tekst limit (WA: 4096)
    ENDPOINT_PATH = "/viber/2/messages"
    SPAN_NAME = "viber_service.send"

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        sender_name: Optional[str] = None,
    ):
        # sender_name (registrirano ime) putuje kroz postojeći
        # sender_number slot baze — payload ga čita iz self.sender_number.
        super().__init__(api_key=api_key, base_url=base_url,
                         sender_number=sender_name)

    def _default_sender(self) -> Optional[str]:
        return _get_settings().VIBER_SENDER

    def build_payload(
        self,
        to: str,
        text: str,
        from_number: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Viber BM messages[] wrapper (za razliku od WA single-message oblika)."""
        return {
            "messages": [
                {
                    "sender": from_number or self.sender_number,
                    "destinations": [{"to": to}],
                    "content": {"text": text, "type": "TEXT"},
                }
            ]
        }

    def _payload_recipient(self, payload: Dict[str, Any]) -> str:
        return payload["messages"][0]["destinations"][0]["to"]
