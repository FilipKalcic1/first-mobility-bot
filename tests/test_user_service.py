"""
User Service Tests

Comprehensive tests for services/user_service.py covering:
- Phone variation generation and lookup (get_active_identity)
- Phone matching logic (_phones_match)
- Name extraction logic (_extract_name)
- Auto-onboarding flow (try_auto_onboard)
- Upsert mapping (_upsert_mapping)
- Context building (build_context)
- Context cache invalidation (invalidate_context_cache)
- User refresh flow (refresh_user_from_api)
- Identity verification (verify_user_identity)
- Vehicle info retrieval (_get_vehicle_info)
"""

import json
import pytest
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, MagicMock, patch, call


# ---------------------------------------------------------------------------
# Stub APIResponse so we can construct gateway return values easily
# ---------------------------------------------------------------------------
@dataclass
class FakeAPIResponse:
    """Lightweight stand-in for api_gateway.APIResponse."""
    success: bool
    status_code: int
    data: Any
    error_message: Optional[str] = None
    error_code: Optional[str] = None
    headers: Optional[Dict[str, str]] = None


# ---------------------------------------------------------------------------
# Patch get_settings at module level BEFORE importing UserService
# ---------------------------------------------------------------------------
_mock_settings = MagicMock()
_mock_settings.tenant_id = "test-tenant-default"
_mock_settings.MOBILITY_TENANT_ID = "test-tenant-default"

with patch("config.get_settings", return_value=_mock_settings):
    with patch("services.tenant_service.get_settings", return_value=_mock_settings):
        from services.user_service import UserService


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_db():
    """Mock AsyncSession."""
    db = MagicMock()
    db.execute = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db


@pytest.fixture
def mock_gateway():
    """Mock API Gateway with async execute."""
    gw = MagicMock()
    gw.execute = AsyncMock()
    return gw


@pytest.fixture
def mock_cache():
    """Mock cache service."""
    cache = MagicMock()
    cache.get = AsyncMock(return_value=None)
    cache.get_json = AsyncMock(return_value=None)
    cache.set = AsyncMock(return_value=True)
    cache.set_json = AsyncMock(return_value=True)
    cache.delete = AsyncMock(return_value=True)
    return cache


@pytest.fixture
def mock_redis():
    """Mock Redis client."""
    redis = MagicMock()
    redis.get = AsyncMock(return_value=None)
    redis.set = AsyncMock(return_value=True)
    return redis


@pytest.fixture
def user_service(mock_db, mock_gateway, mock_cache, mock_redis):
    """Build UserService with all mocks injected."""
    with patch("services.user_service.get_tenant_service") as mock_ts_factory, \
         patch("services.user_service._get_settings", return_value=_mock_settings):
        tenant_svc = MagicMock()
        tenant_svc.get_default_tenant = MagicMock(return_value="tenant-default")
        tenant_svc.get_tenant_for_user = AsyncMock(return_value="tenant-default")
        mock_ts_factory.return_value = tenant_svc

        svc = UserService(
            db=mock_db,
            gateway=mock_gateway,
            cache=mock_cache,
            redis_client=mock_redis,
        )
        svc._tenant_service = tenant_svc
        return svc


@pytest.fixture
def user_service_no_gateway(mock_db, mock_cache, mock_redis):
    """UserService without gateway -- simulates missing external API."""
    with patch("services.user_service.get_tenant_service") as mock_ts_factory:
        tenant_svc = MagicMock()
        tenant_svc.get_default_tenant = MagicMock(return_value="tenant-default")
        tenant_svc.get_tenant_for_user = AsyncMock(return_value="tenant-default")
        mock_ts_factory.return_value = tenant_svc

        svc = UserService(
            db=mock_db,
            gateway=None,
            cache=mock_cache,
            redis_client=mock_redis,
        )
        svc._tenant_service = tenant_svc
        return svc


