"""Async SQLAlchemy engine and session factory."""
from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings

_settings = get_settings()

_pool_kwargs: dict = (
    # Tests run each case in its own event loop; pooled asyncpg connections can't cross loops.
    {"poolclass": NullPool} if _settings.environment == "test" else
    {"pool_size": _settings.db_pool_size, "max_overflow": _settings.db_max_overflow,
     "pool_pre_ping": True,      # survive RDS failovers / idle disconnects
     "pool_recycle": 1800}
)

engine = create_async_engine(
    _settings.database_url,
    **_pool_kwargs,
    connect_args={
        # Kill runaway queries server-side; protects the pool under load.
        "server_settings": {
            "statement_timeout": str(_settings.db_statement_timeout_ms),
            "application_name": "arwall-api",
        }
    },
)

SessionLocal = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    """
    One session per request. Routes call `await db.commit()` explicitly when they
    mutate; anything uncommitted is rolled back here if the request fails.
    """
    async with SessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
