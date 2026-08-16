"""Workspace and resume endpoints (PRD §25.2 /workspaces, /resumes).

Routers contain wiring only: validation, authorization and shaping. All
behaviour lives in the services (PRD §22.2), which is why these handlers are
short enough to read at a glance.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, File, Form, Response, UploadFile, status
from pydantic import BaseModel, Field

from verity.modules.documents.service import ResumeIngestionService
from verity.modules.identity.dependencies import CurrentUser, SessionDep, VerifiedUser
from verity.modules.workspace.service import WorkspaceService
from verity.platform.errors import AppError, ErrorCode
from verity.platform.ratelimit import RateLimitScope, limiter

workspaces_router = APIRouter(prefix="/v1/workspaces", tags=["workspaces"])
resumes_router = APIRouter(prefix="/v1/resumes", tags=["resumes"])


# ── Contracts ────────────────────────────────────────────────────────


class WorkspaceCreate(BaseModel):
    company_name: str = Field(min_length=1, max_length=200)
    role_title: str = Field(min_length=1, max_length=200)
    seniority: str | None = Field(default=None, max_length=32)
    stage: str = "preparing"
    interview_at: dt.datetime | None = None
    timezone: str | None = Field(default=None, max_length=64)
    resume_version_id: uuid.UUID | None = None
    jd_text: str | None = Field(default=None, max_length=60_000)
    integrity_mode: Literal["assisted", "proctored"] = "assisted"


class WorkspaceUpdate(BaseModel):
    company_name: str | None = Field(default=None, max_length=200)
    role_title: str | None = Field(default=None, max_length=200)
    seniority: str | None = Field(default=None, max_length=32)
    role_family: str | None = Field(default=None, max_length=64)
    interview_at: dt.datetime | None = None
    timezone: str | None = Field(default=None, max_length=64)
    notes: str | None = Field(default=None, max_length=20_000)
    integrity_mode: Literal["assisted", "proctored"] | None = None


class StageChange(BaseModel):
    stage: str = Field(max_length=24)


class JobDescriptionCreate(BaseModel):
    raw_text: str = Field(min_length=80, max_length=60_000)
    source_url: str | None = Field(default=None, max_length=1000)


class ResumeBind(BaseModel):
    resume_version_id: uuid.UUID


class WorkspaceSummary(BaseModel):
    id: uuid.UUID
    company_name: str
    role_title: str
    seniority: str | None
    stage: str
    round_index: int
    interview_at: dt.datetime | None
    integrity_mode: str
    readiness_score: float | None
    live_copilot_allowed: bool
    archived: bool


class ContextResponse(BaseModel):
    workspace_id: uuid.UUID
    version: int
    candidate: dict[str, Any]
    opportunity: dict[str, Any]
    match: dict[str, Any]
    inferred_opportunity: bool
    warnings: list[str]


class ImpactPreviewResponse(BaseModel):
    current_version_id: uuid.UUID | None
    new_version_id: uuid.UUID
    match_score_before: float | None
    match_score_after: float
    requirements_changed: int
    gaps_before: int
    gaps_after: int


class ResumeVersionResponse(BaseModel):
    id: uuid.UUID
    resume_id: uuid.UUID
    version: int
    status: str
    source_type: str
    extraction_quality: float | None
    extraction_warnings: list[Any]
    page_count: int | None
    parsed_at: dt.datetime | None


class DiffEntryResponse(BaseModel):
    entity_type: str
    change: str
    label: str
    existing_id: uuid.UUID | None
    user_corrected: bool
    fields_changed: list[str]


class IngestionResponse(BaseModel):
    version: ResumeVersionResponse
    diff: list[DiffEntryResponse]
    created_node_count: int
    experiences_found: int
    skills_found: int
    warnings: list[str]


# ── Shaping ──────────────────────────────────────────────────────────


def _summary(workspace: Any) -> WorkspaceSummary:
    return WorkspaceSummary(
        id=workspace.id,
        company_name=workspace.company_name,
        role_title=workspace.role_title,
        seniority=workspace.seniority,
        stage=workspace.stage,
        round_index=workspace.round_index,
        interview_at=workspace.interview_at,
        integrity_mode=workspace.integrity_mode,
        readiness_score=(
            float(workspace.readiness_score) if workspace.readiness_score is not None else None
        ),
        live_copilot_allowed=workspace.is_live_copilot_allowed,
        archived=workspace.archived_at is not None,
    )


def _context(bundle: Any) -> ContextResponse:
    payload = bundle.to_json()
    return ContextResponse(
        workspace_id=bundle.workspace_id,
        version=bundle.version,
        candidate=payload["candidate"],
        opportunity=payload["opportunity"],
        match=bundle.match.to_json(),
        inferred_opportunity=bundle.inferred_opportunity,
        warnings=bundle.warnings,
    )


def _version(version: Any) -> ResumeVersionResponse:
    return ResumeVersionResponse(
        id=version.id,
        resume_id=version.resume_id,
        version=version.version,
        status=version.status,
        source_type=version.source_type,
        extraction_quality=(
            float(version.extraction_quality) if version.extraction_quality is not None else None
        ),
        extraction_warnings=list(version.extraction_warnings or []),
        page_count=version.page_count,
        parsed_at=version.parsed_at,
    )


def _ingestion(result: Any) -> IngestionResponse:
    return IngestionResponse(
        version=_version(result.version),
        diff=[
            DiffEntryResponse(
                entity_type=d.entity_type,
                change=d.change,
                label=d.label,
                existing_id=d.existing_id,
                user_corrected=d.user_corrected,
                fields_changed=d.fields_changed,
            )
            for d in result.diff
        ],
        created_node_count=len(result.created_node_ids),
        experiences_found=len(result.parsed.experiences),
        skills_found=len(result.parsed.skills),
        warnings=result.parsed.warnings,
    )


# ── Workspaces ───────────────────────────────────────────────────────


@workspaces_router.get("", response_model=list[WorkspaceSummary])
async def list_workspaces(
    principal: CurrentUser, session: SessionDep, include_archived: bool = False
) -> list[WorkspaceSummary]:
    rows = await WorkspaceService(session).list_for_user(
        user_id=principal.user.id, include_archived=include_archived
    )
    return [_summary(w) for w in rows]


@workspaces_router.post("", response_model=WorkspaceSummary, status_code=status.HTTP_201_CREATED)
async def create_workspace(
    payload: WorkspaceCreate, principal: CurrentUser, session: SessionDep
) -> WorkspaceSummary:
    workspace, _ = await WorkspaceService(session).create(
        user_id=principal.user.id,
        company_name=payload.company_name,
        role_title=payload.role_title,
        seniority=payload.seniority,
        stage=payload.stage,
        interview_at=payload.interview_at,
        timezone=payload.timezone,
        resume_version_id=payload.resume_version_id,
        jd_text=payload.jd_text,
        integrity_mode=payload.integrity_mode,
    )
    return _summary(workspace)


@workspaces_router.get("/{workspace_id}", response_model=WorkspaceSummary)
async def get_workspace(
    workspace_id: uuid.UUID, principal: CurrentUser, session: SessionDep
) -> WorkspaceSummary:
    workspace = await WorkspaceService(session).get(
        user_id=principal.user.id, workspace_id=workspace_id
    )
    return _summary(workspace)


@workspaces_router.patch("/{workspace_id}", response_model=ContextResponse)
async def update_workspace(
    workspace_id: uuid.UUID,
    payload: WorkspaceUpdate,
    principal: CurrentUser,
    session: SessionDep,
) -> ContextResponse:
    bundle = await WorkspaceService(session).update(
        user_id=principal.user.id,
        workspace_id=workspace_id,
        changes=payload.model_dump(exclude_unset=True),
    )
    return _context(bundle)


@workspaces_router.get("/{workspace_id}/context", response_model=ContextResponse)
async def get_context(
    workspace_id: uuid.UUID, principal: CurrentUser, session: SessionDep
) -> ContextResponse:
    """The ContextBundle every consumer reads (FR-WS-003)."""
    bundle = await WorkspaceService(session).context(
        user_id=principal.user.id, workspace_id=workspace_id
    )
    return _context(bundle)


@workspaces_router.post("/{workspace_id}/stage", response_model=ContextResponse)
async def advance_stage(
    workspace_id: uuid.UUID, payload: StageChange, principal: CurrentUser, session: SessionDep
) -> ContextResponse:
    bundle = await WorkspaceService(session).advance_stage(
        user_id=principal.user.id, workspace_id=workspace_id, stage=payload.stage
    )
    return _context(bundle)


@workspaces_router.post("/{workspace_id}/job-description", response_model=ContextResponse)
async def attach_job_description(
    workspace_id: uuid.UUID,
    payload: JobDescriptionCreate,
    principal: VerifiedUser,
    session: SessionDep,
) -> ContextResponse:
    service = WorkspaceService(session)
    workspace = await service.get(user_id=principal.user.id, workspace_id=workspace_id)
    await service.attach_job_description(
        user_id=principal.user.id,
        workspace=workspace,
        raw_text=payload.raw_text,
        source_url=payload.source_url,
    )
    return _context(await service.context(user_id=principal.user.id, workspace_id=workspace_id))


@workspaces_router.post("/{workspace_id}/resume/preview", response_model=ImpactPreviewResponse)
async def preview_resume_rebind(
    workspace_id: uuid.UUID, payload: ResumeBind, principal: CurrentUser, session: SessionDep
) -> ImpactPreviewResponse:
    """FR-WS-002: show the consequence before changing the binding."""
    preview = await WorkspaceService(session).preview_resume_rebind(
        user_id=principal.user.id,
        workspace_id=workspace_id,
        resume_version_id=payload.resume_version_id,
    )
    return ImpactPreviewResponse(**preview.__dict__)


@workspaces_router.post("/{workspace_id}/resume", response_model=ContextResponse)
async def bind_resume(
    workspace_id: uuid.UUID, payload: ResumeBind, principal: CurrentUser, session: SessionDep
) -> ContextResponse:
    bundle = await WorkspaceService(session).bind_resume(
        user_id=principal.user.id,
        workspace_id=workspace_id,
        resume_version_id=payload.resume_version_id,
    )
    return _context(bundle)


@workspaces_router.post("/{workspace_id}/archive", status_code=status.HTTP_204_NO_CONTENT)
async def archive_workspace(
    workspace_id: uuid.UUID, principal: CurrentUser, session: SessionDep
) -> Response:
    await WorkspaceService(session).archive(user_id=principal.user.id, workspace_id=workspace_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@workspaces_router.delete("/{workspace_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workspace(
    workspace_id: uuid.UUID, principal: CurrentUser, session: SessionDep
) -> Response:
    await WorkspaceService(session).delete(user_id=principal.user.id, workspace_id=workspace_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── Resumes ──────────────────────────────────────────────────────────

MAX_UPLOAD_BYTES = 10 * 1024 * 1024


class ResumePaste(BaseModel):
    raw_text: str = Field(min_length=100, max_length=200_000)
    label: str | None = Field(default=None, max_length=160)
    resume_id: uuid.UUID | None = None


@resumes_router.post(
    "/upload", response_model=IngestionResponse, status_code=status.HTTP_201_CREATED
)
async def upload_resume(
    principal: VerifiedUser,
    session: SessionDep,
    file: Annotated[UploadFile, File()],
    label: Annotated[str | None, Form()] = None,
) -> IngestionResponse:
    await limiter.enforce(RateLimitScope.UPLOAD, str(principal.user.id))

    # Read with a hard ceiling so an oversized body cannot be buffered whole
    # before the size check runs.
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise AppError(
            ErrorCode.FILE_TOO_LARGE,
            f"Files must be under {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
        )

    result = await ResumeIngestionService(session).ingest_upload(
        user_id=principal.user.id,
        data=data,
        filename=file.filename,
        content_type=file.content_type,
        label=label,
    )
    return _ingestion(result)


@resumes_router.post(
    "/paste", response_model=IngestionResponse, status_code=status.HTTP_201_CREATED
)
async def paste_resume(
    payload: ResumePaste, principal: VerifiedUser, session: SessionDep
) -> IngestionResponse:
    """The fallback path whenever a file cannot be read (PRD §34.2)."""
    await limiter.enforce(RateLimitScope.UPLOAD, str(principal.user.id))
    result = await ResumeIngestionService(session).ingest_text(
        user_id=principal.user.id,
        raw_text=payload.raw_text,
        label=payload.label,
        resume_id=payload.resume_id,
    )
    return _ingestion(result)


@resumes_router.get("/versions", response_model=list[ResumeVersionResponse])
async def list_versions(principal: CurrentUser, session: SessionDep) -> list[ResumeVersionResponse]:
    from sqlalchemy import select

    from verity.modules.documents.models import ResumeVersion

    rows = (
        await session.execute(
            select(ResumeVersion)
            .where(
                ResumeVersion.user_id == principal.user.id,
                ResumeVersion.deleted_at.is_(None),
            )
            .order_by(ResumeVersion.created_at.desc())
            .limit(50)
        )
    ).scalars()
    return [_version(v) for v in rows]
