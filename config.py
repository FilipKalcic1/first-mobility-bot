"""
Configuration Module

Centralized configuration with validation.
NO HARDCODED SECRETS - all sensitive values must come from environment.
"""
import os
import logging
from functools import lru_cache
from typing import List, Optional

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger("configuration")

class Settings(BaseSettings):
    
    VERIFY_WHATSAPP_SIGNATURE: bool = True
    # ---
    # APPLICATION
    # ---

    APP_ENV: str = Field(default="development")
    APP_NAME: str = Field(default="MobilityOne Bot")
    APP_VERSION: str = Field(default="11.0.2")
    
    # ---
    # DATABASE - REQUIRED (no localhost defaults for production safety!)
    # ---

    DATABASE_URL: str = Field(
        ...,  # REQUIRED - must be set via environment variable
        description="PostgreSQL connection string (e.g., postgresql+asyncpg://user:pass@host:5432/db)"
    )
    DB_POOL_SIZE: int = Field(default=5)
    DB_MAX_OVERFLOW: int = Field(default=10)
    DB_POOL_RECYCLE: int = Field(default=3600)
    
    # ---
    # SERVICE ROLE (bot or admin - determines database permissions)
    # ---
    SERVICE_ROLE: str = Field(default="bot")

    # ---
    # REDIS
    # ---
    REDIS_URL: str = Field(
        ...,  # REQUIRED - must be set via environment variable
        description="Redis connection string (e.g., redis://host:6379/0)"
    )
    REDIS_MAX_CONNECTIONS: int = Field(default=100)
    REDIS_STATS_KEY_TOOLS: str = Field(default="stats:tools_loaded")

    # Redis Sentinel (optional - for HA failover)
    REDIS_SENTINEL_ENABLED: bool = Field(default=False)
    REDIS_SENTINEL_HOSTS: Optional[str] = Field(
        default=None,
        description="Comma-separated sentinel hosts (e.g., sentinel1:26379,sentinel2:26379)"
    )
    REDIS_SENTINEL_MASTER: str = Field(default="mymaster")
    REDIS_SENTINEL_PASSWORD: Optional[str] = Field(default=None)
    
    # ---
    # INFOBIP (WhatsApp) - Optional, not required for startup
    # ---
    INFOBIP_API_KEY: Optional[str] = Field(default=None)
    INFOBIP_BASE_URL: str = Field(default="api.infobip.com")
    INFOBIP_SECRET_KEY: Optional[str] = Field(
        default=None,
        description="Secret key for webhook HMAC-SHA256 signature validation"
    )
    INFOBIP_SENDER_NUMBER: Optional[str] = Field(default=None)
    WHATSAPP_VERIFY_TOKEN: Optional[str] = Field(
        default=None,
        description="Token for WhatsApp webhook verification (hub.verify_token)"
    )
    
    # ---
    # MOBILITYONE API - REQUIRED (no defaults, must be set via environment)
    # ---
    MOBILITY_API_URL: str = Field(..., description="MobilityOne API base URL")
    MOBILITY_AUTH_URL: str = Field(..., description="MobilityOne OAuth2 token endpoint")
    MOBILITY_CLIENT_ID: str = Field(..., description="OAuth2 client ID")
    MOBILITY_CLIENT_SECRET: str = Field(..., description="OAuth2 client secret")
    MOBILITY_TENANT_ID: str = Field(..., description="Tenant ID for x-tenant header")
    MOBILITY_SCOPE: Optional[str] = Field(default=None, description="OAuth2 scope(s) for token request")
    
    # ---
    # AZURE OPENAI - REQUIRED (no defaults)
    # ---
    AZURE_OPENAI_ENDPOINT: str = Field(..., description="Azure OpenAI endpoint URL")
    AZURE_OPENAI_API_KEY: str = Field(..., description="Azure OpenAI API key")
    AZURE_OPENAI_API_VERSION: str = Field(default="2024-08-01-preview")
    AZURE_OPENAI_DEPLOYMENT_NAME: str = Field(default="gpt-4o-mini")
    AZURE_OPENAI_EMBEDDING_DEPLOYMENT: str = Field(default="text-embedding-ada-002")
    
    # ---
    # AI SETTINGS
    # ---
    AI_MAX_ITERATIONS: int = Field(default=6)
    AI_TEMPERATURE: float = Field(default=0.2)
    AI_MAX_TOKENS: int = Field(default=1500)
    EMBEDDING_BATCH_SIZE: int = Field(default=5)
    SIMILARITY_THRESHOLD: float = Field(default=0.55)

    MAX_TOOLS_FOR_LLM: int = Field(
        default=25,
        description="Maximum tools shown to LLM per request."
    )
    
    # ---
    # RATE LIMITING
    # ---
    RATE_LIMIT_PER_MINUTE: int = Field(default=20)
    RATE_LIMIT_WINDOW: int = Field(default=60)
    
    # ---
    # CACHE TTL (seconds)
    # ---
    CACHE_TTL_TOKEN: int = Field(default=3500)
    CACHE_TTL_USER: int = Field(default=300)
    CACHE_TTL_CONTEXT: int = Field(default=86400)
    CACHE_TTL_TOOLS: int = Field(default=3600)
    CACHE_TTL_CONVERSATION: int = Field(default=1800)
    
    # ---
    # COST TRACKING (LLM token pricing)
    # ---
    LLM_INPUT_PRICE_PER_1K: float = Field(default=0.00015, description="Input token price per 1K tokens")
    LLM_OUTPUT_PRICE_PER_1K: float = Field(default=0.0006, description="Output token price per 1K tokens")
    DAILY_COST_BUDGET_USD: float = Field(default=50.0, description="Daily cost budget alert threshold in USD")

    # ---
    # MODEL DRIFT DETECTION
    # ---
    DRIFT_BASELINE_DAYS: int = Field(default=7, description="Days of data for baseline metrics")
    DRIFT_ANALYSIS_HOURS: int = Field(default=6, description="Hours of recent data to analyze for drift")
    DRIFT_MIN_SAMPLES: int = Field(default=50, description="Minimum samples needed for valid drift analysis")

    # ---
    # GDPR
    # ---
    GDPR_HASH_SALT: Optional[str] = Field(default=None, description="Salt for GDPR-compliant data hashing")

    # ---
    # RAG SCHEDULER
    # ---
    RAG_REFRESH_INTERVAL_HOURS: int = Field(default=6, description="Hours between RAG index refreshes")
    RAG_LOCK_TTL_SECONDS: int = Field(default=600, description="Lock TTL for RAG refresh (prevents concurrent refreshes)")

    # ---
    # CONFLICT RESOLVER
    # ---
    CONFLICT_LOCK_TTL_MINUTES: int = Field(default=30, description="Edit lock duration in minutes")
    CONFLICT_SNAPSHOT_TTL_DAYS: int = Field(default=90, description="Config snapshot retention for compliance")

    # ---
    # ADMIN API
    # ---
    ADMIN_CORS_ORIGINS: str = Field(
        default="https://admin.mobilityone.io",
        description="Comma-separated CORS origins for admin API"
    )
    ADMIN_RATE_LIMIT_PER_MINUTE: int = Field(default=30, description="Admin API rate limit per minute per user")
    ADMIN_ALLOWED_IPS: Optional[str] = Field(
        default=None,
        description="Comma-separated IP whitelist for admin API (e.g., '10.0.0.0/8,192.168.0.0/16'). If None, all IPs allowed."
    )

    # ---
    # DATABASE ROLES (dual-user security)
    # ---
    BOT_DATABASE_URL: Optional[str] = Field(default=None, description="Limited-access DB URL for bot (falls back to DATABASE_URL)")
    ADMIN_DATABASE_URL: Optional[str] = Field(default=None, description="Full-access DB URL for admin (falls back to DATABASE_URL)")

    # ---
    # MONITORING & LOGGING
    # ---
    SENTRY_DSN: Optional[str] = Field(default=None)

    # Log level: DEBUG, INFO, WARNING, ERROR, CRITICAL
    # In production, set to WARNING or ERROR to reduce noise
    LOG_LEVEL: str = Field(default="INFO", description="Logging level (DEBUG/INFO/WARNING/ERROR)")

    # ---
    # CONFIGURATION (Pydantic V2 Style)
    # ---
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore"
    )

    # ---
    # COMPUTED PROPERTIES
    # ---
    @property
    def tenant_id(self) -> str:
        return self.MOBILITY_TENANT_ID
    
    @property
    def swagger_sources(self) -> List[str]:
        """
        Get Swagger sources from environment variable.

        Expected format: SWAGGER_SOURCES=https://api.example.com/service1/swagger.json,https://api.example.com/service2/swagger.json

        Fallback: Auto-discover from MOBILITY_API_URL if configured.
        """
        # Try environment variable first
        sources_env = os.getenv("SWAGGER_SOURCES", "")
        if sources_env:
            return [s.strip() for s in sources_env.split(",") if s.strip()]

        # Fallback: Auto-discover from API base (backward compatibility)
        if not self.MOBILITY_API_URL:
            return []

        base = self.MOBILITY_API_URL.rstrip("/")

        # Default services (can be overridden via env)
        default_services = {
            "automation": "v1.0.0",
            "tenantmgt": "v2.0.0-alpha",
            "vehiclemgt": "v2.0.0-alpha"
        }

        return [
            f"{base}/{service}/swagger/{version}/swagger.json"
            for service, version in default_services.items()
        ]
    
    @property
    def is_production(self) -> bool:
        return self.APP_ENV == "production"

    @property
    def DEBUG(self) -> bool:
        return self.APP_ENV == "development"

    # ---
    # VALIDATORS
    # ---
    @field_validator('MOBILITY_API_URL', 'MOBILITY_AUTH_URL', 'AZURE_OPENAI_ENDPOINT')
    @classmethod
    def validate_url(cls, v: str) -> str:
        if v and not v.startswith(('http://', 'https://')):
            raise ValueError(f"URL must start with http or https: {v}")
        return v.rstrip('/') if v else v

    @model_validator(mode='after')
    def _require_infobip_secret_when_active(self) -> 'Settings':
        """
        If the Infobip/WhatsApp integration is enabled (API key set),
        INFOBIP_SECRET_KEY is mandatory. Without it, webhook HMAC
        validation cannot run — fail startup rather than accept
        unauthenticated callbacks.
        """
        if self.INFOBIP_API_KEY and not self.INFOBIP_SECRET_KEY:
            raise ValueError(
                "INFOBIP_SECRET_KEY is required when INFOBIP_API_KEY is set. "
                "Webhook HMAC validation cannot be bypassed."
            )
        return self


@lru_cache()
def get_settings() -> Settings:
    """
    Get cached settings instance.
    This call will FAIL if required environment variables are missing.
    """
    try:
        return Settings()
    except Exception as e:
        logger.critical(f"FATAL CONFIG ERROR: Could not load settings. Missing env vars? Error: {e}")
        raise e