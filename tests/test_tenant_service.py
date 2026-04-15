"""
Tenant Service Tests - Dynamic multi-tenant routing.

Tests the tenant resolution chain:
1. UserMapping.tenant_id (DB) -> highest priority (API-discovered)
2. Default tenant (env) -> fallback for bootstrap
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from services.tenant_service import TenantService, TenantConfig


class TestTenantResolutionOrder:
    """Test the 2-level resolution: DB -> default."""

    @pytest.fixture
    def service(self):
        with patch("services.tenant_service._get_settings", return_value=MagicMock(MOBILITY_TENANT_ID="tenant-default")):
            svc = TenantService()
            return svc

    @pytest.mark.asyncio
    async def test_user_mapping_takes_priority(self, service):
        """If user has tenant_id in DB (API-discovered), that is used."""
        user_mapping = MagicMock()
        user_mapping.tenant_id = "company-abc-uuid"

        result = await service.get_tenant_for_user("+385991234567", user_mapping)
        assert result == "company-abc-uuid"

    @pytest.mark.asyncio
    async def test_default_used_when_no_user_mapping(self, service):
        """Without user mapping, default tenant is returned (for bootstrap)."""
        result = await service.get_tenant_for_user("+385991234567", None)
        assert result == "tenant-default"

    @pytest.mark.asyncio
    async def test_default_used_when_user_mapping_has_no_tenant(self, service):
        """If UserMapping exists but tenant_id is None, use default."""
        user_mapping = MagicMock()
        user_mapping.tenant_id = None

        result = await service.get_tenant_for_user("+385991234567", user_mapping)
        assert result == "tenant-default"

    @pytest.mark.asyncio
    async def test_default_used_for_unknown_phone(self, service):
        """Any unknown phone falls back to default tenant."""
        result = await service.get_tenant_for_user("+12025551234", None)
        assert result == "tenant-default"


class TestGetDefaultTenant:
    """Test get_default_tenant method."""

    def test_returns_env_value(self):
        with patch("services.tenant_service._get_settings", return_value=MagicMock(MOBILITY_TENANT_ID="my-tenant-uuid")):
            service = TenantService()
            assert service.get_default_tenant() == "my-tenant-uuid"


class TestTenantCaching:
    """Test Redis-based tenant caching."""

    @pytest.fixture
    def service_with_redis(self):
        with patch("services.tenant_service._get_settings", return_value=MagicMock(MOBILITY_TENANT_ID="tenant-default")):
            redis = AsyncMock()
            svc = TenantService(redis_client=redis)
            return svc, redis

    @pytest.mark.asyncio
    async def test_cache_hit_returns_cached_value(self, service_with_redis):
        service, redis = service_with_redis
        redis.get = AsyncMock(return_value="tenant-cached")

        result = await service.get_tenant_for_user("+385991234567", None)
        assert result == "tenant-cached"

    @pytest.mark.asyncio
    async def test_cache_miss_falls_to_default(self, service_with_redis):
        service, redis = service_with_redis
        redis.get = AsyncMock(return_value=None)
        redis.set = AsyncMock()

        result = await service.get_tenant_for_user("+385991234567", None)
        assert result == "tenant-default"

    @pytest.mark.asyncio
    async def test_redis_failure_does_not_break_resolution(self, service_with_redis):
        """If Redis is down, tenant resolution still works via default."""
        service, redis = service_with_redis
        redis.get = AsyncMock(side_effect=Exception("Redis connection lost"))
        redis.set = AsyncMock(side_effect=Exception("Redis connection lost"))

        result = await service.get_tenant_for_user("+385991234567", None)
        assert result == "tenant-default"


class TestTenantValidation:
    """Test tenant ID format validation."""

    @pytest.fixture
    def service(self):
        with patch("services.tenant_service._get_settings", return_value=MagicMock(MOBILITY_TENANT_ID="tenant-default")):
            return TenantService()

    def test_valid_uuid_tenant_id(self, service):
        assert service.validate_tenant("dee707eb-66ad-42e1-92f2-068be031f18a") is True

    def test_valid_short_tenant_id(self, service):
        assert service.validate_tenant("my-company-123") is True

    def test_empty_tenant_id_invalid(self, service):
        assert service.validate_tenant("") is False
        assert service.validate_tenant(None) is False

    def test_short_tenant_id_invalid(self, service):
        assert service.validate_tenant("ab") is False

    def test_special_characters_invalid(self, service):
        assert service.validate_tenant("tenant;DROP TABLE") is False
        assert service.validate_tenant("tenant<script>") is False
        assert service.validate_tenant("tenant/../etc") is False


class TestTenantConfig:
    """Test TenantConfig dataclass."""

    def test_defaults(self):
        config = TenantConfig(tenant_id="test", name="Test Tenant")
        assert config.rate_limit == 20
        assert config.is_active is True
        assert config.api_url is None

    def test_custom_values(self):
        config = TenantConfig(
            tenant_id="premium",
            name="Premium Tenant",
            rate_limit=100,
            api_url="https://custom-api.example.com"
        )
        assert config.rate_limit == 100
        assert config.api_url == "https://custom-api.example.com"


class TestTenantStats:
    """Test get_tenant_stats method."""

    def test_returns_stats_dict(self):
        with patch("services.tenant_service._get_settings", return_value=MagicMock(MOBILITY_TENANT_ID="tenant-default")):
            service = TenantService()
            stats = service.get_tenant_stats()

            assert "default_tenant" in stats
            assert "resolution" in stats
            assert stats["default_tenant"] == "tenant-default"
            assert "API-discovered" in stats["resolution"]


class TestUpdateUserTenant:
    """Test update_user_tenant method."""

    @pytest.fixture
    def service_with_db(self):
        with patch("services.tenant_service._get_settings", return_value=MagicMock(MOBILITY_TENANT_ID="tenant-default")):
            db = AsyncMock()
            redis = AsyncMock()
            svc = TenantService(db_session=db, redis_client=redis)
            return svc, db, redis

    @pytest.mark.asyncio
    async def test_no_db_returns_false(self):
        """Test returns False when no database session."""
        with patch("services.tenant_service._get_settings", return_value=MagicMock(MOBILITY_TENANT_ID="tenant-default")):
            service = TenantService(db_session=None)

            result = await service.update_user_tenant("+385991234567", "tenant-new", "admin")
            assert result is False

    @pytest.mark.asyncio
    async def test_successful_update(self, service_with_db):
        """Test successful tenant update."""
        service, db, redis = service_with_db

        mock_result = MagicMock()
        mock_result.rowcount = 1
        db.execute = AsyncMock(return_value=mock_result)
        db.commit = AsyncMock()

        result = await service.update_user_tenant("+385991234567", "tenant-new", "admin-123")

        assert result is True
        db.execute.assert_called_once()
        db.commit.assert_called_once()
        redis.delete.assert_called_once_with("tenant:+385991234567")

    @pytest.mark.asyncio
    async def test_update_no_rows_affected(self, service_with_db):
        """Test update when no rows affected returns False."""
        service, db, redis = service_with_db

        mock_result = MagicMock()
        mock_result.rowcount = 0
        db.execute = AsyncMock(return_value=mock_result)
        db.commit = AsyncMock()

        result = await service.update_user_tenant("+385999999999", "tenant-new", "admin")

        assert result is False

    @pytest.mark.asyncio
    async def test_update_exception_rollback(self, service_with_db):
        """Test exception during update triggers rollback."""
        service, db, redis = service_with_db

        db.execute = AsyncMock(side_effect=Exception("DB error"))
        db.rollback = AsyncMock()

        result = await service.update_user_tenant("+385991234567", "tenant-new", "admin")

        assert result is False
        db.rollback.assert_called_once()


class TestGetTenantServiceSingleton:
    """Test get_tenant_service factory function."""

    def test_creates_singleton(self):
        """Test creates singleton instance."""
        import services.tenant_service as ts_module

        # Reset singleton
        ts_module._tenant_service = None

        with patch("services.tenant_service._get_settings", return_value=MagicMock(MOBILITY_TENANT_ID="tenant-default")):

            from services.tenant_service import get_tenant_service

            svc1 = get_tenant_service()
            svc2 = get_tenant_service()

            assert svc1 is svc2

    def test_updates_db_session_if_missing(self):
        """Test updates db_session if service exists but db is None."""
        import services.tenant_service as ts_module

        ts_module._tenant_service = None

        with patch("services.tenant_service._get_settings", return_value=MagicMock(MOBILITY_TENANT_ID="tenant-default")):

            from services.tenant_service import get_tenant_service

            svc1 = get_tenant_service()
            assert svc1.db is None

            mock_db = MagicMock()
            svc2 = get_tenant_service(db_session=mock_db)

            assert svc1 is svc2
            assert svc2.db is mock_db

    def test_updates_redis_if_missing(self):
        """Test updates redis if service exists but redis is None."""
        import services.tenant_service as ts_module

        ts_module._tenant_service = None

        with patch("services.tenant_service._get_settings", return_value=MagicMock(MOBILITY_TENANT_ID="tenant-default")):

            from services.tenant_service import get_tenant_service

            svc1 = get_tenant_service()
            assert svc1.redis is None

            mock_redis = MagicMock()
            svc2 = get_tenant_service(redis_client=mock_redis)

            assert svc1 is svc2
            assert svc2.redis is mock_redis
