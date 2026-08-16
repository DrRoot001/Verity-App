"""Workspace lifecycle and propagation (PRD §11, §11.4).

The propagation contract is the product promise: the user never manually
synchronizes anything (FR-WS-010). Changing a resume binding, ingesting a JD or
approving a story all reach the surfaces that depend on them, because every
write that invalidates context goes through here and marks the bundle stale.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from verity.modules.documents.extraction.jd_parser import (
    parse_job_description,
    serialize_facts,
    serialize_inferences,
)
from verity.modules.documents.models import Job, JobStatus, ResumeVersion, SourceType
from verity.modules.workspace.context import ContextBundle, ContextResolver
from verity.modules.workspace.models import InterviewStage, Workspace, WorkspaceContext
from verity.platform.errors import AppError, ErrorCode
from verity.platform.logging import get_logger

log = get_logger("workspace.service")

#: Terminal stages do not advance further.
_TERMINAL_STAGES = frozenset({InterviewStage.OFFER, InterviewStage.REJECTED})


@dataclass(slots=True)
class ImpactPreview:
    """What changes if a resume rebinding is confirmed (PRD FR-WS-002).

    Rebinding is an explicit action with a visible consequence, never a silent
    swap, because it changes which evidence every downstream answer can cite.
    """

    current_version_id: uuid.UUID | None
    new_version_id: uuid.UUID
    match_score_before: float | None
    match_score_after: float
    requirements_changed: int
    gaps_before: int
    gaps_after: int


class WorkspaceService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._context = ContextResolver(session)

    # ── Lifecycle ────────────────────────────────────────────────────

    async def create(
        self,
        *,
        user_id: uuid.UUID,
        company_name: str,
        role_title: str,
        seniority: str | None = None,
        stage: str = InterviewStage.PREPARING,
        interview_at: dt.datetime | None = None,
        timezone: str | None = None,
        resume_version_id: uuid.UUID | None = None,
        jd_text: str | None = None,
        integrity_mode: str = "assisted",
    ) -> tuple[Workspace, ContextBundle]:
        """FR-WS-001: only company and role are required.

        Everything else is progressively enrichable, so a user who learns about
        an interview an hour before it happens can still start.
        """
        if resume_version_id is not None:
            await self._require_resume_version(user_id, resume_version_id)

        workspace = Workspace(
            user_id=user_id,
            company_name=company_name.strip(),
            role_title=role_title.strip(),
            seniority=seniority,
            stage=stage,
            interview_at=interview_at,
            timezone=timezone,
            resume_version_id=resume_version_id,
            integrity_mode=integrity_mode,
        )
        self._session.add(workspace)
        await self._session.flush()

        if jd_text:
            await self.attach_job_description(
                user_id=user_id, workspace=workspace, raw_text=jd_text
            )

        bundle = await self._context.rebuild(workspace=workspace)
        log.info("workspace_created", workspace_id=str(workspace.id))
        return workspace, bundle

    async def get(self, *, user_id: uuid.UUID, workspace_id: uuid.UUID) -> Workspace:
        workspace = (
            await self._session.execute(
                select(Workspace).where(
                    Workspace.id == workspace_id,
                    Workspace.user_id == user_id,
                    Workspace.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if workspace is None:
            raise AppError.not_found("Workspace")
        return workspace

    async def list_for_user(
        self, *, user_id: uuid.UUID, include_archived: bool = False, limit: int = 50
    ) -> list[Workspace]:
        query = select(Workspace).where(
            Workspace.user_id == user_id, Workspace.deleted_at.is_(None)
        )
        if not include_archived:
            query = query.where(Workspace.archived_at.is_(None))
        query = query.order_by(Workspace.updated_at.desc()).limit(min(limit, 100))
        return list((await self._session.execute(query)).scalars())

    async def update(
        self, *, user_id: uuid.UUID, workspace_id: uuid.UUID, changes: dict[str, Any]
    ) -> ContextBundle:
        workspace = await self.get(user_id=user_id, workspace_id=workspace_id)

        context_affecting = {
            "company_name",
            "role_title",
            "seniority",
            "stage",
            "role_family",
            "notes",
        }
        touched_context = False

        for field_name, value in changes.items():
            if value is None or not hasattr(workspace, field_name):
                continue
            setattr(workspace, field_name, value)
            if field_name in context_affecting:
                touched_context = True

        await self._session.flush()

        if touched_context:
            return await self._context.rebuild(workspace=workspace)
        return await self._context.resolve(workspace_id=workspace.id, user_id=user_id)

    async def archive(self, *, user_id: uuid.UUID, workspace_id: uuid.UUID) -> None:
        workspace = await self.get(user_id=user_id, workspace_id=workspace_id)
        workspace.archived_at = dt.datetime.now(dt.UTC)
        await self._session.flush()

    async def delete(self, *, user_id: uuid.UUID, workspace_id: uuid.UUID) -> None:
        """Soft delete. The deletion pipeline hard-deletes later (PRD §29.4)."""
        workspace = await self.get(user_id=user_id, workspace_id=workspace_id)
        workspace.deleted_at = dt.datetime.now(dt.UTC)
        await self._session.flush()

    async def advance_stage(
        self, *, user_id: uuid.UUID, workspace_id: uuid.UUID, stage: str
    ) -> ContextBundle:
        """Stage change opens a new round; context stays workspace-scoped."""
        workspace = await self.get(user_id=user_id, workspace_id=workspace_id)
        if workspace.stage in _TERMINAL_STAGES:
            raise AppError(
                ErrorCode.INVALID_STATE_TRANSITION,
                f"This workspace is closed ({workspace.stage}).",
                detail={"current_stage": workspace.stage},
            )

        if stage != workspace.stage:
            workspace.stage = stage
            if stage not in _TERMINAL_STAGES:
                workspace.round_index += 1
            await self._session.flush()

        return await self._context.rebuild(workspace=workspace)

    # ── Job description ──────────────────────────────────────────────

    async def attach_job_description(
        self,
        *,
        user_id: uuid.UUID,
        workspace: Workspace,
        raw_text: str,
        source_url: str | None = None,
    ) -> Job:
        if len(raw_text.strip()) < 80:
            raise AppError.validation(
                "That job description is too short to analyse.",
                fields={"raw_text": "at least 80 characters"},
            )

        parsed = parse_job_description(raw_text, title_hint=workspace.role_title)

        job = Job(
            user_id=user_id,
            company_name=workspace.company_name,
            title=parsed.facts.title or workspace.role_title,
            location=parsed.facts.location,
            remote_policy=parsed.facts.remote_policy,
            source_type=SourceType.URL if source_url else SourceType.PASTE,
            source_url=source_url,
            raw_text=raw_text,
            facts=serialize_facts(parsed.facts),
            inferences=serialize_inferences(parsed.inferences),
            status=JobStatus.READY,
        )
        if parsed.inferences.seniority is not None:
            job.seniority = parsed.inferences.seniority.value

        self._session.add(job)
        await self._session.flush()

        workspace.job_id = job.id
        # Only fill a blank; a seniority the user set outranks an inference.
        if workspace.seniority is None and job.seniority:
            workspace.seniority = job.seniority
        await self._session.flush()

        await self._context.mark_stale(workspace.id)
        log.info("jd_attached", workspace_id=str(workspace.id), job_id=str(job.id))
        return job

    # ── Resume rebinding (FR-WS-002) ─────────────────────────────────

    async def preview_resume_rebind(
        self, *, user_id: uuid.UUID, workspace_id: uuid.UUID, resume_version_id: uuid.UUID
    ) -> ImpactPreview:
        """Compute the consequence without committing it.

        The preview runs the real match against the candidate binding, then
        rolls the workspace back, so what the user is shown is exactly what
        confirming would produce.
        """
        workspace = await self.get(user_id=user_id, workspace_id=workspace_id)
        await self._require_resume_version(user_id, resume_version_id)

        before = await self._context.resolve(workspace_id=workspace.id, user_id=user_id)
        original = workspace.resume_version_id

        workspace.resume_version_id = resume_version_id
        await self._session.flush()
        after = await self._context.rebuild(workspace=workspace)

        workspace.resume_version_id = original
        await self._session.flush()
        await self._context.rebuild(workspace=workspace)

        changed = sum(
            1
            for a, b in zip(after.match.requirements, before.match.requirements, strict=False)
            if a.status != b.status
        )
        return ImpactPreview(
            current_version_id=original,
            new_version_id=resume_version_id,
            match_score_before=before.match.overall_score,
            match_score_after=after.match.overall_score,
            requirements_changed=changed,
            gaps_before=len(before.match.gaps),
            gaps_after=len(after.match.gaps),
        )

    async def bind_resume(
        self, *, user_id: uuid.UUID, workspace_id: uuid.UUID, resume_version_id: uuid.UUID
    ) -> ContextBundle:
        workspace = await self.get(user_id=user_id, workspace_id=workspace_id)
        await self._require_resume_version(user_id, resume_version_id)

        workspace.resume_version_id = resume_version_id
        await self._session.flush()

        bundle = await self._context.rebuild(workspace=workspace)
        log.info(
            "workspace_resume_bound",
            workspace_id=str(workspace.id),
            resume_version_id=str(resume_version_id),
        )
        return bundle

    # ── Propagation (PRD §11.4) ──────────────────────────────────────

    async def invalidate_for_user(self, user_id: uuid.UUID) -> int:
        """Mark every active workspace stale after a graph-wide change.

        Approving an experience or a story changes the evidence available to
        every opportunity, so all of them need recomputation. Marking is cheap;
        the rebuild happens on next read or in a background job.
        """
        rows = list(
            (
                await self._session.execute(
                    select(WorkspaceContext)
                    .join(Workspace, Workspace.id == WorkspaceContext.workspace_id)
                    .where(
                        Workspace.user_id == user_id,
                        Workspace.deleted_at.is_(None),
                        Workspace.archived_at.is_(None),
                    )
                )
            ).scalars()
        )
        for row in rows:
            row.stale = True
        await self._session.flush()
        return len(rows)

    async def context(self, *, user_id: uuid.UUID, workspace_id: uuid.UUID) -> ContextBundle:
        """The single entry point every consumer uses (FR-WS-003)."""
        return await self._context.resolve(workspace_id=workspace_id, user_id=user_id)

    async def _require_resume_version(
        self, user_id: uuid.UUID, resume_version_id: uuid.UUID
    ) -> ResumeVersion:
        version = (
            await self._session.execute(
                select(ResumeVersion).where(
                    ResumeVersion.id == resume_version_id,
                    ResumeVersion.user_id == user_id,
                    ResumeVersion.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if version is None:
            raise AppError.not_found("Resume version")
        return version