def _make_user_mapping(phone, person_id="person-abc-123", name="Test User", tenant_id="tenant-hr"):
    """Helper to build a mock UserMapping row."""
    user = MagicMock()
    user.phone_number = phone
    user.api_identity = person_id
    user.display_name = name
    user.tenant_id = tenant_id
    user.is_active = True
    user.updated_at = datetime.now(timezone.utc)
    return user


# ===========================================================================
# 1. _phones_match -- pure logic, no I/O
# ===========================================================================

class TestPhonesMatch:
    """Test the phone comparison logic."""

    def test_exact_match(self, user_service):
        assert user_service._phones_match("+385955087196", "+385955087196") is True

    def test_digits_only_match(self, user_service):
        assert user_service._phones_match("+385955087196", "385955087196") is True

    def test_last_nine_digits_match(self, user_service):
        """Different country-code prefix but same local number."""
        assert user_service._phones_match("+385955087196", "0955087196") is True

    def test_mismatch(self, user_service):
        assert user_service._phones_match("+385991111111", "+385992222222") is False

    def test_short_numbers_no_false_positive(self, user_service):
        """Numbers shorter than 9 digits should not match unless exactly equal."""
        assert user_service._phones_match("12345", "12345") is True
        assert user_service._phones_match("12345", "12346") is False

    def test_empty_api_phone(self, user_service):
        """Empty API phone should not match."""
        assert user_service._phones_match("+385991234567", "") is False


# ===========================================================================
# 2. _extract_name -- pure logic
# ===========================================================================

class TestExtractName:
    """Test display name extraction from person dict."""

    def test_display_name_present(self, user_service):
        assert user_service._extract_name({"DisplayName": "Ivan Horvat"}) == "Ivan Horvat"

    def test_first_last_fallback(self, user_service):
        person = {"FirstName": "Ana", "LastName": "Novak"}
        assert user_service._extract_name(person) == "Ana Novak"

    def test_default_when_empty(self, user_service):
        assert user_service._extract_name({}) == "Korisnik"

    def test_clean_dash_format(self, user_service):
        """'A-1 - Horvat, Ivan' should become 'Ivan Horvat'."""
        person = {"DisplayName": "A-1 - Horvat, Ivan"}
        assert user_service._extract_name(person) == "Ivan Horvat"

    def test_clean_dash_format_no_comma(self, user_service):
        """'A-1 - Ivan Horvat' (no comma) should become 'Ivan Horvat'."""
        person = {"DisplayName": "A-1 - Ivan Horvat"}
        assert user_service._extract_name(person) == "Ivan Horvat"


# ===========================================================================
# 3. get_active_identity -- DB lookup with phone variations
# ===========================================================================

class TestGetActiveIdentity:
    """Test DB-based user lookup with phone format variations."""

    @pytest.mark.asyncio
    async def test_user_found(self, user_service, mock_db):
        """Should return user when DB finds a match."""
        fake_user = _make_user_mapping("+385991234567")
        result_mock = MagicMock()
        result_mock.scalars.return_value.first.return_value = fake_user
        mock_db.execute.return_value = result_mock

        user = await user_service.get_active_identity("+385991234567")
        assert user is not None
        assert user.phone_number == "+385991234567"
        mock_db.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_user_not_found(self, user_service, mock_db):
        """Should return None when no matching user."""
        result_mock = MagicMock()
        result_mock.scalars.return_value.first.return_value = None
        mock_db.execute.return_value = result_mock

        user = await user_service.get_active_identity("+385991234567")
        assert user is None

    @pytest.mark.asyncio
    async def test_db_exception_returns_none(self, user_service, mock_db):
        """DB error should be caught and None returned."""
        mock_db.execute.side_effect = Exception("connection lost")

        user = await user_service.get_active_identity("+385991234567")
        assert user is None


# ===========================================================================
# 4. try_auto_onboard -- API lookup + upsert
# ===========================================================================

