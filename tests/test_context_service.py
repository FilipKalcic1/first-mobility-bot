"""
Tests for ContextService - Pydantic models, validation, and context management.
"""

import pytest
import time
from unittest.mock import AsyncMock, MagicMock, patch

from services.context_service import (
    VehicleContext,
    UserContext,
    ContextService,
    UUID_PATTERN,
)


class TestUUIDPattern:
    def test_valid_uuid(self):
        assert UUID_PATTERN.match("12345678-1234-1234-1234-123456789abc")

    def test_invalid_uuid(self):
        assert UUID_PATTERN.match("not-a-uuid") is None

    def test_phone_number(self):
        assert UUID_PATTERN.match("+385912345678") is None


class TestVehicleContext:
    def test_create_from_alias(self):
        v = VehicleContext(Id="v-1", RegistrationNumber="ZG-1234-AB", Mileage=14000)
        assert v.id == "v-1"
        assert v.registration == "ZG-1234-AB"
        assert v.mileage == 14000

    def test_create_from_field_name(self):
        v = VehicleContext(id="v-1", registration="ZG-1234-AB")
        assert v.id == "v-1"
        assert v.registration == "ZG-1234-AB"

    def test_default_values(self):
        v = VehicleContext()
        assert v.id is None
        assert v.registration is None
        assert v.mileage is None
        assert v.raw == {}

    def test_raw_data(self):
        raw = {"CustomField": "value"}
        v = VehicleContext(raw=raw)
        assert v.raw == raw


class TestUserContext:
    def test_create_with_phone(self):
        ctx = UserContext(phone="+385912345678")
        assert ctx.phone == "+385912345678"
        assert ctx.display_name == "Korisnik"
        assert ctx.is_guest is False

    def test_guest_context(self):
        ctx = UserContext.guest("+385912345678")
        assert ctx.phone == "+385912345678"
        assert ctx.is_guest is True
        assert ctx.display_name == "Korisnik"
        assert ctx.cached_at is not None

    def test_from_dict_basic(self):
        data = {
            "person_id": "p-123",
            "phone": "+385912345678",
            "tenant_id": "t-1",
            "display_name": "Ivan",
            "is_guest": False,
        }
        ctx = UserContext.from_dict(data)
        assert ctx.person_id == "p-123"
        assert ctx.phone == "+385912345678"
        assert ctx.display_name == "Ivan"

    def test_from_dict_with_vehicle(self):
        data = {
            "phone": "+385912345678",
            "vehicle": {
                "Id": "v-1",
                "RegistrationNumber": "ZG-1234-AB",
                "Mileage": 14000,
            },
        }
        ctx = UserContext.from_dict(data)
        assert ctx.vehicle is not None
        assert ctx.vehicle.id == "v-1"

    def test_from_dict_without_vehicle(self):
        data = {"phone": "+385912345678"}
        ctx = UserContext.from_dict(data)
        assert ctx.vehicle is None

    def test_to_dict(self):
        ctx = UserContext(
            person_id="p-123",
            phone="+385912345678",
            tenant_id="t-1",
            display_name="Ivan",
            is_guest=False,
            cached_at=1000.0,
        )
        d = ctx.to_dict()
        assert d["person_id"] == "p-123"
        assert d["phone"] == "+385912345678"
        assert d["display_name"] == "Ivan"
        assert d["cached_at"] == 1000.0

    def test_to_dict_with_vehicle(self):
        vehicle = VehicleContext(raw={"Id": "v-1", "Brand": "VW"})
        ctx = UserContext(phone="+385912345678", vehicle=vehicle)
        d = ctx.to_dict()
        assert "vehicle" in d
        assert d["vehicle"]["Id"] == "v-1"

    def test_roundtrip(self):
        """to_dict -> from_dict should preserve data."""
        original = UserContext(
            person_id="p-123",
            phone="+385912345678",
            tenant_id="t-1",
            display_name="Ivan",
            is_guest=False,
            cached_at=time.time(),
        )
        restored = UserContext.from_dict(original.to_dict())
        assert restored.person_id == original.person_id
        assert restored.phone == original.phone
        assert restored.display_name == original.display_name


