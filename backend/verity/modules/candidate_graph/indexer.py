"""Graph indexing (PRD §11.4 propagation contract, §12.3).

Approving a node must make it retrievable immediately — the seamlessness
contract gives story approval a < 3 s budget (PRD §11.4) and forbids any manual
"reindex" control. Indexing therefore runs in the same transaction as the
approval write, so a crash cannot leave an approved-but-unretrievable node.

Deletion is symmetric: removing a node removes its vectors in the same
transaction (FR-RES-012, §29.4), which is what makes deletion propagation
verifiable rather than eventual.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import and_, delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from verity.ai.providers.base import EmbeddingProvider
from verity.modules.candidate_graph.embeddings import EntityType, GraphEmbedding
from verity.modules.candidate_graph.models import (
    Achievement,
    CandidateStory,
    Certification,
    Education,
    Experience,
    NodeStatus,
    Project,
    Skill,
    StoryStatus,
)
from verity.platform.logging import get_logger

log = get_logger("candidate_graph.indexer")


def _content_hash(text: str, model: str) -> bytes:
    return hashlib.blake2b(f"{model}\x00{text}".encode(), digest_size=32).digest()


def build_projection(entity_type: EntityType, row: Any) -> str:
    """Canonical text embedded for a node.

    The projection is deliberately denser than the raw record: role, company,
    skills, verbs and metrics in one string. Retrieval quality depends far more
    on this shape than on the choice of embedding model.
    """
    match entity_type:
        case EntityType.EXPERIENCE:
            parts = [row.title, row.company_name, row.seniority or "", row.description or ""]
            parts.extend(row.bullets or [])
            return " | ".join(p for p in parts if p)
        case EntityType.PROJECT:
            parts = [row.name, row.role or "", row.description or "", row.impact or ""]
            parts.extend(row.technologies or [])
            return " | ".join(p for p in parts if p)
        case EntityType.SKILL:
            return " | ".join(
                p for p in [row.canonical_name, row.raw_name, row.category or ""] if p
            )
        case EntityType.ACHIEVEMENT:
            metric_text = " ".join(
                f"{m.get('label', '')} {m.get('before', '')} {m.get('after', '')}"
                for m in (row.metrics or [])
                if isinstance(m, dict)
            )
            return " | ".join(
                p for p in [row.statement, row.action or "", row.outcome or "", metric_text] if p
            )
        case EntityType.EDUCATION:
            return " | ".join(p for p in [row.institution, row.degree or "", row.field or ""] if p)
        case EntityType.CERTIFICATION:
            return " | ".join(p for p in [row.name, row.issuer or ""] if p)
        case EntityType.STORY:
            parts = [row.title, row.situation or "", row.task or "", row.result or ""]
            parts.extend(row.actions or [])
            parts.extend(row.categories or [])
            parts.extend(row.skills_demonstrated or [])
            return " | ".join(p for p in parts if p)


_MODEL_BY_TYPE: dict[EntityType, Any] = {
    EntityType.EXPERIENCE: Experience,
    EntityType.PROJECT: Project,
    EntityType.SKILL: Skill,
    EntityType.ACHIEVEMENT: Achievement,
    EntityType.EDUCATION: Education,
    EntityType.CERTIFICATION: Certification,
    EntityType.STORY: CandidateStory,
}


class GraphIndexer:
    def __init__(self, session: AsyncSession, embedder: EmbeddingProvider) -> None:
        self._session = session
        self._embedder = embedder

    async def index_node(self, *, user_id: uuid.UUID, entity_type: EntityType, row: Any) -> bool:
        """Index one node. Returns True when a vector was written.

        Nodes that are not approved are removed from the index rather than
        indexed, so retrieval's approved-only guarantee holds even if a node is
        un-approved after the fact.
        """
        approved = (
            row.status == StoryStatus.APPROVED
            if entity_type is EntityType.STORY
            else row.status == NodeStatus.APPROVED
        )
        if not approved or getattr(row, "deleted_at", None) is not None:
            await self.remove_node(entity_type=entity_type, entity_id=row.id)
            return False

        projection = build_projection(entity_type, row)
        if not projection.strip():
            return False

        digest = _content_hash(projection, self._embedder.model)

        # Skip the embedding call when nothing that affects the vector changed.
        existing = await self._session.execute(
            select(GraphEmbedding.content_hash).where(
                and_(
                    GraphEmbedding.entity_type == str(entity_type),
                    GraphEmbedding.entity_id == row.id,
                    GraphEmbedding.model == self._embedder.model,
                )
            )
        )
        current = existing.scalar_one_or_none()
        if current == digest:
            return False

        embedded = await self._embedder.embed([projection])

        stmt = insert(GraphEmbedding).values(
            user_id=user_id,
            entity_type=str(entity_type),
            entity_id=row.id,
            content_hash=digest,
            model=self._embedder.model,
            embedding=embedded.vectors[0],
            text_projection=projection,
        )
        await self._session.execute(
            stmt.on_conflict_do_update(
                constraint="uq_graph_embeddings_entity",
                set_={
                    "content_hash": stmt.excluded.content_hash,
                    "embedding": stmt.excluded.embedding,
                    "text_projection": stmt.excluded.text_projection,
                    "user_id": stmt.excluded.user_id,
                },
            )
        )
        log.info("node_indexed", entity_type=str(entity_type), entity_id=str(row.id))
        return True

    async def remove_node(self, *, entity_type: EntityType, entity_id: uuid.UUID) -> None:
        await self._session.execute(
            delete(GraphEmbedding).where(
                and_(
                    GraphEmbedding.entity_type == str(entity_type),
                    GraphEmbedding.entity_id == entity_id,
                )
            )
        )

    async def reindex_user(self, user_id: uuid.UUID) -> dict[str, int]:
        """Rebuild a user's whole index. Used after an embedding-model change."""
        counts: dict[str, int] = {}
        for entity_type, model in _MODEL_BY_TYPE.items():
            approved = (
                StoryStatus.APPROVED if entity_type is EntityType.STORY else NodeStatus.APPROVED
            )
            rows = (
                await self._session.execute(
                    select(model).where(
                        model.user_id == user_id,
                        model.status == approved,
                        model.deleted_at.is_(None),
                    )
                )
            ).scalars()
            written = 0
            for row in rows:
                if await self.index_node(user_id=user_id, entity_type=entity_type, row=row):
                    written += 1
            counts[str(entity_type)] = written
        return counts

    async def purge_user(self, user_id: uuid.UUID) -> int:
        """Remove every vector for a user (PRD §29.4 deletion propagation)."""
        result = await self._session.execute(
            delete(GraphEmbedding).where(GraphEmbedding.user_id == user_id)
        )
        # DML results carry rowcount at runtime; the async Result type does not
        # declare it, so read it defensively rather than casting.
        return int(getattr(result, "rowcount", 0) or 0)

    async def index_many(
        self, *, user_id: uuid.UUID, entity_type: EntityType, rows: Sequence[Any]
    ) -> int:
        written = 0
        for row in rows:
            if await self.index_node(user_id=user_id, entity_type=entity_type, row=row):
                written += 1
        return written