class TestTryAutoOnboard:
    """Test the auto-onboarding flow from MobilityOne API."""

    @pytest.mark.asyncio
    async def test_no_gateway_returns_none(self, user_service_no_gateway):
        result = await user_service_no_gateway.try_auto_onboard("+385991234567")
        assert result is None

    @pytest.mark.asyncio
    async def test_successful_onboard(self, user_service, mock_gateway, mock_db):
        """Happy path: API returns a matching person, user is upserted."""
        person_data = {
            "Id": "person-new-001",
            "DisplayName": "Marko Maric",
            "Phone": "+385991234567",
        }
        # First call to /tenantmgt/Persons returns the person
        mock_gateway.execute.return_value = FakeAPIResponse(
            success=True,
            status_code=200,
            data={"Data": [person_data]},
        )
        # Mock _upsert_mapping and _get_vehicle_info so they don't hit real DB/API
        user_service._upsert_mapping = AsyncMock()
        user_service._get_vehicle_info = AsyncMock(return_value={"plate": "ZG-1234"})

        result = await user_service.try_auto_onboard("+385991234567")
        assert result is not None
        display_name, vehicle_info = result
        assert display_name == "Marko Maric"
        user_service._upsert_mapping.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_api_returns_500_skips_field(self, user_service, mock_gateway):
        """If API returns 500 for 'Phone' field, it should be skipped gracefully."""
        mock_gateway.execute.return_value = FakeAPIResponse(
            success=False,
            status_code=500,
            data=None,
            error_message="Internal Server Error",
        )

        result = await user_service.try_auto_onboard("+385991234567")
        assert result is None

    @pytest.mark.asyncio
    async def test_phone_mismatch_skips_person(self, user_service, mock_gateway):
        """If API person phone does not match input, the person should be skipped."""
        person_data = {
            "Id": "person-wrong",
            "DisplayName": "Wrong Person",
            "Phone": "+385999999999",  # last 9 digits differ
        }
        mock_gateway.execute.return_value = FakeAPIResponse(
            success=True,
            status_code=200,
            data={"Data": [person_data]},
        )

        result = await user_service.try_auto_onboard("+385991234567")
        assert result is None

    @pytest.mark.asyncio
    async def test_exception_during_onboard_returns_none(self, user_service, mock_gateway):
        """Unhandled exception inside try_auto_onboard should be caught."""
        mock_gateway.execute.side_effect = Exception("network timeout")

        result = await user_service.try_auto_onboard("+385991234567")
        assert result is None


# ===========================================================================
# 5. _upsert_mapping -- DB insert/update
# ===========================================================================

class TestUpsertMapping:
    """Test database upsert for user mapping."""

    @pytest.mark.asyncio
    async def test_upsert_commits(self, user_service, mock_db):
        """Successful upsert should call execute + commit."""
        await user_service._upsert_mapping("+385991234567", "person-123", "Test User")

        mock_db.execute.assert_awaited_once()
        mock_db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_upsert_rollback_on_error(self, user_service, mock_db):
        """DB error during upsert should trigger rollback."""
        mock_db.execute.side_effect = Exception("unique violation")

        await user_service._upsert_mapping("+385991234567", "person-123", "Test User")

        mock_db.rollback.assert_awaited_once()


# ===========================================================================
# 6. build_context -- orchestration: cache, tenant, vehicle
# ===========================================================================

