"""Tests for TokenManager - OAuth2 token management."""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timedelta, timezone

from services.token_manager import TokenManager


@pytest.fixture
def mock_redis():
    return AsyncMock()


@pytest.fixture
def tm(mock_redis):
    return TokenManager(redis_client=mock_redis)


@pytest.fixture
def tm_no_redis():
    return TokenManager(redis_client=None)


class TestInit:
    def test_init_with_redis(self, tm, mock_redis):
        assert tm._redis is mock_redis
        assert tm._token is None
        assert tm.is_valid is False

    def test_init_without_redis(self, tm_no_redis):
        assert tm_no_redis._redis is None


class TestIsValid:
    def test_no_token(self, tm):
        assert tm.is_valid is False

    def test_valid_token(self, tm):
        tm._token = "test_token"
        tm._expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        assert tm.is_valid is True

    def test_expired_token(self, tm):
        tm._token = "test_token"
        tm._expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
        assert tm.is_valid is False


class TestGetToken:
    pytestmark = pytest.mark.asyncio

    async def test_returns_cached_token(self, tm):
        tm._token = "cached_token"
        tm._expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        token = await tm.get_token()
        assert token == "cached_token"

    async def test_loads_from_redis(self, tm, mock_redis):
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()
        mock_redis.get.return_value = json.dumps({
            "token": "redis_token",
            "expires_at": expires_at
        })
        token = await tm.get_token()
        assert token == "redis_token"

    async def test_fetches_new_token(self, tm, mock_redis):
        mock_redis.get.return_value = None
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "access_token": "new_token",
                "expires_in": 3600
            }
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client
            token = await tm.get_token()
            assert token == "new_token"

    async def test_redis_cache_failure_still_fetches(self, tm, mock_redis):
        mock_redis.get.side_effect = Exception("Redis down")
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "access_token": "fallback_token",
                "expires_in": 3600
            }
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client
            token = await tm.get_token()
            assert token == "fallback_token"


class TestFetchNewToken:
    pytestmark = pytest.mark.asyncio

    async def test_auth_failure_raises(self, tm, mock_redis):
        mock_redis.get.return_value = None
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_response = MagicMock()
            mock_response.status_code = 401
            mock_response.text = "Unauthorized"
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client
            with pytest.raises(Exception, match="Auth failed"):
                await tm.get_token()


class TestInvalidate:
    pytestmark = pytest.mark.asyncio

    async def test_invalidate_clears_token(self, tm):
        tm._token = "old_token"
        tm._expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        await tm.invalidate()
        assert tm._token is None
        assert tm.is_valid is False

    async def test_invalidate_without_redis(self, tm_no_redis):
        tm_no_redis._token = "old"
        await tm_no_redis.invalidate()
        assert tm_no_redis._token is None
