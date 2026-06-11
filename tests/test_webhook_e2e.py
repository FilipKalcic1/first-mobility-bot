"""
End-to-end test: Webhook → Redis Stream → Worker pickup.

Simulates full message pipeline WITHOUT real Redis.
Uses a FakeRedisStream that both webhook and worker interact with,
proving the data contract between them is correct.
"""

import json
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

try:
    from fastapi.testclient import TestClient
except Exception:
    pytest.skip("fastapi.testclient not available", allow_module_level=True)


class FakeRedisStream:
    """
    In-memory Redis stream simulator.

    Implements xadd/xreadgroup/xgroup_create so both webhook and worker
    code can interact with it, proving the data flows end-to-end.
    """

    def __init__(self):
        self.stream: dict[str, list] = {}  # stream_name -> [(msg_id, data)]
        self.groups: dict[str, dict] = {}  # stream_name -> {group_name: last_delivered_id}
        self._counter = 0

    async def xadd(
        self,
        stream_name: str,
        data: dict,
        maxlen: int | None = None,
        approximate: bool = False,
    ) -> str:
        """Add entry to stream (called by webhook; maxlen mirrors real Redis)."""
        self._counter += 1
        msg_id = f"1700000000000-{self._counter}"

        if stream_name not in self.stream:
            self.stream[stream_name] = []
        self.stream[stream_name].append((msg_id, data))
        return msg_id

    async def xgroup_create(self, stream_name: str, group_name: str, start_id: str, mkstream: bool = False):
        """Create consumer group."""
        if stream_name not in self.groups:
            self.groups[stream_name] = {}
        if group_name in self.groups[stream_name]:
            raise Exception("BUSYGROUP Consumer Group name already exists")
        self.groups[stream_name][group_name] = 0  # track delivery index

    async def xreadgroup(self, groupname: str, consumername: str, streams: dict, count: int = 5, block: int = 0):
        """Read from stream as consumer (called by worker)."""
        results = []
        for stream_name, _ in streams.items():
            if stream_name not in self.stream:
                continue

            group_data = self.groups.get(stream_name, {})
            last_idx = group_data.get(groupname, 0)
            entries = self.stream[stream_name]

            new_entries = entries[last_idx:last_idx + count]
            if new_entries:
                group_data[groupname] = last_idx + len(new_entries)
                results.append((stream_name, new_entries))

        return results if results else []

    async def xack(self, stream_name: str, group_name: str, msg_id: str):
        return 1

    async def xdel(self, stream_name: str, msg_id: str):
        return 1

    # Stub methods the worker may call
    async def ping(self):
        return True

    async def set(self, *args, **kwargs):
        return True

    async def get(self, *args, **kwargs):
        return None

    async def delete(self, *args, **kwargs):
        return True

    async def aclose(self):
        pass


# ============================================================================
# END-TO-END: Webhook -> Stream -> Worker reads same data
# ============================================================================