class TestContextServiceValidation:
    def _make_service(self):
        mock_redis = AsyncMock()
        return ContextService(redis_client=mock_redis)

    def test_validate_empty_user_id_raises(self):
        from services.errors import ConversationError
        svc = self._make_service()
        with pytest.raises(ConversationError):
            svc._validate_user_id("")
        with pytest.raises(ConversationError):
            svc._validate_user_id(None)

    def test_validate_phone_number(self):
        svc = self._make_service()
        assert svc._validate_user_id("+385912345678") is True

    def test_validate_uuid_raises(self):
        from services.errors import ConversationError
        svc = self._make_service()
        with pytest.raises(ConversationError):
            svc._validate_user_id("12345678-1234-1234-1234-123456789abc")

    def test_key_generation(self):
        svc = self._make_service()
        key = svc._key("+385912345678")
        assert key == "chat_history:+385912345678"


class TestContextServiceAsync:
    @pytest.mark.asyncio
    async def test_get_history_empty(self):
        mock_redis = AsyncMock()
        mock_redis.lrange.return_value = []
        svc = ContextService(redis_client=mock_redis)

        history = await svc.get_history("+385912345678")
        assert history == []

    @pytest.mark.asyncio
    async def test_get_history_with_messages(self):
        import json
        mock_redis = AsyncMock()
        mock_redis.lrange.return_value = [
            json.dumps({"role": "user", "content": "hello"}),
            json.dumps({"role": "assistant", "content": "hi"}),
        ]
        svc = ContextService(redis_client=mock_redis)

        history = await svc.get_history("+385912345678")
        assert len(history) == 2
        assert history[0]["role"] == "user"

    @pytest.mark.asyncio
    async def test_get_history_invalid_json(self):
        mock_redis = AsyncMock()
        mock_redis.lrange.return_value = ["not-json", '{"role": "user", "content": "ok"}']
        svc = ContextService(redis_client=mock_redis)

        history = await svc.get_history("+385912345678")
        assert len(history) == 1  # Invalid JSON skipped

    @pytest.mark.asyncio
    async def test_add_message(self):
        mock_redis = AsyncMock()
        mock_redis.llen.return_value = 5
        svc = ContextService(redis_client=mock_redis)

        result = await svc.add_message("+385912345678", "user", "hello")
        assert result is True
        mock_redis.rpush.assert_called_once()
        mock_redis.expire.assert_called_once()

    @pytest.mark.asyncio
    async def test_add_message_trims_when_over_max(self):
        mock_redis = AsyncMock()
        mock_redis.llen.return_value = 25  # Over max_history (20)
        svc = ContextService(redis_client=mock_redis)

        await svc.add_message("+385912345678", "user", "hello")
        mock_redis.ltrim.assert_called_once()

    @pytest.mark.asyncio
    async def test_clear_history(self):
        mock_redis = AsyncMock()
        svc = ContextService(redis_client=mock_redis)

        result = await svc.clear_history("+385912345678")
        assert result is True
        mock_redis.delete.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_recent_messages(self):
        import json
        mock_redis = AsyncMock()
        mock_redis.lrange.return_value = [
            json.dumps({"role": "user", "content": "msg1"}),
            json.dumps({"role": "assistant", "content": "msg2"}),
            json.dumps({"role": "system", "content": "ignored"}),
        ]
        svc = ContextService(redis_client=mock_redis)

        recent = await svc.get_recent_messages("+385912345678", count=10)
        assert len(recent) == 2  # System messages filtered out
        assert recent[0]["role"] == "user"
        assert recent[1]["role"] == "assistant"

    @pytest.mark.asyncio
    async def test_add_message_redis_failure(self):
        mock_redis = AsyncMock()
        mock_redis.rpush.side_effect = Exception("Redis down")
        svc = ContextService(redis_client=mock_redis)

        result = await svc.add_message("+385912345678", "user", "hello")
        assert result is False

    @pytest.mark.asyncio
    async def test_get_history_redis_failure(self):
        mock_redis = AsyncMock()
        mock_redis.lrange.side_effect = Exception("Redis down")
        svc = ContextService(redis_client=mock_redis)

        history = await svc.get_history("+385912345678")
        assert history == []
