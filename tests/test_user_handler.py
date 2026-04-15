"""Tests for services/engine/user_handler.py – UserHandler."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from services.engine.user_handler import UserHandler


def _user_context(vehicle=True):
    ctx = {
        "person_id": "00000000-0000-0000-0000-000000000001",
        "phone": "+385991234567",
        "tenant_id": "t1",
        "display_name": "Igor",
    }
    if vehicle:
        ctx["vehicle"] = {"id": "v1", "plate": "ZG-1234-AB", "name": "Golf", "mileage": 50000}
    return ctx


@pytest.fixture
def handler():
    return UserHandler(db_session=MagicMock(), gateway=MagicMock(), cache_service=MagicMock())


class TestInit:
    def test_attributes(self, handler):
        assert handler.db is not None
        assert handler.gateway is not None
        assert handler.cache is not None


class TestIdentifyUser:
    @pytest.mark.asyncio
    async def test_existing_user(self, handler):
        user_mock = MagicMock()
        user_mock.display_name = "Igor"
        user_mock.api_identity = "api-id-123"

        with patch("services.engine.user_handler.UserService") as MockUS:
            svc = MagicMock()
            svc.get_active_identity = AsyncMock(return_value=user_mock)
            svc.build_context = AsyncMock(return_value={"person_id": "p1"})
            MockUS.return_value = svc

            result = await handler.identify_user("+385991234567")
            assert result is not None
            assert result["display_name"] == "Igor"
            assert result["is_new"] is False

    @pytest.mark.asyncio
    async def test_auto_onboard(self, handler):
        user_mock = MagicMock()
        user_mock.display_name = "Novi"
        user_mock.api_identity = "api-new"

        with patch("services.engine.user_handler.UserService") as MockUS:
            svc = MagicMock()
            svc.get_active_identity = AsyncMock(side_effect=[None, user_mock])
            svc.try_auto_onboard = AsyncMock(return_value=("Novi", {"id": "v1"}))
            svc.build_context = AsyncMock(return_value={"person_id": "p2"})
            MockUS.return_value = svc

            result = await handler.identify_user("+385991234567")
            assert result is not None
            assert result["is_new"] is True
            assert result["display_name"] == "Novi"

    @pytest.mark.asyncio
    async def test_user_not_found_returns_guest_context(self, handler):
        """When user is not in MobilityOne, returns guest context (never None)."""
        with patch("services.engine.user_handler.UserService") as MockUS:
            svc = MagicMock()
            svc.get_active_identity = AsyncMock(return_value=None)
            svc.try_auto_onboard = AsyncMock(return_value=None)
            svc.default_tenant_id = "default-tenant"
            MockUS.return_value = svc

            result = await handler.identify_user("+385000000000")
            assert result is not None
            assert result["is_guest"] is True
            assert result["person_id"] is None
            assert result["phone"] == "+385000000000"
            assert result["display_name"] == "Korisnik"

    @pytest.mark.asyncio
    async def test_user_exists_without_api_identity_triggers_auto_onboard(self, handler):
        """When user exists in DB but has no api_identity, system should auto-onboard from API."""
        user_no_id = MagicMock()
        user_no_id.api_identity = None
        user_no_id.display_name = "Filip"
        user_no_id.tenant_id = "tenant-123"

        user_with_id = MagicMock()
        user_with_id.api_identity = "real-person-id-from-api"
        user_with_id.display_name = "Filip"
        user_with_id.tenant_id = "tenant-123"

        with patch("services.engine.user_handler.UserService") as MockUS:
            svc = MagicMock()
            # First call returns user without api_identity, second returns updated user
            svc.get_active_identity = AsyncMock(side_effect=[user_no_id, user_with_id])
            svc.try_auto_onboard = AsyncMock(return_value=("Filip", {}))
            svc.build_context = AsyncMock(return_value={"person_id": "real-person-id-from-api"})
            MockUS.return_value = svc

            result = await handler.identify_user("+385991234567")
            # Should have called auto-onboard
            svc.try_auto_onboard.assert_called_once()
            assert result is not None
            assert result["is_new"] is False

    @pytest.mark.asyncio
    async def test_user_exists_without_api_identity_api_down_returns_limited_context(self, handler):
        """When user exists but API is unreachable, return limited context (not guest)."""
        user_no_id = MagicMock()
        user_no_id.api_identity = None
        user_no_id.display_name = "Filip"
        user_no_id.tenant_id = "tenant-123"

        mock_tenant_svc = MagicMock()
        mock_tenant_svc.get_tenant_for_user = AsyncMock(return_value="tenant-123")

        with patch("services.engine.user_handler.UserService") as MockUS:
            svc = MagicMock()
            svc.get_active_identity = AsyncMock(return_value=user_no_id)
            svc.try_auto_onboard = AsyncMock(return_value=None)  # API down
            svc._tenant_service = mock_tenant_svc
            MockUS.return_value = svc

            result = await handler.identify_user("+385991234567")
            assert result is not None
            assert result["is_guest"] is False  # NOT guest — has DB record
            assert result["person_id"] is None  # Can't resolve without API
            assert result["display_name"] == "Filip"
            assert result["tenant_id"] == "tenant-123"

    @pytest.mark.asyncio
    async def test_auto_onboard_second_lookup_fails_returns_fallback_context(self, handler):
        """When auto-onboard succeeds but second lookup fails, returns API-based context (not guest)."""
        with patch("services.engine.user_handler.UserService") as MockUS:
            svc = MagicMock()
            svc.get_active_identity = AsyncMock(side_effect=[None, None])
            svc.try_auto_onboard = AsyncMock(return_value=("Novi", {"id": "v1"}))
            svc.default_tenant_id = "default-tenant"
            MockUS.return_value = svc

            result = await handler.identify_user("+385000000000")
            assert result is not None
            # User IS in MobilityOne (auto-onboard succeeded), so not a guest
            assert result["is_guest"] is False
            assert result["is_new"] is True
            assert result["display_name"] == "Novi"


class TestBuildGreeting:
    def test_with_vehicle_plate(self, handler):
        ctx = _user_context(vehicle=True)
        greeting = handler.build_greeting(ctx)
        assert "Igor" in greeting
        assert "Golf" in greeting
        assert "ZG-1234-AB" in greeting
        assert "50000" in greeting

    def test_with_vehicle_id_only(self, handler):
        ctx = _user_context(vehicle=False)
        ctx["vehicle"] = {"id": "v1", "plate": "", "name": "Passat"}
        greeting = handler.build_greeting(ctx)
        assert "Igor" in greeting
        assert "Passat" in greeting

    def test_no_vehicle(self, handler):
        ctx = _user_context(vehicle=False)
        greeting = handler.build_greeting(ctx)
        assert "Igor" in greeting
        assert "nemate" in greeting.lower()
        assert "rezervirati" in greeting.lower()