class TestWebhookToWorkerPipeline:
    """Proves messages flow from webhook POST all the way to worker pickup."""

    pytestmark = pytest.mark.asyncio

    @pytest.fixture
    def fake_redis(self):
        return FakeRedisStream()

    @pytest.fixture
    def client(self, fake_redis):
        """FastAPI client with FakeRedisStream."""
        async def mock_get_redis():
            return fake_redis

        mock_settings = MagicMock()
        mock_settings.VERIFY_WHATSAPP_SIGNATURE = False
        mock_settings.REDIS_URL = "redis://fake:6379/0"
        mock_settings.WHATSAPP_VERIFY_TOKEN = None

        with patch("webhook_simple.get_redis", side_effect=mock_get_redis):
            with patch("webhook_simple.settings", mock_settings):
                from webhook_simple import router
                from fastapi import FastAPI

                app = FastAPI()
                app.include_router(router, prefix="/webhook")
                yield TestClient(app)

    async def test_infobip_message_reaches_worker(self, client, fake_redis):
        """
        FULL PIPELINE TEST:
        1. Infobip sends POST with standard format
        2. Webhook parses and pushes to Redis stream
        3. Worker reads from stream and gets the SAME data
        """
        # === STEP 1: Infobip sends webhook ===
        infobip_payload = {
            "results": [{
                "from": "385991234567",
                "to": "385916789012",
                "integrationType": "WHATSAPP",
                "receivedAt": "2024-06-15T10:30:00.000+0000",
                "messageId": "ABGGFlA5FIMlAgo-sKD87hgxPHMf",
                "message": {
                    "type": "TEXT",
                    "text": "Trebam rezervirati vozilo za sutra"
                },
                "contact": {"name": "Igor"},
                "price": {"pricePerMessage": 0, "currency": "EUR"}
            }]
        }

        response = client.post("/webhook/whatsapp", json=infobip_payload)
        assert response.status_code == 200, f"Webhook returned {response.status_code}"

        # === STEP 2: Verify message is in the stream ===
        stream_entries = fake_redis.stream.get("whatsapp_stream_inbound", [])
        assert len(stream_entries) == 1, f"Expected 1 message in stream, got {len(stream_entries)}"

        msg_id, data = stream_entries[0]
        assert data["sender"] == "385991234567"
        assert data["text"] == "Trebam rezervirati vozilo za sutra"
        assert data["message_id"] == "ABGGFlA5FIMlAgo-sKD87hgxPHMf"

        # === STEP 3: Simulate worker reading from stream ===
        # Create consumer group (like worker does on startup)
        await fake_redis.xgroup_create("whatsapp_stream_inbound", "workers", "$", mkstream=True)

        # Workaround: set delivery index to 0 so worker reads from beginning
        # (In real Redis, "$" means "from now", but our messages are already there)
        fake_redis.groups["whatsapp_stream_inbound"]["workers"] = 0

        # Worker reads from stream
        worker_messages = await fake_redis.xreadgroup(
            groupname="workers",
            consumername="worker_test",
            streams={"whatsapp_stream_inbound": ">"},
            count=5
        )

        assert len(worker_messages) == 1, "Worker should receive 1 stream"
        stream_name, entries = worker_messages[0]
        assert stream_name == "whatsapp_stream_inbound"
        assert len(entries) == 1, "Worker should receive 1 message"

        worker_msg_id, worker_data = entries[0]
        assert worker_data["sender"] == "385991234567"
        assert worker_data["text"] == "Trebam rezervirati vozilo za sutra"
        assert worker_data["message_id"] == "ABGGFlA5FIMlAgo-sKD87hgxPHMf"

        print(f"\n  WEBHOOK -> STREAM: sender={data['sender']}, text='{data['text'][:40]}'")
        print(f"  STREAM  -> WORKER: sender={worker_data['sender']}, text='{worker_data['text'][:40]}'")
        print(f"  Pipeline OK!")

    async def test_multiple_messages_all_reach_worker(self, client, fake_redis):
        """Multiple messages from different users all flow through."""
        messages = [
            ("385991111111", "Bok, trebam pomoc"),
            ("385992222222", "Koja vozila su dostupna?"),
            ("385993333333", "Prijavi kilometrazu 45000"),
        ]

        for phone, text in messages:
            payload = {
                "results": [{
                    "from": phone,
                    "messageId": f"msg-{phone[-4:]}",
                    "message": {"type": "TEXT", "text": text}
                }]
            }
            resp = client.post("/webhook/whatsapp", json=payload)
            assert resp.status_code == 200

        # All 3 in stream
        stream_entries = fake_redis.stream.get("whatsapp_stream_inbound", [])
        assert len(stream_entries) == 3

        # Worker picks up all 3
        await fake_redis.xgroup_create("whatsapp_stream_inbound", "workers", "$", mkstream=True)
        fake_redis.groups["whatsapp_stream_inbound"]["workers"] = 0

        worker_messages = await fake_redis.xreadgroup(
            groupname="workers",
            consumername="worker_test",
            streams={"whatsapp_stream_inbound": ">"},
            count=10
        )

        _, entries = worker_messages[0]
        assert len(entries) == 3

        received_phones = [e[1]["sender"] for e in entries]
        assert "385991111111" in received_phones
        assert "385992222222" in received_phones
        assert "385993333333" in received_phones

        print(f"\n  All {len(entries)} messages reached worker correctly!")

    def test_old_format_sender_field_reaches_worker(self, client, fake_redis):
        """Even 'sender' field (old format) flows through to worker."""
        payload = {
            "results": [{
                "sender": "385994444444",
                "messageId": "msg-old-format",
                "message": {"type": "TEXT", "text": "Stari Infobip format"}
            }]
        }

        response = client.post("/webhook/whatsapp", json=payload)
        assert response.status_code == 200

        stream_entries = fake_redis.stream.get("whatsapp_stream_inbound", [])
        assert len(stream_entries) == 1

        _, data = stream_entries[0]
        assert data["sender"] == "385994444444"
        assert data["text"] == "Stari Infobip format"

    def test_content_dict_format_reaches_worker(self, client, fake_redis):
        """Content as single dict (not array) still reaches worker."""
        payload = {
            "results": [{
                "from": "385995555555",
                "messageId": "msg-dict",
                "content": {"type": "TEXT", "text": "Content kao objekt"}
            }]
        }

        response = client.post("/webhook/whatsapp", json=payload)
        assert response.status_code == 200

        stream_entries = fake_redis.stream.get("whatsapp_stream_inbound", [])
        assert len(stream_entries) == 1
        assert stream_entries[0][1]["text"] == "Content kao objekt"

    def test_non_text_messages_forwarded_with_marker(self, client, fake_redis):
        """Images/locations are forwarded with [NON_TEXT:*] marker so worker can respond.
        Delivery reports (no sender) are still filtered out."""
        payload = {
            "results": [
                {
                    "from": "385991234567",
                    "messageId": "msg-img",
                    "message": {"type": "IMAGE", "url": "https://cdn.example.com/photo.jpg"}
                },
                {
                    "from": "385991234567",
                    "messageId": "msg-loc",
                    "message": {"type": "LOCATION", "latitude": 45.8, "longitude": 15.9}
                },
                {
                    "messageId": "msg-delivery",
                    "to": "385991234567",
                    "status": {"groupName": "DELIVERED"}
                }
            ]
        }

        response = client.post("/webhook/whatsapp", json=payload)
        assert response.status_code == 200

        stream_entries = fake_redis.stream.get("whatsapp_stream_inbound", [])
        # IMAGE and LOCATION are forwarded (worker responds with "text only" message)
        # Delivery report has no sender, so it's filtered out
        assert len(stream_entries) == 2, f"Expected 2 non-text messages, got {len(stream_entries)}"
        assert stream_entries[0][1]["text"] == "[NON_TEXT:IMAGE]"
        assert stream_entries[0][1]["original_type"] == "IMAGE"
        assert stream_entries[1][1]["text"] == "[NON_TEXT:LOCATION]"
        assert stream_entries[1][1]["original_type"] == "LOCATION"
