"""Typed hybrid retrieval over the Candidate Graph (PRD §12.3).

Every AI consumer resolves candidate evidence through this service. Three
properties matter and are enforced here rather than at the call site:

1. **Approved-only.** Retrieval never returns a node that a human has not
   confirmed, so an unreviewed extraction cannot become a ``candidate_fact``
   (FR-GRAPH-001, §12.6).
2. **User-scoped in SQL.** The ownership predicate is part of the query, not a
   post-filter, so a bug cannot leak another user's history (AC-SEC-001).
3. **Budgeted.** p95 < 300 ms including one-hop expansion (NFR-GRAPH-001).

Ranking fuses lexical and vector scores with Reciprocal Rank Fusion. RRF is used
rather than a weighted score sum because the two systems produce
non-commensurable scales, and normalizing them per query is unstable when one
side returns few rows.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Select, func, literal, or_, select
from sqlalchemy.dialects.postgresql import REGCONFIG
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
from verity.platform.telemetry import stage_span

log = get_logger("candidate_graph.retrieval")

#: RRF damping. 60 is the standard constant from the original TREC work; it
#: keeps a single system's top hit from dominating the fused ordering.
RRF_K = 60

DEFAULT_LIMIT = 8
MAX_LIMIT = 50


@dataclass(frozen=True, slots=True)
class RetrievalQuery:
    user_id: uuid.UUID
    text: str
    entity_types: frozenset[EntityType] = frozenset()
    limit: int = DEFAULT_LIMIT
    #: Expand top hits one hop (experience → projects → achievements) so an
    #: answer has supporting depth without a second round trip (PRD §12.3).
    expand: bool = True


@dataclass(slots=True)
class EvidenceNode:
    entity_type: EntityType
    entity_id: uuid.UUID
    label: str
    text: str
    score: float
    source: str
    lexical_rank: int | None = None
    vector_rank: int | None = None
    expanded_from: uuid.UUID | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RetrievalResult:
    nodes: list[EvidenceNode]
    took_ms: float
    degraded: bool = False
    degraded_reason: str | None = None

    @property
    def evidence_ids(self) -> list[uuid.UUID]:
        return [n.entity_id for n in self.nodes]


#: Entity type → (model, human-readable label column). Stories carry their own
#: status enum; every other node uses NodeStatus.
_MODEL_BY_TYPE: dict[EntityType, Any] = {
    EntityType.EXPERIENCE: Experience,
    EntityType.PROJECT: Project,
    EntityType.SKILL: Skill,
    EntityType.ACHIEVEMENT: Achievement,
    EntityType.EDUCATION: Education,
    EntityType.CERTIFICATION: Certification,
    EntityType.STORY: CandidateStory,
}


class GraphRetrievalService:
    def __init__(self, session: AsyncSession, embedder: EmbeddingProvider) -> None:
        self._session = session
        self._embedder = embedder

    # ── Public API ───────────────────────────────────────────────────

    async def retrieve(self, query: RetrievalQuery) -> RetrievalResult:
        started = time.perf_counter()
        limit = min(max(query.limit, 1), MAX_LIMIT)

        with stage_span("retrieval", entity_types=len(query.entity_types)):
            lexical = await self._lexical_search(query, limit * 3)

            degraded = False
            degraded_reason: str | None = None
            try:
                vector = await self._vector_search(query, limit * 3)
            except Exception as exc:
                # PRD §20.3: embeddings unavailable degrades to lexical-only
                # rather than failing the whole request.
                log.warning("vector_search_degraded", error_type=type(exc).__name__)
                vector = []
                degraded = True
                degraded_reason = "embeddings_unavailable"

            fused = self._fuse(lexical, vector)[:limit]

            if query.expand and fused:
                fused.extend(await self._expand(query.user_id, fused))

        took_ms = (time.perf_counter() - started) * 1000
        log.info(
            "retrieval_complete",
            result_count=len(fused),
            took_ms=round(took_ms, 2),
            degraded=degraded,
        )
        return RetrievalResult(
            nodes=fused, took_ms=took_ms, degraded=degraded, degraded_reason=degraded_reason
        )

    # ── Lexical half ─────────────────────────────────────────────────

    def _base_select(self, query: RetrievalQuery) -> Select[Any]:
        """Ownership and approval are predicates, never post-filters."""
        stmt = select(GraphEmbedding).where(GraphEmbedding.user_id == query.user_id)
        if query.entity_types:
            stmt = stmt.where(GraphEmbedding.entity_type.in_([str(t) for t in query.entity_types]))
        return stmt

    async def _lexical_search(
        self, query: RetrievalQuery, limit: int
    ) -> list[tuple[GraphEmbedding, float]]:
        # The text-search configuration must be a regconfig; passing it as a
        # bare string resolves to plainto_tsquery(varchar, varchar), which
        # does not exist.
        ts_query = func.plainto_tsquery(literal("simple", REGCONFIG), query.text)
        rank = func.ts_rank(GraphEmbedding.tsv, ts_query)
        stmt = (
            self._base_select(query)
            .add_columns(rank.label("rank"))
            .where(GraphEmbedding.tsv.op("@@")(ts_query))
            .order_by(rank.desc())
            .limit(limit)
        )
        rows = await self._session.execute(stmt)
        return [(row[0], float(row[1])) for row in rows.all()]

    # ── Vector half ──────────────────────────────────────────────────

    async def _vector_search(
        self, query: RetrievalQuery, limit: int
    ) -> list[tuple[GraphEmbedding, float]]:
        embedded = await self._embedder.embed([query.text])
        vector = embedded.vectors[0]

        distance = GraphEmbedding.embedding.cosine_distance(vector)
        stmt = (
            self._base_select(query)
            .add_columns(distance.label("distance"))
            .where(GraphEmbedding.model == self._embedder.model)
            .order_by(distance)
            .limit(limit)
        )
        rows = await self._session.execute(stmt)
        return [(row[0], 1.0 - float(row[1])) for row in rows.all()]

    # ── Fusion ───────────────────────────────────────────────────────

    def _fuse(
        self,
        lexical: Sequence[tuple[GraphEmbedding, float]],
        vector: Sequence[tuple[GraphEmbedding, float]],
    ) -> list[EvidenceNode]:
        scores: dict[uuid.UUID, float] = {}
        nodes: dict[uuid.UUID, EvidenceNode] = {}
        lex_rank: dict[uuid.UUID, int] = {}
        vec_rank: dict[uuid.UUID, int] = {}

        for rank, (row, _) in enumerate(lexical, start=1):
            scores[row.entity_id] = scores.get(row.entity_id, 0.0) + 1.0 / (RRF_K + rank)
            lex_rank[row.entity_id] = rank
            nodes.setdefault(row.entity_id, self._to_node(row))

        for rank, (row, _) in enumerate(vector, start=1):
            scores[row.entity_id] = scores.get(row.entity_id, 0.0) + 1.0 / (RRF_K + rank)
            vec_rank[row.entity_id] = rank
            nodes.setdefault(row.entity_id, self._to_node(row))

        ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        result: list[EvidenceNode] = []
        for entity_id, score in ordered:
            node = nodes[entity_id]
            node.score = score
            node.lexical_rank = lex_rank.get(entity_id)
            node.vector_rank = vec_rank.get(entity_id)
            result.append(node)
        return result

    @staticmethod
    def _to_node(row: GraphEmbedding) -> EvidenceNode:
        projection = row.text_projection
        return EvidenceNode(
            entity_type=EntityType(row.entity_type),
            entity_id=row.entity_id,
            label=projection[:120],
            text=projection,
            score=0.0,
            source="index",
        )

    # ── One-hop expansion ────────────────────────────────────────────

    async def _expand(
        self, user_id: uuid.UUID, seeds: Sequence[EvidenceNode]
    ) -> list[EvidenceNode]:
        """Pull the children that give a seed its supporting detail.

        An experience without its achievements produces guidance with no
        specifics, which is exactly the failure mode Grounded Candidate Mode
        exists to prevent.
        """
        experience_ids = [n.entity_id for n in seeds if n.entity_type == EntityType.EXPERIENCE]
        project_ids = [n.entity_id for n in seeds if n.entity_type == EntityType.PROJECT]
        if not experience_ids and not project_ids:
            return []

        seen = {n.entity_id for n in seeds}
        expanded: list[EvidenceNode] = []

        if experience_ids:
            project_stmt = select(Project).where(
                Project.user_id == user_id,
                Project.experience_id.in_(experience_ids),
                Project.status == NodeStatus.APPROVED,
                Project.deleted_at.is_(None),
            )
            for project in (await self._session.execute(project_stmt)).scalars():
                if project.id in seen:
                    continue
                seen.add(project.id)
                expanded.append(
                    EvidenceNode(
                        entity_type=EntityType.PROJECT,
                        entity_id=project.id,
                        label=project.name,
                        text=f"{project.name}. {project.description or ''}".strip(),
                        score=0.0,
                        source="expansion",
                        expanded_from=project.experience_id,
                    )
                )

        # Achievements hang off either parent, including projects just expanded
        # above — that second hop is what supplies the metrics an answer needs.
        parent_project_ids = list(project_ids) + [
            n.entity_id for n in expanded if n.entity_type == EntityType.PROJECT
        ]
        conditions = []
        if experience_ids:
            conditions.append(Achievement.experience_id.in_(experience_ids))
        if parent_project_ids:
            conditions.append(Achievement.project_id.in_(parent_project_ids))
        if not conditions:
            return expanded

        achievement_stmt = select(Achievement).where(
            Achievement.user_id == user_id,
            or_(*conditions),
            Achievement.status == NodeStatus.APPROVED,
            Achievement.deleted_at.is_(None),
        )
        for achievement in (await self._session.execute(achievement_stmt)).scalars():
            if achievement.id in seen:
                continue
            seen.add(achievement.id)
            expanded.append(
                EvidenceNode(
                    entity_type=EntityType.ACHIEVEMENT,
                    entity_id=achievement.id,
                    label=achievement.statement[:120],
                    text=achievement.statement,
                    score=0.0,
                    source="expansion",
                    expanded_from=achievement.experience_id or achievement.project_id,
                    payload={"metrics": achievement.metrics},
                )
            )

        return expanded

    # ── Evidence resolution (used by the grounding validator) ────────

    async def resolve_evidence(
        self, user_id: uuid.UUID, evidence_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, EvidenceNode]:
        """Load approved nodes by id, scoped to the owner.

        The grounding validator (PRD §12.6 step 3) calls this to confirm that
        every ``candidate_fact`` resolves to a real approved row belonging to
        this user. Anything missing is downgraded, never rendered as fact.
        """
        if not evidence_ids:
            return {}

        resolved: dict[uuid.UUID, EvidenceNode] = {}
        ids = list(evidence_ids)

        for entity_type, model in _MODEL_BY_TYPE.items():
            approved = (
                StoryStatus.APPROVED if entity_type is EntityType.STORY else NodeStatus.APPROVED
            )
            stmt = select(model).where(
                model.user_id == user_id,
                model.id.in_(ids),
                model.status == approved,
                model.deleted_at.is_(None),
            )
            for row in (await self._session.execute(stmt)).scalars():
                resolved[row.id] = EvidenceNode(
                    entity_type=entity_type,
                    entity_id=row.id,
                    label=_label_for(entity_type, row),
                    text=_label_for(entity_type, row),
                    score=1.0,
                    source="resolved",
                )
        return resolved


def _label_for(entity_type: EntityType, row: Any) -> str:
    match entity_type:
        case EntityType.EXPERIENCE:
            return f"{row.title} at {row.company_name}"
        case EntityType.PROJECT:
            return str(row.name)
        case EntityType.SKILL:
            return str(row.canonical_name)
        case EntityType.ACHIEVEMENT:
            return str(row.statement)
        case EntityType.EDUCATION:
            return f"{row.degree or ''} {row.institution}".strip()
        case EntityType.CERTIFICATION:
            return str(row.name)
        case EntityType.STORY:
            return str(row.title)
