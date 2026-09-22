"""Async SQLAlchemy bootstrap for authoritative control-plane state.

The production architecture uses PostgreSQL and Alembic migrations. Automatic table
creation exists only for zero-setup development/tests. SessionFactory is shared by
API and workers while repository classes encapsulate transaction details.
"""
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from minion.config import get_settings
from minion.models import Base

settings = get_settings()
engine = create_async_engine(settings.database_url, pool_pre_ping=True)
SessionFactory = async_sessionmaker(engine, expire_on_commit=False)


async def init_db() -> None:
    """Create tables only in zero-setup/dev mode.

    Production should set MINION_AUTO_CREATE_SCHEMA=false and run Alembic
    migrations before starting the API/worker processes.
    """
    if not settings.auto_create_schema:
        return
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def session_scope() -> AsyncIterator[AsyncSession]:
    async with SessionFactory() as session:
        yield session
