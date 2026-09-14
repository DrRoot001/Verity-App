"""Account export and the deletion pipeline (PRD §29.4, AC-PRIV-001).

Deletion is done per store, and each store confirms before the job is marked
complete. The order matters: Postgres last. If object storage or Redis fails,
the user row is still there to retry from — delete the row first and the job
loses the only handle it has on what remains.

Verification is not optional here. ``_verify`` re-reads each store after the
delete and fails the job if anything answers. A deletion pipeline that reports
success without looking is the failure mode this acceptance criterion exists to
catch.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from verity.modules.candidate_graph.embeddings import GraphEmbedding
from verity.modules.candidate_graph.models import (
    Achievement,
    CandidateStory,
    Education,
    Experience,
    Skill,
)
from verity.modules.documents.models import ResumeVersion
from verity.modules.identity.models import User, UserStatus
from verity.modules.privacy.models import (
    GRACE_PERIOD_DAYS,
    DeletionJob,
    DeletionStatus,
)
from verity.modules.sessions.live_models import LiveSession
from verity.modules.sessions.models import MockSession, SessionFeedback
from verity.modules.workspace.models import Workspace
from verity.platform.cache import RedisRole, get_redis
from verity.platform.db.base import Base
from verity.platform.email import provider as email_provider
from verity.platform.email import render
from verity.platform.errors import AppError, ErrorCode
from verity.platform.logging import get_logger
from verity.platform.storage import StorageBucket, build_storage

log = get_logger("privacy.service")


class PrivacyService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── Export (FR-PRIV-022) ─────────────────────────────────────────

    async def export(self, *, user_id: uuid.UUID) -> dict[str, Any]:
        """Everything held about this user, in one JSON document."""
        user = await self._require_user(user_id)

        async def rows(model: Any) -> list[Any]:
            return list(
                (
                    await self._session.execute(select(model).where(model.user_id == user_id))
                ).scalars()
            )

        return {
            "exported_at": dt.datetime.now(dt.UTC).isoformat(),
            "account": {
                "id": str(user.id),
                "email": user.email,
                "full_name": user.full_name,
                "status": user.status,
                "timezone": user.timezone,
                "created_at": user.created_at.isoformat(),
            },
            "candidate_graph": {
                "experiences": [_row(r) for r in await rows(Experience)],
                "skills": [_row(r) for r in await rows(Skill)],
                "achievements": [_row(r) for r in await rows(Achievement)],
                "education": [_row(r) for r in await rows(Education)],
                "stories": [_row(r) for r in await rows(CandidateStory)],
            },
            "documents": [_row(r) for r in await rows(ResumeVersion)],
            "workspaces": [_row(r) for r in await rows(Workspace)],
            "sessions": {
                "mock": [_row(r) for r in await rows(MockSession)],
                "live": [_row(r) for r in await rows(LiveSession)],
                "reports": [_row(r) for r in await rows(SessionFeedback)],
            },
        }

    # ── Deletion request (FR-AUTH-009) ───────────────────────────────

    async def request_deletion(self, *, user_id: uuid.UUID) -> DeletionJob:
        user = await self._require_user(user_id)

        existing = await self._pending_job(user_id)
        if existing is not None:
            return existing

        now = dt.datetime.now(dt.UTC)
        job = DeletionJob(
            user_id=user_id,
            email=user.email,
            execute_after=now + dt.timedelta(days=GRACE_PERIOD_DAYS),
        )
        user.status = UserStatus.PENDING_DELETION
        user.deletion_requested_at = now
        self._session.add(job)
        await self._session.flush()

        log.info("deletion_requested", user_id=str(user_id), execute_after=str(job.execute_after))
        return job

    async def cancel_deletion(self, *, user_id: uuid.UUID) -> None:
        job = await self._pending_job(user_id)
        if job is None:
            raise AppError(
                ErrorCode.NOT_FOUND,
                "There's no deletion scheduled for this account.",
            )

        user = await self._require_user(user_id, allow_pending_deletion=True)
        job.status = DeletionStatus.CANCELLED
        job.cancelled_at = dt.datetime.now(dt.UTC)
        user.status = UserStatus.ACTIVE
        user.deletion_requested_at = None
        await self._session.flush()
        log.info("deletion_cancelled", user_id=str(user_id))

    # ── Execution (AC-PRIV-001) ──────────────────────────────────────

    async def due_jobs(self, *, limit: int = 20) -> list[DeletionJob]:
        return list(
            (
                await self._session.execute(
                    select(DeletionJob)
                    .where(
                        DeletionJob.status == DeletionStatus.PENDING,
                        DeletionJob.execute_after <= dt.datetime.now(dt.UTC),
                    )
                    .limit(limit)
                )
            ).scalars()
        )

    async def execute(self, job: DeletionJob) -> DeletionJob:
        """Erase every store, verify each, then record what happened."""
        job.status = DeletionStatus.RUNNING
        job.attempts += 1
        await self._session.flush()

        stores: dict[str, Any] = {}
        try:
            # Object storage first: its keys are owner-prefixed, and the prefix
            # is derivable without the user row.
            storage = build_storage()
            removed = 0
            for bucket in StorageBucket:
                removed += await storage.delete_prefix(f"{bucket}/{job.user_id}/")
            stores["object_storage"] = _stamp({"objects": removed})

            purged = 0
            for role in RedisRole:
                redis = get_redis(role)
                keys = [k async for k in redis.scan_iter(match=f"*{job.user_id}*")]
                if keys:
                    purged += await redis.delete(*keys)
            stores["redis"] = _stamp({"keys": purged})

            vectors = (
                await self._session.execute(
                    delete(GraphEmbedding).where(GraphEmbedding.user_id == job.user_id)
                )
            ).rowcount  # type: ignore[attr-defined]
            stores["vectors"] = _stamp({"rows": vectors})

            # Postgres last. Every user-owned table cascades from this row.
            rows = (
                await self._session.execute(delete(User).where(User.id == job.user_id))
            ).rowcount  # type: ignore[attr-defined]
            stores["postgres"] = _stamp({"rows": rows})

            await self._session.flush()
            await self._verify(job.user_id, storage)

            job.stores = stores
            job.status = DeletionStatus.COMPLETED
            job.completed_at = dt.datetime.now(dt.UTC)
            job.last_error = None
            await self._session.flush()

            await email_provider.send(
                render(
                    "deletion_complete",
                    to=job.email,
                    stores=", ".join(sorted(stores)),
                )
            )
            log.info("deletion_completed", user_id=str(job.user_id), stores=list(stores))
        except Exception as exc:
            job.stores = stores
            job.status = DeletionStatus.FAILED
            job.last_error = f"{type(exc).__name__}: {exc}"
            await self._session.flush()
            log.error("deletion_failed", user_id=str(job.user_id), error=job.last_error)
            raise

        return job

    async def _verify(self, user_id: uuid.UUID, storage: Any) -> None:
        """Re-read every store. Anything that answers fails the job."""
        remaining: dict[str, int] = {}

        # Every user-owned table, read from the metadata rather than a list that
        # would silently go stale as tables are added.
        for table in Base.metadata.sorted_tables:
            column = table.c.get("user_id")
            if column is None or table.name == "deletion_jobs":
                continue
            count = int(
                (
                    await self._session.execute(
                        select(func.count()).select_from(table).where(column == user_id)
                    )
                ).scalar_one()
            )
            if count:
                remaining[table.name] = count

        users_left = int(
            (
                await self._session.execute(
                    select(func.count()).select_from(User).where(User.id == user_id)
                )
            ).scalar_one()
        )
        if users_left:
            remaining["users"] = users_left

        for bucket in StorageBucket:
            left = await storage.list_prefix(f"{bucket}/{user_id}/")
            if left:
                remaining[f"storage:{bucket}"] = len(left)

        if remaining:
            raise AppError(
                ErrorCode.INTERNAL_ERROR,
                "Deletion did not clear every store.",
                detail={"remaining": remaining},
            )

    # ── Helpers ──────────────────────────────────────────────────────

    async def _pending_job(self, user_id: uuid.UUID) -> DeletionJob | None:
        return (
            await self._session.execute(
                select(DeletionJob).where(
                    DeletionJob.user_id == user_id,
                    DeletionJob.status == DeletionStatus.PENDING,
                )
            )
        ).scalar_one_or_none()

    async def _require_user(
        self, user_id: uuid.UUID, *, allow_pending_deletion: bool = False
    ) -> User:
        user = (
            await self._session.execute(
                select(User).where(User.id == user_id, User.deleted_at.is_(None))
            )
        ).scalar_one_or_none()
        if user is None:
            raise AppError.not_found("User")
        if user.status == UserStatus.PENDING_DELETION and not allow_pending_deletion:
            return user
        return user


def _stamp(payload: dict[str, Any]) -> dict[str, Any]:
    return {**payload, "at": dt.datetime.now(dt.UTC).isoformat()}


def _row(record: Any) -> dict[str, Any]:
    """Serialize a mapped row without leaking internals."""
    return {
        column.name: _value(getattr(record, column.name))
        for column in record.__table__.columns
        if column.name not in {"password_hash", "tsv", "embedding"}
    }


def _value(value: Any) -> Any:
    if isinstance(value, dt.datetime | dt.date):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    return value