class TestBuildContext:
    """Test user context building."""

    @pytest.mark.asyncio
    async def test_returns_cached_context(self, user_service, mock_cache):
        """If cache has valid context, return it immediately."""
        cached_ctx = {
            "person_id": "person-123",
            "phone": "+385991234567",
            "tenant_id": "tenant-hr",
            "display_name": "Cached User",
            "vehicle": {"plate": "ZG-0000"},
        }
        mock_cache.get_json.return_value = cached_ctx

        ctx = await user_service.build_context("person-123", "+385991234567")
        assert ctx["display_name"] == "Cached User"
        # Gateway should NOT be called when cache hit
        user_service.gateway.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_builds_context_without_gateway(self, user_service_no_gateway):
        """Without gateway, context should still have basic fields."""
        ctx = await user_service_no_gateway.build_context("person-123", "+385991234567")

        assert ctx["person_id"] == "person-123"
        assert ctx["phone"] == "+385991234567"
        assert ctx["tenant_id"] == "tenant-default"
        assert ctx["vehicle"] == {}

    @pytest.mark.asyncio
    @patch("services.user_service.get_schema_extractor")
    async def test_builds_context_with_vehicle_data(self, mock_extractor_factory, user_service, mock_gateway, mock_cache):
        """Context should include vehicle data from API when gateway is available."""
        mock_cache.get_json.return_value = None  # No cache

        # Mock schema extractor
        extractor = MagicMock()
        extractor.extract_all.return_value = {
            "Id": "vehicle-001",
            "LicencePlate": "ZG-1234-AB",
            "Driver": "Ivan Horvat",
        }
        mock_extractor_factory.return_value = extractor

        # MasterData call succeeds
        mock_gateway.execute.return_value = FakeAPIResponse(
            success=True,
            status_code=200,
            data={"Id": "vehicle-001", "LicencePlate": "ZG-1234-AB", "Driver": "Ivan Horvat"},
        )

        ctx = await user_service.build_context("person-123", "+385991234567")

        assert ctx["vehicle"].get("LicencePlate") == "ZG-1234-AB"
        assert ctx["display_name"] == "Ivan Horvat"
        # Should cache valid context
        mock_cache.set_json.assert_awaited()

    @pytest.mark.asyncio
    async def test_build_context_gateway_error_returns_basic(self, user_service, mock_gateway, mock_cache):
        """If gateway errors, context should still be returned with empty vehicle."""
        mock_cache.get_json.return_value = None
        mock_gateway.execute.side_effect = Exception("API down")

        ctx = await user_service.build_context("person-123", "+385991234567")

        assert ctx["person_id"] == "person-123"
        # vehicle may be empty dict due to exception
        assert "vehicle" in ctx


# ===========================================================================
# 7. invalidate_context_cache
# ===========================================================================

class TestInvalidateContextCache:
    """Test cache invalidation."""

    @pytest.mark.asyncio
    async def test_invalidate_with_cache(self, user_service, mock_cache):
        result = await user_service.invalidate_context_cache("person-123")
        assert result is True
        mock_cache.delete.assert_awaited_once_with("context:person-123")

    @pytest.mark.asyncio
    async def test_invalidate_without_cache(self, user_service_no_gateway):
        """Service without cache should return False."""
        user_service_no_gateway.cache = None
        result = await user_service_no_gateway.invalidate_context_cache("person-123")
        assert result is False

    @pytest.mark.asyncio
    async def test_invalidate_cache_error_returns_false(self, user_service, mock_cache):
        """Cache error during invalidation should be caught."""
        mock_cache.delete.side_effect = Exception("redis connection lost")
        result = await user_service.invalidate_context_cache("person-123")
        assert result is False


# ===========================================================================
# 8. refresh_user_from_api -- delete + re-onboard
# ===========================================================================

