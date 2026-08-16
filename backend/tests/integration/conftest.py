"""Database-backed test fixtures.

Each test runs inside a transaction that is rolled back afterwards, so tests are
isolated without truncating tables between runs.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from verity.ai.providers.embeddings import HashedNgramEmbedding
from verity.modules.candidate_graph.indexer import GraphIndexer
from verity.modules.candidate_graph.retrieval import GraphRetrievalService
from verity.modules.identity.models import User, normalize_email
from verity.platform.config import settings


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    """A session bound to an outer transaction that is always rolled back.

    The engine is created and disposed per test rather than reusing the shared
    application engine: pytest-asyncio gives each test a fresh event loop, and
    asyncpg connections cannot outlive the loop they were opened on.
    ``NullPool`` keeps no connection alive past teardown.
    """
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            maker = async_sessionmaker(bind=connection, expire_on_commit=False)
            session = maker()
            try:
                yield session
            finally:
                await session.close()
                await transaction.rollback()
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def user(db: AsyncSession) -> User:
    record = User(
        email=normalize_email(f"candidate-{uuid.uuid4().hex[:12]}@verity.test"),
        full_name="Test Candidate",
    )
    db.add(record)
    await db.flush()
    return record


@pytest_asyncio.fixture
async def other_user(db: AsyncSession) -> User:
    """A second tenant, for cross-user isolation assertions."""
    record = User(
        email=normalize_email(f"other-{uuid.uuid4().hex[:12]}@verity.test"),
        full_name="Other Candidate",
    )
    db.add(record)
    await db.flush()
    return record


@pytest.fixture
def embedder() -> HashedNgramEmbedding:
    return HashedNgramEmbedding()


@pytest.fixture
def indexer(db: AsyncSession, embedder: HashedNgramEmbedding) -> GraphIndexer:
    return GraphIndexer(db, embedder)


@pytest.fixture
def retrieval(db: AsyncSession, embedder: HashedNgramEmbedding) -> GraphRetrievalService:
    return GraphRetrievalService(db, embedder)
