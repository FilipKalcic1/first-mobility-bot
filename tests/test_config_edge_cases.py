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

    def test_dead_config_vars_stay_removed(self):
        """Config higijena (2026-07-04): mrtve varijable (čitao ih samo
        config) su uklonjene i ne smiju se tiho vratiti bez potrošača —
        cost tracking (admin_api obrisan), drift detekcija (nikad spojena),
        SENTRY_DSN (nikad inicijaliziran), WHATSAPP_VERIFY_TOKEN (GET
        verifikacija je bezuvjetni 'ok')."""
        from config import get_settings

        settings = get_settings()
        for dead in (
            # runda 1 (cost/drift/sentry/verify-token)
            "LLM_INPUT_PRICE_PER_1K", "LLM_OUTPUT_PRICE_PER_1K",
            "DAILY_COST_BUDGET_USD", "DRIFT_BASELINE_DAYS",
            "DRIFT_ANALYSIS_HOURS", "DRIFT_MIN_SAMPLES",
            "SENTRY_DSN", "WHATSAPP_VERIFY_TOKEN",
            # runda 2 (2026-07-04 — 19 config polja bez ijednog čitača + 3
            # .env-only phantoma; verificirano grep-om)
            "REDIS_MAX_CONNECTIONS", "REDIS_SENTINEL_ENABLED",
            "REDIS_SENTINEL_HOSTS", "REDIS_SENTINEL_MASTER",
            "REDIS_SENTINEL_PASSWORD", "AI_MAX_ITERATIONS", "AI_TEMPERATURE",
            "AI_MAX_TOKENS", "EMBEDDING_BATCH_SIZE", "SIMILARITY_THRESHOLD",
            "MAX_TOOLS_FOR_LLM", "CACHE_TTL_TOKEN", "CACHE_TTL_TOOLS",
            "CACHE_TTL_CONVERSATION", "CONFLICT_LOCK_TTL_MINUTES",
            "CONFLICT_SNAPSHOT_TTL_DAYS", "SANITY_CHECKER_ENABLED",
            "BURST_MAX_MESSAGES", "BURST_IDLE_TIMEOUT",
            # .env-only (nikad ni deklarirani kao Settings polja):
            "MOBILITY_API_TOKEN", "MOBILITY_AUDIENCE", "AI_CONFIDENCE_THRESHOLD",
        ):
            assert not hasattr(settings, dead), (
                f"{dead} se vratio u Settings bez živog potrošača")

    def test_live_config_vars_still_present(self):
        """Kontrola: polja s DOKAZANIM potrošačem MORAJU ostati (da čišćenje
        ne ode predaleko)."""
        from config import get_settings

        settings = get_settings()
        for live in ("ADMIN_CORS_ORIGINS", "CACHE_TTL_USER", "CACHE_TTL_CONTEXT",
                     "DEBUG", "GDPR_HASH_SALT", "MAX_CONCURRENT",
                     "AZURE_LLM_MAX_CONCURRENT", "LOG_LEVEL"):
            assert hasattr(settings, live), f"{live} nestao — ima živog potrošača!"

    def test_hash_phone_is_salted(self, monkeypatch):
        """GDPR (D1 2026-07-04): hash_phone mora saltati s GDPR_HASH_SALT —
        inače je mali MSISDN prostor trivijalno rainbow-tableable. Prazan
        salt = staro (nesaljeno) ponašanje radi backward-compata."""
        from services.v2 import telemetry
        from config import get_settings

        get_settings.cache_clear()
        monkeypatch.setenv("GDPR_HASH_SALT", "salt-A")
        a = telemetry.hash_phone("385991234567")
        get_settings.cache_clear()
        monkeypatch.setenv("GDPR_HASH_SALT", "salt-B")
        b = telemetry.hash_phone("385991234567")
        get_settings.cache_clear()
        monkeypatch.delenv("GDPR_HASH_SALT", raising=False)
        unsalted = telemetry.hash_phone("385991234567")
        get_settings.cache_clear()

        assert a != b, "isti broj + različit salt mora dati različit hash"
        assert a != unsalted and b != unsalted, "salt mora promijeniti izlaz"
        assert telemetry.hash_phone("") == ""  # prazan ulaz → prazan

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