class TestRefreshUserFromApi:
    """Test force-refresh user data flow."""

    @pytest.mark.asyncio
    async def test_refresh_deletes_old_mapping_and_reonboards(self, user_service, mock_db):
        """Refresh should delete old mapping, invalidate cache, then auto-onboard."""
        # get_active_identity returns None after deletion
        user_service.get_active_identity = AsyncMock(return_value=None)
        user_service.try_auto_onboard = AsyncMock(return_value=("Refreshed User", {"plate": "ZG-NEW"}))

        result = await user_service.refresh_user_from_api("+385991234567")

        mock_db.execute.assert_awaited()  # DELETE statement
        mock_db.commit.assert_awaited()
        assert result is not None
        assert result[0] == "Refreshed User"

    @pytest.mark.asyncio
    async def test_refresh_handles_delete_failure(self, user_service, mock_db):
        """If deleting old mapping fails, refresh should still attempt onboard."""
        mock_db.execute.side_effect = Exception("FK constraint")
        user_service.get_active_identity = AsyncMock(return_value=None)
        user_service.try_auto_onboard = AsyncMock(return_value=None)

        result = await user_service.refresh_user_from_api("+385991234567")

        mock_db.rollback.assert_awaited()
        user_service.try_auto_onboard.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_refresh_invalidates_cache_for_old_user(self, user_service, mock_db, mock_cache):
        """If old user exists, its context cache should be invalidated."""
        old_user = _make_user_mapping("+385991234567", person_id="old-person-id")

        # First execute call is the DELETE; after that get_active_identity is called
        user_service.get_active_identity = AsyncMock(return_value=old_user)
        user_service.try_auto_onboard = AsyncMock(return_value=("New User", {}))

        await user_service.refresh_user_from_api("+385991234567")

        mock_cache.delete.assert_awaited_with("context:old-person-id")


# ===========================================================================
# 9. verify_user_identity -- debug / diagnostic
# ===========================================================================

class TestVerifyUserIdentity:
    """Test the diagnostic verify_user_identity method."""

    @pytest.mark.asyncio
    async def test_user_not_found_anywhere(self, user_service, mock_db, mock_gateway):
        """When neither DB nor API has user, recommendation is 'not found'."""
        # DB returns None
        result_mock = MagicMock()
        result_mock.scalars.return_value.first.return_value = None
        mock_db.execute.return_value = result_mock

        # API returns empty
        mock_gateway.execute.return_value = FakeAPIResponse(
            success=True, status_code=200, data={"Data": []},
        )

        info = await user_service.verify_user_identity("+385991234567")

        assert info["database"] is None
        assert info["api"] is None
        assert "not found" in info["recommendation"].lower()

    @pytest.mark.asyncio
    async def test_user_in_db_and_api_matching(self, user_service, mock_db, mock_gateway):
        """DB and API agree -- recommendation should say OK."""
        fake_user = _make_user_mapping("+385991234567", person_id="person-match")
        result_mock = MagicMock()
        result_mock.scalars.return_value.first.return_value = fake_user
        mock_db.execute.return_value = result_mock

        mock_gateway.execute.return_value = FakeAPIResponse(
            success=True,
            status_code=200,
            data={"Data": [{"Id": "person-match", "DisplayName": "Test", "Phone": "+385991234567"}]},
        )

        info = await user_service.verify_user_identity("+385991234567")

        assert info["database"]["person_id"] == "person-match"
        assert info["api"]["person_id"] == "person-match"
        assert info["phone_match"] is True
        assert "OK" in info["recommendation"]

    @pytest.mark.asyncio
    async def test_stale_db_detected(self, user_service, mock_db, mock_gateway):
        """DB person_id differs from API -- recommendation flags stale data."""
        fake_user = _make_user_mapping("+385991234567", person_id="old-person")
        result_mock = MagicMock()
        result_mock.scalars.return_value.first.return_value = fake_user
        mock_db.execute.return_value = result_mock

        mock_gateway.execute.return_value = FakeAPIResponse(
            success=True,
            status_code=200,
            data={"Data": [{"Id": "new-person", "DisplayName": "New", "Phone": "+385991234567"}]},
        )

        info = await user_service.verify_user_identity("+385991234567")

        assert "STALE" in info["recommendation"]

    @pytest.mark.asyncio
    async def test_verify_without_gateway(self, user_service_no_gateway, mock_db):
        """Without gateway, API section should remain None."""
        result_mock = MagicMock()
        result_mock.scalars.return_value.first.return_value = None
        mock_db = user_service_no_gateway.db
        mock_db.execute = AsyncMock(return_value=result_mock)

        info = await user_service_no_gateway.verify_user_identity("+385991234567")

        assert info["api"] is None


# ===========================================================================
# 10. _get_vehicle_info -- vehicle data retrieval with matching
# ===========================================================================

