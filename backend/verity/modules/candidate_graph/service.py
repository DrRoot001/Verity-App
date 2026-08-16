"""Candidate Graph review and approval (PRD §12.2, §10.3).

Approval is the moment an extraction becomes usable as candidate fact, so it is
the moment indexing must happen — the two run in one transaction so an approved
node can never be unretrievable (PRD §11.4, FR-STORY-007).
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from verity.ai.providers.embeddings import build_embedding_provider
from verity.modules.candidate_graph.embeddings import EntityType
from verity.modules.candidate_graph.indexer import GraphIndexer
from verity.modules.candidate_graph.models import (
    Achievement,
    CandidateProfile,
    CandidateStory,
    Certification,
    Education,
    Experience,
    NodeStatus,
    Project,
    ProvenanceSource,
    Skill,
    StoryStatus,
)
from verity.modules.candidate_graph.schemas import (
    GraphNodeResponse,
    ProvenanceResponse,
    ReviewQueueResponse,
)
from verity.platform.errors import AppError, ErrorCode
from verity.platform.logging import get_logger

log = get_logger("candidate_graph.service")

MODEL_BY_NAME: dict[str, Any] = {
    "experience": Experience,
    "project": Project,
    "skill": Skill,
    "achievement": Achievement,
    "education": Education,
    "certification": Certification,
}

_ENTITY_TYPE_BY_NAME: dict[str, EntityType] = {
    "experience": EntityType.EXPERIENCE,
    "project": EntityType.PROJECT,
    "skill": EntityType.SKILL,
    "achievement": EntityType.ACHIEVEMENT,
    "education": EntityType.EDUCATION,
    "certification": EntityType.CERTIFICATION,
}


def _label(entity_type: str, row: Any) -> str:
    match entity_type:
        case "experience":
            return f"{row.effective('title')} at {row.effective('company_name')}"
        case "project":
            return str(row.name)
        case "skill":
            return str(row.canonical_name)
        case "achievement":
            return str(row.statement)[:160]
        case "education":
            return f"{row.degree or ''} {row.institution}".strip()
        case "certification":
            return str(row.name)
    return ""


def _detail(entity_type: str, row: Any) -> dict[str, Any]:
    match entity_type:
        case "experience":
            return {
                "title": row.effective("title"),
                "company_name": row.effective("company_name"),
                "location": row.location,
                "start_month": row.start_month.isoformat() if row.start_month else None,
                "end_month": row.end_month.isoformat() if row.end_month else None,
                "is_current": row.is_current,
                "date_confidence": float(row.date_confidence) if row.date_confidence else None,
                "bullets": list(row.bullets or []),
            }
        case "skill":
            return {"canonical_name": row.canonical_name, "raw_name": row.raw_name}
        case "achievement":
            return {
                "statement": row.statement,
                "metrics": row.metrics,
                "has_quantified_metric": row.has_quantified_metric,
            }
        case "education":
            return {
                "institution": row.institution,
                "degree": row.degree,
                "end_year": row.end_year,
            }
    return {}


def to_node_response(entity_type: str, row: Any) -> GraphNodeResponse:
    return GraphNodeResponse(
        id=row.id,
        entity_type=entity_type,
        label=_label(entity_type, row),
        detail=_detail(entity_type, row),
        provenance=ProvenanceResponse(
            status=row.status,
            source=row.source,
            source_ref=row.source_ref,
            extracted_by=row.extracted_by,
            confidence=float(row.confidence) if row.confidence is not None else None,
            confirmed_at=row.confirmed_at,
            has_user_corrections=bool(row.user_corrected),
        ),
    )


class CandidateGraphService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._indexer = GraphIndexer(session, build_embedding_provider())

    # ── Review ───────────────────────────────────────────────────────

    async def review_queue(self, user_id: uuid.UUID) -> ReviewQueueResponse:
        pending: list[GraphNodeResponse] = []
        counts = {"approved": 0, "pending": 0, "rejected": 0}

        for name, model in MODEL_BY_NAME.items():
            rows = (
                await self._session.execute(
                    select(model).where(model.user_id == user_id, model.deleted_at.is_(None))
                )
            ).scalars()
            for row in rows:
                if row.status == NodeStatus.PENDING_REVIEW:
                    pending.append(to_node_response(name, row))
                    counts["pending"] += 1
                elif row.status == NodeStatus.APPROVED:
                    counts["approved"] += 1
                elif row.status == NodeStatus.REJECTED:
                    counts["rejected"] += 1

        return ReviewQueueResponse(
            pending=pending,
            approved_count=counts["approved"],
            pending_count=counts["pending"],
            rejected_count=counts["rejected"],
        )

    async def list_nodes(
        self, user_id: uuid.UUID, entity_type: str, *, status: str | None = None
    ) -> list[GraphNodeResponse]:
        model = self._require_model(entity_type)
        query = select(model).where(model.user_id == user_id, model.deleted_at.is_(None))
        if status:
            query = query.where(model.status == status)
        rows = (await self._session.execute(query)).scalars()
        return [to_node_response(entity_type, row) for row in rows]

    # ── Mutations ────────────────────────────────────────────────────

    async def update_node(
        self, user_id: uuid.UUID, entity_type: str, node_id: uuid.UUID, changes: dict[str, Any]
    ) -> GraphNodeResponse:
        """Write edits into ``user_corrected``, never over the extraction.

        Keeping the layers separate is what lets a later re-upload diff against
        the original extraction while preserving this edit (FR-RES-006, PP6).
        """
        row = await self._require_node(user_id, entity_type, node_id)

        corrections = dict(row.user_corrected or {})
        for key, value in changes.items():
            if value is None:
                continue
            corrections[key] = value.isoformat() if isinstance(value, dt.date) else value
            # Mirror onto the column so queries and indexing see the edit.
            if hasattr(row, key):
                setattr(row, key, value)

        row.user_corrected = corrections
        await self._session.flush()

        if row.status == NodeStatus.APPROVED:
            await self._reindex(user_id, entity_type, row)

        return to_node_response(entity_type, row)

    async def approve(
        self, user_id: uuid.UUID, entity_type: str, node_ids: list[uuid.UUID]
    ) -> tuple[int, int]:
        """Approve nodes and index them in the same transaction."""
        model = self._require_model(entity_type)
        rows = list(
            (
                await self._session.execute(
                    select(model).where(
                        model.user_id == user_id,
                        model.id.in_(node_ids),
                        model.deleted_at.is_(None),
                    )
                )
            ).scalars()
        )
        if not rows:
            raise AppError.not_found("Entities")

        indexed = 0
        for row in rows:
            row.status = NodeStatus.APPROVED
            row.confirmed_at = dt.datetime.now(dt.UTC)
        await self._session.flush()

        for row in rows:
            if await self._reindex(user_id, entity_type, row):
                indexed += 1

        log.info(
            "graph_nodes_approved",
            entity_type=entity_type,
            approved=len(rows),
            indexed=indexed,
        )
        return len(rows), indexed

    async def reject(
        self, user_id: uuid.UUID, entity_type: str, node_id: uuid.UUID
    ) -> GraphNodeResponse:
        """Rejected nodes are retained, not deleted (PRD FR-ONB-003).

        Deleting would let the next extraction resurrect the same wrong entity;
        keeping it as ``rejected`` records the decision.
        """
        row = await self._require_node(user_id, entity_type, node_id)
        row.status = NodeStatus.REJECTED
        await self._session.flush()
        await self._reindex(user_id, entity_type, row)
        return to_node_response(entity_type, row)

    # ── Stories ──────────────────────────────────────────────────────

    async def approve_story(self, user_id: uuid.UUID, story_id: uuid.UUID) -> CandidateStory:
        """FR-STORY-007: approval makes a story retrievable immediately."""
        story = (
            await self._session.execute(
                select(CandidateStory).where(
                    CandidateStory.id == story_id,
                    CandidateStory.user_id == user_id,
                    CandidateStory.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if story is None:
            raise AppError.not_found("Story")

        if not story.result or not story.situation:
            # A story without a result is not usable in an interview; ask rather
            # than approving something hollow (FR-STORY-003).
            story.status = StoryStatus.NEEDS_DETAIL
            await self._session.flush()
            raise AppError(
                ErrorCode.VALIDATION_FAILED,
                "Add the situation and result before approving this story.",
                detail={
                    "missing": [
                        f
                        for f, v in (("situation", story.situation), ("result", story.result))
                        if not v
                    ]
                },
            )

        story.status = StoryStatus.APPROVED
        await self._session.flush()
        await self._indexer.index_node(user_id=user_id, entity_type=EntityType.STORY, row=story)
        return story

    async def profile_counts(self, user_id: uuid.UUID) -> tuple[dict[str, int], dict[str, int]]:
        approved: dict[str, int] = {}
        pending: dict[str, int] = {}
        for name, model in MODEL_BY_NAME.items():
            rows = (
                await self._session.execute(
                    select(model.status).where(model.user_id == user_id, model.deleted_at.is_(None))
                )
            ).scalars()
            for status in rows:
                if status == NodeStatus.APPROVED:
                    approved[name] = approved.get(name, 0) + 1
                elif status == NodeStatus.PENDING_REVIEW:
                    pending[name] = pending.get(name, 0) + 1
        return approved, pending

    async def get_profile(self, user_id: uuid.UUID) -> CandidateProfile:
        profile = (
            await self._session.execute(
                select(CandidateProfile).where(CandidateProfile.user_id == user_id)
            )
        ).scalar_one_or_none()
        if profile is None:
            profile = CandidateProfile(user_id=user_id)
            self._session.add(profile)
            await self._session.flush()
        return profile

    # ── Helpers ──────────────────────────────────────────────────────

    async def _reindex(self, user_id: uuid.UUID, entity_type: str, row: Any) -> bool:
        return await self._indexer.index_node(
            user_id=user_id, entity_type=_ENTITY_TYPE_BY_NAME[entity_type], row=row
        )

    def _require_model(self, entity_type: str) -> Any:
        model = MODEL_BY_NAME.get(entity_type)
        if model is None:
            raise AppError.validation(
                f"Unknown entity type '{entity_type}'.",
                fields={"entity_type": f"one of {sorted(MODEL_BY_NAME)}"},
            )
        return model

    async def _require_node(self, user_id: uuid.UUID, entity_type: str, node_id: uuid.UUID) -> Any:
        model = self._require_model(entity_type)
        row = (
            await self._session.execute(
                select(model).where(
                    model.id == node_id,
                    model.user_id == user_id,
                    model.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise AppError.not_found(entity_type.title())
        return row


def manual_experience(
    *, user_id: uuid.UUID, title: str, company_name: str, **kwargs: Any
) -> Experience:
    """Manually entered experience is approved on arrival.

    The user typed it, so there is nothing to review — the review gate exists
    for extractions, not for direct input (PRD §8.2 manual profile wizard).
    """
    return Experience(
        user_id=user_id,
        title=title,
        company_name=company_name,
        status=NodeStatus.APPROVED,
        source=ProvenanceSource.USER_MANUAL,
        confirmed_at=dt.datetime.now(dt.UTC),
        **kwargs,
    )
