"""Database lifecycle.

The first implementation used a module-global engine/session factory, which made
integration tests and multi-instance deployment unnecessarily rigid. Database is
now an injected resource owned by the FastAPI application.
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from minion.models import Base


class Database:
    def __init__(self, url: str):
        self.engine = create_async_engine(url, pool_pre_ping=True)
        self.session_factory = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    async def initialize(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def ready(self) -> bool:
        try:
            async with self.engine.connect() as connection:
                await connection.exec_driver_sql("SELECT 1")
            return True
        except (OSError, RuntimeError):
            return False

    async def close(self) -> None:
        await self.engine.dispose()