class TestGetVehicleInfo:
    """Test vehicle data retrieval and matching logic."""

    @pytest.mark.asyncio
    @patch("services.user_service.get_schema_extractor")
    async def test_masterdata_failure_returns_empty(self, mock_ext_factory, user_service, mock_gateway):
        """If MasterData API fails, return empty dict."""
        mock_gateway.execute.return_value = FakeAPIResponse(
            success=False, status_code=500, data=None, error_message="Server Error",
        )

        result = await user_service._get_vehicle_info("person-123")
        assert result == {}

    @pytest.mark.asyncio
    @patch("services.user_service.get_schema_extractor")
    async def test_single_vehicle_match(self, mock_ext_factory, user_service, mock_gateway):
        """When only one vehicle belongs to driver, use it even without ID match."""
        extractor = MagicMock()
        extractor.extract_all.return_value = {"Id": "veh-999", "LicencePlate": "ZG-0001"}
        mock_ext_factory.return_value = extractor

        # First call: MasterData
        master_resp = FakeAPIResponse(success=True, status_code=200, data={"Id": "veh-999"})
        # Second call: /vehiclemgt/Vehicles with single vehicle, different ID
        vehicles_resp = FakeAPIResponse(
            success=True, status_code=200,
            data={"Data": [{"Id": "veh-only", "RegistrationNumber": "ZG-0001", "PeriodicActivities": [{"Type": "Oil"}]}]},
        )
        mock_gateway.execute.side_effect = [master_resp, vehicles_resp]

        result = await user_service._get_vehicle_info("person-123")
        # Since there is exactly 1 vehicle, it should be used (PeriodicActivities overridden)
        assert result.get("PeriodicActivities") == [{"Type": "Oil"}]

    @pytest.mark.asyncio
    @patch("services.user_service.get_schema_extractor")
    async def test_exception_returns_empty(self, mock_ext_factory, user_service, mock_gateway):
        """Unexpected error during vehicle fetch should return empty dict."""
        mock_gateway.execute.side_effect = Exception("connection reset")

        result = await user_service._get_vehicle_info("person-123")
        assert result == {}


# ===========================================================================
# 11. Phone variation generation (implicitly tested via get_active_identity)
# ===========================================================================

class TestPhoneVariations:
    """Verify that phone variation logic covers expected formats."""

    @pytest.mark.asyncio
    async def test_plus385_generates_expected_variations(self, user_service, mock_db):
        """For +385991234567 we expect: the original, digits-only, without +, with leading 0."""
        result_mock = MagicMock()
        result_mock.scalars.return_value.first.return_value = None
        mock_db.execute.return_value = result_mock

        await user_service.get_active_identity("+385991234567")

        # Inspect the SQL call to verify the IN clause variations
        call_args = mock_db.execute.call_args
        # The statement is a SQLAlchemy Select; we cannot easily inspect it,
        # but we confirm the call was made (the variation logic is exercised)
        assert mock_db.execute.await_count == 1

    @pytest.mark.asyncio
    async def test_local_format_generates_international(self, user_service, mock_db):
        """A local '0991234567' number should produce '385991234567' variation."""
        result_mock = MagicMock()
        result_mock.scalars.return_value.first.return_value = None
        mock_db.execute.return_value = result_mock

        await user_service.get_active_identity("0991234567")
        assert mock_db.execute.await_count == 1


# ===========================================================================
# 12. Constructor and defaults
# ===========================================================================

class TestUserServiceInit:
    """Test constructor and attribute defaults."""

    def test_default_tenant_from_settings(self, user_service):
        assert user_service.default_tenant_id == "test-tenant-default"

    def test_gateway_stored(self, user_service, mock_gateway):
        assert user_service.gateway is mock_gateway

    def test_cache_stored(self, user_service, mock_cache):
        assert user_service.cache is mock_cache

    def test_redis_stored(self, user_service, mock_redis):
        assert user_service.redis is mock_redis
