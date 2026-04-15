"""
Database Configuration

Async SQLAlchemy setup with proper connection pooling.
DEPENDS ON: config.py only

ARCHITECTURE:
- BOT_DATABASE_URL: Used by bot/workers (limited permissions)
- ADMIN_DATABASE_URL: Used by admin API (full permissions)
- Both use same DB, different PostgreSQL users with different GRANT

CONNECTION POOLING (CRITICAL for production):
- pool_size: Base number of persistent connections
- max_overflow: Extra connections allowed during peak load
- pool_recycle: Recycle connections every N seconds (prevents stale)
- pool_pre_ping: Verify connection is alive before use
"""

import logging
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    create_async_engine,
    AsyncSession,
    async_sessionmaker,
    AsyncEngine
)
from sqlalchemy.pool import NullPool, AsyncAdaptedQueuePool

from config import get_settings
from base import Base  # Import shared Base

logger = logging.getLogger(__name__)
settings = get_settings()

# ---
# DUAL-USER DATABASE URLS
# ---
# Bot uses limited user (can't see admin tables)
# Admin uses full-access user

BOT_DATABASE_URL = settings.BOT_DATABASE_URL or settings.DATABASE_URL
ADMIN_DATABASE_URL = settings.ADMIN_DATABASE_URL or settings.DATABASE_URL

# Detect which service we are (set in docker-compose environment)
SERVICE_TYPE = settings.SERVICE_ROLE  # "bot" or "admin" (from config.py)


def get_database_url() -> str:
    """
    Get appropriate database URL based on service type.

    - admin service -> ADMIN_DATABASE_URL (full access)
    - api/worker -> BOT_DATABASE_URL (limited access)
    """
    if SERVICE_TYPE == "admin":
        logger.info("Using ADMIN database connection (full access)")
        return ADMIN_DATABASE_URL
    else:
        logger.info(f"Using BOT database connection (limited access) for {SERVICE_TYPE}")
        return BOT_DATABASE_URL


def create_engine_with_pool(database_url: str) -> AsyncEngine:
    """
    Create async engine with proper connection pooling.

    CRITICAL: Without pooling, each request creates new connection.
    With 100 concurrent users = 100 connections = PostgreSQL crash!

    Pool settings from config:
    - DB_POOL_SIZE: 5 (base connections)
    - DB_MAX_OVERFLOW: 10 (peak = 15 total max)
    - DB_POOL_RECYCLE: 3600 (recycle after 1 hour)
    """
    # Test environment - no pooling needed
    if settings.APP_ENV == "test":
        logger.info("Test mode: Using NullPool (no connection pooling)")
        return create_async_engine(
            database_url,
            echo=settings.DEBUG,
            poolclass=NullPool
        )

    # Production/Development - use connection pool
    logger.info(
        f"Creating engine with pool: size={settings.DB_POOL_SIZE}, "
        f"overflow={settings.DB_MAX_OVERFLOW}, recycle={settings.DB_POOL_RECYCLE}s"
    )

    return create_async_engine(
        database_url,
        echo=settings.DEBUG,

        poolclass=AsyncAdaptedQueuePool,
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=settings.DB_MAX_OVERFLOW,
        pool_recycle=settings.DB_POOL_RECYCLE,
        pool_pre_ping=True,
        pool_timeout=30,
        pool_use_lifo=True,
    )


# Create async engine with pooling
engine: AsyncEngine = create_engine_with_pool(get_database_url())

# Session factory
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False
)

# NOTE: Base is imported from base.py (shared between models and database)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency for database sessions."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


async def init_db() -> None:
    """Initialize database tables."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def close_db() -> None:
    """Close database connections."""
    await engine.dispose()
