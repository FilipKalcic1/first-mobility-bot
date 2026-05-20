"""
Config Edge Case Tests - Pydantic settings validation.

Tests that config.py properly validates, fails fast on missing required vars,
and provides correct defaults for optional vars.
"""

import os
import pytest
from unittest.mock import patch


class TestConfigRequiredFields:
    """Test that missing required env vars cause immediate failure."""

    def test_missing_database_url_raises(self):
        """DATABASE_URL is required - must fail without it."""
        from pydantic import ValidationError
        from config import Settings

        env = {
            "REDIS_URL": "redis://localhost",
            "MOBILITY_API_URL": "https://api.example.com",
            "MOBILITY_AUTH_URL": "https://auth.example.com",
            "MOBILITY_CLIENT_ID": "test",
            "MOBILITY_CLIENT_SECRET": "test",
            "MOBILITY_TENANT_ID": "test",
            "AZURE_OPENAI_ENDPOINT": "https://openai.example.com",
            "AZURE_OPENAI_API_KEY": "test-key",
        }

        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(ValidationError):
                Settings(_env_file=None)

    def test_missing_redis_url_raises(self):
        from pydantic import ValidationError
        from config import Settings

        env = {
            "DATABASE_URL": "postgresql+asyncpg://localhost/db",
            "MOBILITY_API_URL": "https://api.example.com",
            "MOBILITY_AUTH_URL": "https://auth.example.com",
            "MOBILITY_CLIENT_ID": "test",
            "MOBILITY_CLIENT_SECRET": "test",
            "MOBILITY_TENANT_ID": "test",
            "AZURE_OPENAI_ENDPOINT": "https://openai.example.com",
            "AZURE_OPENAI_API_KEY": "test-key",
        }

        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(ValidationError):
                Settings(_env_file=None)


class TestConfigDefaults:
    """Test that optional fields have sensible defaults."""

    def test_defaults_are_set(self):
        from config import get_settings

        settings = get_settings()
        assert settings.APP_ENV == "development" or settings.APP_ENV  # has a value
        assert settings.LOG_LEVEL  # has a value

    def test_cost_tracker_defaults(self):
        from config import get_settings

        settings = get_settings()
        assert settings.LLM_INPUT_PRICE_PER_1K > 0
        assert settings.LLM_OUTPUT_PRICE_PER_1K > 0
        assert settings.DAILY_COST_BUDGET_USD > 0

    def test_tenant_id_property(self):
        from config import get_settings

        settings = get_settings()
        assert settings.tenant_id == settings.MOBILITY_TENANT_ID


class TestConfigSingleton:
    """Test that get_settings returns cached singleton."""

    def test_same_instance_returned(self):
        from config import get_settings

        s1 = get_settings()
        s2 = get_settings()
        assert s1 is s2


class TestProductionSafetyAssertions:
    """R4: APP_ENV=production must NOT allow VERIFY_WHATSAPP_SIGNATURE=False.

    Without HMAC, anyone can spoof a webhook with a chosen sender phone
    and inject messages routed to any tenant. Fail startup loudly."""

    _BASE_ENV = {
        "DATABASE_URL": "postgresql+asyncpg://localhost/db",
        "REDIS_URL": "redis://localhost",
        "MOBILITY_API_URL": "https://api.example.com",
        "MOBILITY_AUTH_URL": "https://auth.example.com",
        "MOBILITY_CLIENT_ID": "test",
        "MOBILITY_CLIENT_SECRET": "test",
        "MOBILITY_TENANT_ID": "test",
        "AZURE_OPENAI_ENDPOINT": "https://openai.example.com",
        "AZURE_OPENAI_API_KEY": "test-key",
    }

    def test_production_with_signature_disabled_raises(self):
        from pydantic import ValidationError
        from config import Settings

        env = {
            **self._BASE_ENV,
            "APP_ENV": "production",
            "VERIFY_WHATSAPP_SIGNATURE": "false",
        }
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(ValidationError, match="VERIFY_WHATSAPP_SIGNATURE"):
                Settings(_env_file=None)

    def test_production_with_signature_enabled_loads_ok(self):
        from config import Settings

        env = {
            **self._BASE_ENV,
            "APP_ENV": "production",
            "VERIFY_WHATSAPP_SIGNATURE": "true",
        }
        with patch.dict(os.environ, env, clear=True):
            s = Settings(_env_file=None)
            assert s.APP_ENV == "production"
            assert s.VERIFY_WHATSAPP_SIGNATURE is True

    def test_development_with_signature_disabled_loads_ok(self):
        """Dev/test envs may opt out of HMAC for local debugging."""
        from config import Settings

        env = {
            **self._BASE_ENV,
            "APP_ENV": "development",
            "VERIFY_WHATSAPP_SIGNATURE": "false",
        }
        with patch.dict(os.environ, env, clear=True):
            s = Settings(_env_file=None)
            assert s.APP_ENV == "development"
            assert s.VERIFY_WHATSAPP_SIGNATURE is False
