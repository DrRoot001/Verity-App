"""Account export and deletion endpoints (PRD §29.4).

Deletion is destructive and irreversible once the grace period elapses, so the
request requires the user to type their own email address back — a mis-click
cannot start it (PRD §10.3, type-to-confirm).
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import select, update

from verity.modules.identity.dependencies import CurrentUser, SessionDep
from verity.modules.identity.models import AuthSession, User, normalize_email
from verity.modules.privacy.models import GRACE_PERIOD_DAYS
from verity.modules.privacy.service import PrivacyService
from verity.platform.errors import AppError

privacy_router = APIRouter(prefix="/v1/account", tags=["account"])
admin_privacy_router = APIRouter(prefix="/v1/admin", tags=["admin"])


class DeletionRequest(BaseModel):
    #: Type-to-confirm. Must match the signed-in account exactly.
    confirm_email: str = Field(min_length=3, max_length=320)


class DeletionResponse(BaseModel):
    status: str
    execute_after: dt.datetime
    grace_period_days: int
    cancellable: bool


@privacy_router.get("/export")
async def export_account(principal: CurrentUser, session: SessionDep) -> dict[str, Any]:
    """FR-PRIV-022: everything held about this account, as JSON."""
    return await PrivacyService(session).export(user_id=principal.user.id)


@privacy_router.post("/deletion", response_model=DeletionResponse)
async def request_deletion(
    payload: DeletionRequest, principal: CurrentUser, session: SessionDep
) -> DeletionResponse:
    if normalize_email(payload.confirm_email) != principal.user.email:
        raise AppError.validation(
            "Type your account's email address exactly to confirm.",
            fields={"confirm_email": "does not match this account"},
        )

    job = await PrivacyService(session).request_deletion(user_id=principal.user.id)
    return DeletionResponse(
        status=job.status,
        execute_after=job.execute_after,
        grace_period_days=GRACE_PERIOD_DAYS,
        cancellable=job.is_cancellable,
    )


@privacy_router.delete("/deletion", status_code=204)
async def cancel_deletion(principal: CurrentUser, session: SessionDep) -> None:
    """The cancel path the grace period exists to make possible."""
    await PrivacyService(session).cancel_deletion(user_id=principal.user.id)


class RunResponse(BaseModel):
    executed: list[uuid.UUID]


@privacy_router.post("/deletion/run", response_model=RunResponse, include_in_schema=False)
async def run_due_deletions(principal: CurrentUser, session: SessionDep) -> RunResponse:
    """Run every due job.

    The scheduler that normally drives this is infrastructure, not application
    code; exposing the same entry point keeps the pipeline runnable from a cron
    container without a second implementation. Requires staff.
    """
    from verity.modules.admin.router import require, staff_principal

    staff = await staff_principal(principal, session)
    require(staff, "user.delete")

    service = PrivacyService(session)
    executed: list[uuid.UUID] = []
    for job in await service.due_jobs():
        await service.execute(job)
        executed.append(job.user_id)
    return RunResponse(executed=executed)


class AdminDeletionRequest(BaseModel):
    confirm_email: str = Field(min_length=3, max_length=320)
    reason: str = Field(min_length=8, max_length=500)
    case_id: str = Field(min_length=1, max_length=64)


class AdminDeletionResponse(BaseModel):
    user_id: uuid.UUID
    status: str
    execute_after: dt.datetime


class AdminDeletionCancelRequest(BaseModel):
    reason: str = Field(min_length=8, max_length=500)
    case_id: str = Field(min_length=1, max_length=64)


@admin_privacy_router.post("/users/{user_id}/delete", response_model=AdminDeletionResponse)
async def admin_schedule_deletion(
    user_id: uuid.UUID,
    payload: AdminDeletionRequest,
    principal: CurrentUser,
    session: SessionDep,
) -> AdminDeletionResponse:
    from verity.modules.admin.router import record, require, staff_principal

    staff = await staff_principal(principal, session)
    require(staff, "user.delete")
    if user_id == staff.user_id:
        raise AppError.validation("You cannot schedule deletion of your own admin account.")
    user = (
        await session.execute(select(User).where(User.id == user_id, User.deleted_at.is_(None)))
    ).scalar_one_or_none()
    if user is None:
        raise AppError.not_found("User")
    if normalize_email(payload.confirm_email) != user.email:
        raise AppError.validation(
            "Type the account email exactly to schedule deletion.",
            fields={"confirm_email": "does not match"},
        )
    job = await PrivacyService(session).request_deletion(user_id=user.id)
    await session.execute(
        update(AuthSession)
        .where(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=dt.datetime.now(dt.UTC), revoked_reason="admin_deletion_scheduled")
    )
    await record(
        session,
        staff,
        action="user.delete",
        resource_type="deletion_job",
        resource_id=str(job.id),
        subject_user_id=user.id,
        reason=payload.reason,
        case_id=payload.case_id,
    )
    return AdminDeletionResponse(
        user_id=user.id, status=job.status, execute_after=job.execute_after
    )


@admin_privacy_router.post("/users/{user_id}/deletion/cancel", status_code=204)
async def admin_cancel_deletion(
    user_id: uuid.UUID,
    payload: AdminDeletionCancelRequest,
    principal: CurrentUser,
    session: SessionDep,
) -> None:
    from verity.modules.admin.router import record, require, staff_principal

    staff = await staff_principal(principal, session)
    require(staff, "user.delete")
    await PrivacyService(session).cancel_deletion(user_id=user_id)
    await record(
        session,
        staff,
        action="user.restore",
        resource_type="deletion_job",
        subject_user_id=user_id,
        reason=payload.reason,
        case_id=payload.case_id,
    )
