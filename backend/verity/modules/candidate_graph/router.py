"""Candidate profile and Story Bank endpoints (PRD §25.2 /profile, /stories)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query, status
from pydantic import BaseModel

from verity.modules.candidate_graph.schemas import (
    AchievementUpdate,
    ApprovalResponse,
    BulkApproveRequest,
    EducationUpdate,
    EntityTypeName,
    ExperienceUpdate,
    GraphNodeResponse,
    ProfileResponse,
    ReviewQueueResponse,
    SkillUpdate,
    StoryResponse,
    StoryUpsert,
)
from verity.modules.candidate_graph.service import CandidateGraphService
from verity.modules.identity.dependencies import CurrentUser, SessionDep
from verity.platform.errors import AppError

profile_router = APIRouter(prefix="/v1/profile", tags=["profile"])
stories_router = APIRouter(prefix="/v1/stories", tags=["stories"])

#: Per-entity update contracts. Validating against the right model is what
#: stops an arbitrary JSON body from writing unknown keys into the
#: user_corrected layer.
_UPDATE_MODELS: dict[str, type[BaseModel]] = {
    "experience": ExperienceUpdate,
    "skill": SkillUpdate,
    "education": EducationUpdate,
    "achievement": AchievementUpdate,
}


@profile_router.get("", response_model=ProfileResponse)
async def get_profile(principal: CurrentUser, session: SessionDep) -> ProfileResponse:
    service = CandidateGraphService(session)
    profile = await service.get_profile(principal.user.id)
    approved, pending = await service.profile_counts(principal.user.id)
    return ProfileResponse(
        headline=profile.headline,
        summary=profile.summary,
        experience_level=profile.experience_level,
        target_role=profile.target_role,
        years_experience=None,
        approved_counts=approved,
        pending_counts=pending,
    )


@profile_router.get("/review", response_model=ReviewQueueResponse)
async def review_queue(principal: CurrentUser, session: SessionDep) -> ReviewQueueResponse:
    """Everything awaiting confirmation (PRD §10.3 step 7)."""
    return await CandidateGraphService(session).review_queue(principal.user.id)


@profile_router.get("/{entity_type}", response_model=list[GraphNodeResponse])
async def list_nodes(
    entity_type: EntityTypeName,
    principal: CurrentUser,
    session: SessionDep,
    node_status: str | None = Query(default=None, alias="status"),
) -> list[GraphNodeResponse]:
    return await CandidateGraphService(session).list_nodes(
        principal.user.id, entity_type, status=node_status
    )


@profile_router.patch("/{entity_type}/{node_id}", response_model=GraphNodeResponse)
async def update_node(
    entity_type: EntityTypeName,
    node_id: uuid.UUID,
    payload: dict[str, object],
    principal: CurrentUser,
    session: SessionDep,
) -> GraphNodeResponse:
    """Edits land in the ``user_corrected`` layer, never over the extraction."""
    model = _UPDATE_MODELS.get(entity_type)
    if model is None:
        raise AppError.validation(
            f"'{entity_type}' cannot be edited directly.",
            fields={"entity_type": f"one of {sorted(_UPDATE_MODELS)}"},
        )
    changes = model.model_validate(payload).model_dump(exclude_unset=True)
    return await CandidateGraphService(session).update_node(
        principal.user.id, entity_type, node_id, changes
    )


@profile_router.post("/{entity_type}/{node_id}/approve", response_model=GraphNodeResponse)
async def approve_node(
    entity_type: EntityTypeName,
    node_id: uuid.UUID,
    principal: CurrentUser,
    session: SessionDep,
) -> GraphNodeResponse:
    service = CandidateGraphService(session)
    await service.approve(principal.user.id, entity_type, [node_id])
    nodes = await service.list_nodes(principal.user.id, entity_type)
    return next(n for n in nodes if n.id == node_id)


@profile_router.post("/{entity_type}/{node_id}/reject", response_model=GraphNodeResponse)
async def reject_node(
    entity_type: EntityTypeName,
    node_id: uuid.UUID,
    principal: CurrentUser,
    session: SessionDep,
) -> GraphNodeResponse:
    return await CandidateGraphService(session).reject(principal.user.id, entity_type, node_id)


@profile_router.post("/bulk-approve", response_model=ApprovalResponse)
async def bulk_approve(
    payload: BulkApproveRequest, principal: CurrentUser, session: SessionDep
) -> ApprovalResponse:
    approved, indexed = await CandidateGraphService(session).approve(
        principal.user.id, payload.entity_type, payload.ids
    )
    return ApprovalResponse(approved=approved, indexed=indexed)


# ── Story Bank ───────────────────────────────────────────────────────


@stories_router.get("", response_model=list[StoryResponse])
async def list_stories(
    principal: CurrentUser,
    session: SessionDep,
    story_status: str | None = Query(default=None, alias="status"),
) -> list[StoryResponse]:
    from sqlalchemy import select

    from verity.modules.candidate_graph.models import CandidateStory

    query = select(CandidateStory).where(
        CandidateStory.user_id == principal.user.id, CandidateStory.deleted_at.is_(None)
    )
    if story_status:
        query = query.where(CandidateStory.status == story_status)

    return [_story_response(s) for s in (await session.execute(query)).scalars()]


@stories_router.post("", response_model=StoryResponse, status_code=status.HTTP_201_CREATED)
async def create_story(
    payload: StoryUpsert, principal: CurrentUser, session: SessionDep
) -> StoryResponse:
    from verity.modules.candidate_graph.models import CandidateStory, StoryStatus

    story = CandidateStory(
        user_id=principal.user.id,
        title=payload.title,
        categories=payload.categories,
        situation=payload.situation,
        task=payload.task,
        actions=payload.actions,
        result=payload.result,
        skills_demonstrated=payload.skills_demonstrated,
        source_evidence_ids=payload.source_evidence_ids,
        # A user-authored story still starts suggested: approving is the
        # explicit act that makes it citable by the copilot.
        status=StoryStatus.SUGGESTED,
        speak_time_seconds=_estimate_speak_time(payload),
    )
    session.add(story)
    await session.flush()
    return _story_response(story)


@stories_router.post("/{story_id}/approve", response_model=StoryResponse)
async def approve_story(
    story_id: uuid.UUID, principal: CurrentUser, session: SessionDep
) -> StoryResponse:
    story = await CandidateGraphService(session).approve_story(principal.user.id, story_id)
    return _story_response(story)


def _estimate_speak_time(payload: StoryUpsert) -> int:
    """Words at 150 wpm. Speakability is a product requirement (FR-STORY-006)."""
    words = sum(
        len((text or "").split())
        for text in [payload.situation, payload.task, payload.result, *payload.actions]
    )
    return max(15, round(words / 150 * 60))


def _story_response(story: object) -> StoryResponse:
    return StoryResponse(
        id=story.id,  # type: ignore[attr-defined]
        title=story.title,  # type: ignore[attr-defined]
        categories=list(story.categories or []),  # type: ignore[attr-defined]
        situation=story.situation,  # type: ignore[attr-defined]
        task=story.task,  # type: ignore[attr-defined]
        actions=list(story.actions or []),  # type: ignore[attr-defined]
        result=story.result,  # type: ignore[attr-defined]
        metrics=list(story.metrics or []),  # type: ignore[attr-defined]
        skills_demonstrated=list(story.skills_demonstrated or []),  # type: ignore[attr-defined]
        status=story.status,  # type: ignore[attr-defined]
        speak_time_seconds=story.speak_time_seconds,  # type: ignore[attr-defined]
        source_evidence_ids=list(story.source_evidence_ids or []),  # type: ignore[attr-defined]
    )
