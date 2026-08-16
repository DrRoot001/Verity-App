"""Preparation endpoints (PRD §25.2 /preparation)."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

from verity.modules.identity.dependencies import CurrentUser, SessionDep, VerifiedUser
from verity.modules.preparation.service import PreparationService, task_to_json

preparation_router = APIRouter(prefix="/v1/preparation", tags=["preparation"])


class PlanResponse(BaseModel):
    plan_id: uuid.UUID
    workspace_id: uuid.UUID
    round_index: int
    version: int
    readiness: dict[str, Any]
    tasks: list[dict[str, Any]]


class TaskStatusChange(BaseModel):
    status: Literal["open", "in_progress", "done", "dismissed"]


class StoryGenerationResponse(BaseModel):
    created: int
    titles: list[str] = Field(default_factory=list)


@preparation_router.post("/plans/generate", response_model=PlanResponse)
async def generate_plan(
    workspace_id: uuid.UUID, principal: VerifiedUser, session: SessionDep
) -> PlanResponse:
    result = await PreparationService(session).generate(
        user_id=principal.user.id, workspace_id=workspace_id
    )
    return PlanResponse(
        plan_id=result.plan.id,
        workspace_id=workspace_id,
        round_index=result.plan.round_index,
        version=result.plan.version,
        readiness=result.readiness.to_json(),
        tasks=[
            task_to_json(t) for t in sorted(result.tasks, key=lambda t: -float(t.priority_score))
        ],
    )


@preparation_router.get("/plans", response_model=PlanResponse | None)
async def get_plan(
    workspace_id: uuid.UUID, principal: CurrentUser, session: SessionDep
) -> PlanResponse | None:
    current = await PreparationService(session).current_plan(
        user_id=principal.user.id, workspace_id=workspace_id
    )
    if current is None:
        return None

    plan, tasks = current
    return PlanResponse(
        plan_id=plan.id,
        workspace_id=workspace_id,
        round_index=plan.round_index,
        version=plan.version,
        readiness=plan.summary,
        tasks=[task_to_json(t) for t in tasks],
    )


@preparation_router.patch("/tasks/{task_id}", response_model=dict[str, Any])
async def update_task(
    task_id: uuid.UUID,
    payload: TaskStatusChange,
    principal: CurrentUser,
    session: SessionDep,
) -> dict[str, Any]:
    task = await PreparationService(session).set_task_status(
        user_id=principal.user.id, task_id=task_id, status=payload.status
    )
    return task_to_json(task)


@preparation_router.post("/stories/generate", response_model=StoryGenerationResponse)
async def generate_stories(principal: VerifiedUser, session: SessionDep) -> StoryGenerationResponse:
    """Propose stories from approved experience (FR-STORY-001/002)."""
    created = await PreparationService(session).generate_stories(principal.user.id)
    return StoryGenerationResponse(created=len(created), titles=[s.title for s in created])
