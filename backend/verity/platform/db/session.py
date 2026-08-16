"""Async engine, session factory and the repository guard (PRD §22.3, §28.2).

Sessions are request-scoped and always used inside an explicit transaction, so
a domain write and its outbox event share one commit (PRD §22.3).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from verity.platform.config import settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            settings.database_url,
            echo=settings.db_echo,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_pre_ping=True,
            pool_recycle=1800,
        )
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,
            autoflush=False,
        )
    return _sessionmaker


@asynccontextmanager
async def transaction() -> AsyncIterator[AsyncSession]:
    """Unit of work. Commits on success, rolls back on any exception."""
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def session_dependency() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a transactional session."""
    async with transaction() as session:
        yield session


async def warm_pool() -> None:
    """Open and validate one connection before the process reports ready.

    Without this, the first ``/readyz`` probe pays pool-construction cost and can
    exceed its budget, so a healthy pod briefly advertises itself as down and the
    rollout health gate flaps.
    """
    from sqlalchemy import text

    async with get_sessionmaker()() as session:
        await session.execute(text("SELECT 1"))


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
