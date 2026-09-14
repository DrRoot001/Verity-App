"""Admin endpoints (PRD §18).

Mounted under a distinct prefix with its own dependency, so a customer token can
never reach an admin route by accident. Content access is gated on a stated
reason and leaves a record the user can read back (FR-ADMIN-002).
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update

from verity.ai.prompts import PromptStatus, registry
from verity.ai.providers.base import TaskClass
from verity.modules.admin.models import (
    REASON_REQUIRED,
    AuditLog,
    FeatureFlag,
    PlatformSetting,
    PromptVersion,
    StaffMember,
    StaffRole,
)
from verity.modules.admin.runtime import (
    install_prompt,
    publish_flag,
    upsert_default_flags,
)
from verity.modules.candidate_graph.models import CandidateStory
from verity.modules.identity.dependencies import CurrentUser, SessionDep
from verity.modules.identity.models import AuthSession, User, UserStatus
from verity.modules.sessions.live_models import AiSuggestion, LiveSession
from verity.modules.sessions.models import MockSession, SessionFeedback
from verity.modules.workspace.models import Workspace
from verity.platform.errors import AppError, ErrorCode
from verity.platform.logging import get_logger
from verity.platform.runtime_config import ai_settings, publish_ai_settings

log = get_logger("admin.router")

admin_router = APIRouter(prefix="/v1/admin", tags=["admin"])


class Principal(BaseModel):
    user_id: uuid.UUID
    email: str
    roles: list[str]
    permissions: list[str]


async def staff_principal(principal: CurrentUser, session: SessionDep) -> Principal:
    """Resolve staff grants, or refuse."""
    grants = list(
        (
            await session.execute(
                select(StaffMember).where(
                    StaffMember.user_id == principal.user.id,
                    StaffMember.revoked_at.is_(None),
                )
            )
        ).scalars()
    )
    if not grants:
        # 404 rather than 403: the admin surface does not confirm its own
        # existence to a non-staff caller.
        raise AppError.not_found("Resource")

    permissions: set[str] = set()
    for grant in grants:
        permissions |= grant.permissions

    return Principal(
        user_id=principal.user.id,
        email=principal.user.email,
        roles=[g.role for g in grants],
        permissions=sorted(permissions),
    )


StaffDep = Annotated[Principal, Depends(staff_principal)]


def require(principal: Principal, permission: str) -> None:
    if permission not in principal.permissions:
        raise AppError(
            ErrorCode.FORBIDDEN,
            "Your role does not include this action.",
            detail={"required": permission, "roles": principal.roles},
        )


async def record(
    session: Any,
    principal: Principal,
    *,
    action: str,
    resource_type: str,
    resource_id: str | None = None,
    subject_user_id: uuid.UUID | None = None,
    reason: str | None = None,
    case_id: str | None = None,
) -> None:
    if action in REASON_REQUIRED and not (reason and case_id):
        raise AppError.validation(
            "This action requires a reason and a support case id.",
            fields={"reason": "required", "case_id": "required"},
        )

    session.add(
        AuditLog(
            actor_type="admin",
            actor_id=principal.user_id,
            subject_user_id=subject_user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            reason=reason,
            case_id=case_id,
        )
    )
    log.info("admin_action", action=action, actor=str(principal.user_id))


# ── Metrics ──────────────────────────────────────────────────────────


class MetricsResponse(BaseModel):
    users_total: int
    users_active_7d: int
    workspaces_total: int
    mock_sessions_total: int
    live_sessions_total: int
    reports_total: int
    stories_approved: int
    #: Activation per PRD §31.2 — the metric that only moves when the loop works.
    activated_users: int


@admin_router.get("/metrics", response_model=MetricsResponse)
async def metrics(principal: StaffDep, session: SessionDep) -> MetricsResponse:
    require(principal, "metrics.read")

    async def count(model: Any, *where: Any) -> int:
        stmt = select(func.count()).select_from(model)
        for clause in where:
            stmt = stmt.where(clause)
        return int((await session.execute(stmt)).scalar_one())

    since = dt.datetime.now(dt.UTC) - dt.timedelta(days=7)

    # Activation: a workspace plus at least one session of either kind. Counting
    # only mocks would report zero for a user whose whole journey was live.
    activated = int(
        (
            await session.execute(
                select(func.count(func.distinct(Workspace.user_id))).where(
                    Workspace.deleted_at.is_(None),
                    Workspace.id.in_(
                        select(MockSession.workspace_id).union(select(LiveSession.workspace_id))
                    ),
                )
            )
        ).scalar_one()
    )

    return MetricsResponse(
        users_total=await count(User, User.deleted_at.is_(None)),
        users_active_7d=await count(User, User.deleted_at.is_(None), User.updated_at >= since),
        workspaces_total=await count(Workspace, Workspace.deleted_at.is_(None)),
        mock_sessions_total=await count(MockSession, MockSession.deleted_at.is_(None)),
        live_sessions_total=await count(LiveSession, LiveSession.deleted_at.is_(None)),
        reports_total=await count(SessionFeedback),
        stories_approved=await count(
            CandidateStory,
            CandidateStory.status == "approved",
            CandidateStory.deleted_at.is_(None),
        ),
        activated_users=activated,
    )


# ── Users ────────────────────────────────────────────────────────────


class AdminUserResponse(BaseModel):
    id: uuid.UUID
    email: str
    full_name: str | None
    status: str
    email_verified: bool
    created_at: dt.datetime
    workspaces: int
    mock_sessions: int
    live_sessions: int
    staff_roles: list[str] = []


@admin_router.get("/users", response_model=list[AdminUserResponse])
async def search_users(
    principal: StaffDep,
    session: SessionDep,
    q: str = Query(default="", max_length=200),
    limit: int = Query(default=25, ge=1, le=100),
) -> list[AdminUserResponse]:
    require(principal, "user.read")

    stmt = select(User).where(User.deleted_at.is_(None)).order_by(User.created_at.desc())
    if q:
        stmt = stmt.where(User.email.ilike(f"%{q.lower()}%"))
    users = list((await session.execute(stmt.limit(limit))).scalars())

    return [await _admin_user(session, user) for user in users]


async def _admin_user(session: Any, user: User) -> AdminUserResponse:
    async def owned_count(model: Any, *where: Any) -> int:
        return int(
            (
                await session.execute(
                    select(func.count()).select_from(model).where(model.user_id == user.id, *where)
                )
            ).scalar_one()
        )

    roles = list(
        (
            await session.execute(
                select(StaffMember.role).where(
                    StaffMember.user_id == user.id, StaffMember.revoked_at.is_(None)
                )
            )
        ).scalars()
    )
    return AdminUserResponse(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        status=user.status,
        email_verified=user.is_verified,
        created_at=user.created_at,
        workspaces=await owned_count(Workspace, Workspace.deleted_at.is_(None)),
        mock_sessions=await owned_count(MockSession),
        live_sessions=await owned_count(LiveSession),
        staff_roles=roles,
    )


class SuspendRequest(BaseModel):
    reason: str = Field(min_length=8, max_length=500)
    case_id: str = Field(min_length=1, max_length=64)


@admin_router.post("/users/{user_id}/suspend", response_model=AdminUserResponse)
async def suspend_user(
    user_id: uuid.UUID, payload: SuspendRequest, principal: StaffDep, session: SessionDep
) -> AdminUserResponse:
    require(principal, "user.suspend")

    user = (
        await session.execute(select(User).where(User.id == user_id, User.deleted_at.is_(None)))
    ).scalar_one_or_none()
    if user is None:
        raise AppError.not_found("User")

    user.status = "suspended"
    await record(
        session,
        principal,
        action="user.suspend",
        resource_type="user",
        resource_id=str(user_id),
        subject_user_id=user_id,
        reason=payload.reason,
        case_id=payload.case_id,
    )
    await session.flush()

    await session.execute(
        update(AuthSession)
        .where(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=dt.datetime.now(dt.UTC), revoked_reason="admin_suspension")
    )
    return await _admin_user(session, user)


@admin_router.post("/users/{user_id}/restore", response_model=AdminUserResponse)
async def restore_user(
    user_id: uuid.UUID, payload: SuspendRequest, principal: StaffDep, session: SessionDep
) -> AdminUserResponse:
    require(principal, "user.suspend")
    user = (
        await session.execute(select(User).where(User.id == user_id, User.deleted_at.is_(None)))
    ).scalar_one_or_none()
    if user is None:
        raise AppError.not_found("User")
    if user.status == UserStatus.PENDING_DELETION:
        raise AppError.validation("Cancel the scheduled deletion before restoring this account.")
    user.status = UserStatus.ACTIVE
    user.deletion_requested_at = None
    await record(
        session,
        principal,
        action="user.restore",
        resource_type="user",
        resource_id=str(user_id),
        subject_user_id=user_id,
        reason=payload.reason,
        case_id=payload.case_id,
    )
    await session.flush()
    return await _admin_user(session, user)


# ── Staff and RBAC ──────────────────────────────────────────────────


class StaffGrantResponse(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    email: str
    role: str
    created_at: dt.datetime


class StaffGrantRequest(BaseModel):
    user_id: uuid.UUID
    role: StaffRole


@admin_router.get("/staff", response_model=list[StaffGrantResponse])
async def list_staff(principal: StaffDep, session: SessionDep) -> list[StaffGrantResponse]:
    require(principal, "staff.read")
    rows = list(
        (
            await session.execute(
                select(StaffMember, User.email)
                .join(User, User.id == StaffMember.user_id)
                .where(StaffMember.revoked_at.is_(None))
                .order_by(User.email, StaffMember.role)
            )
        ).all()
    )
    return [
        StaffGrantResponse(
            id=grant.id,
            user_id=grant.user_id,
            email=email,
            role=grant.role,
            created_at=grant.created_at,
        )
        for grant, email in rows
    ]


@admin_router.post("/staff", response_model=StaffGrantResponse, status_code=201)
async def grant_staff(
    payload: StaffGrantRequest, principal: StaffDep, session: SessionDep
) -> StaffGrantResponse:
    require(principal, "staff.write")
    user = await session.get(User, payload.user_id)
    if user is None or user.deleted_at is not None:
        raise AppError.not_found("User")
    grant = (
        await session.execute(
            select(StaffMember).where(
                StaffMember.user_id == payload.user_id, StaffMember.role == payload.role
            )
        )
    ).scalar_one_or_none()
    if grant is None:
        grant = StaffMember(
            user_id=payload.user_id, role=payload.role, granted_by=principal.user_id
        )
        session.add(grant)
    else:
        grant.revoked_at = None
        grant.granted_by = principal.user_id
    await record(
        session,
        principal,
        action="staff.grant",
        resource_type="staff_grant",
        resource_id=str(grant.id),
        subject_user_id=user.id,
        reason=f"Granted {payload.role}",
    )
    await session.flush()
    return StaffGrantResponse(
        id=grant.id,
        user_id=user.id,
        email=user.email,
        role=grant.role,
        created_at=grant.created_at,
    )


@admin_router.delete("/staff/{grant_id}", status_code=204)
async def revoke_staff(grant_id: uuid.UUID, principal: StaffDep, session: SessionDep) -> None:
    require(principal, "staff.write")
    grant = await session.get(StaffMember, grant_id)
    if grant is None or grant.revoked_at is not None:
        raise AppError.not_found("Staff grant")
    if grant.user_id == principal.user_id and grant.role == StaffRole.ADMIN:
        raise AppError.validation("You cannot revoke your own admin grant.")
    if grant.role == StaffRole.ADMIN:
        active_admins = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(StaffMember)
                    .where(StaffMember.role == StaffRole.ADMIN, StaffMember.revoked_at.is_(None))
                )
            ).scalar_one()
        )
        if active_admins <= 1:
            raise AppError.validation("The final active admin grant cannot be revoked.")
    grant.revoked_at = dt.datetime.now(dt.UTC)
    await record(
        session,
        principal,
        action="staff.revoke",
        resource_type="staff_grant",
        resource_id=str(grant.id),
        subject_user_id=grant.user_id,
        reason=f"Revoked {grant.role}",
    )


# ── Session inventory ───────────────────────────────────────────────


class AdminSessionResponse(BaseModel):
    id: uuid.UUID
    kind: str
    user_id: uuid.UUID
    user_email: str
    workspace_id: uuid.UUID
    status: str
    mode: str
    created_at: dt.datetime
    duration_seconds: int | None


@admin_router.get("/sessions", response_model=list[AdminSessionResponse])
async def list_admin_sessions(
    principal: StaffDep,
    session: SessionDep,
    kind: str = Query(default="all", pattern="^(all|mock|live)$"),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[AdminSessionResponse]:
    require(principal, "session.read_metadata")
    result: list[AdminSessionResponse] = []
    if kind in ("all", "live"):
        live_rows = (
            await session.execute(
                select(LiveSession, User.email)
                .join(User, User.id == LiveSession.user_id)
                .order_by(LiveSession.created_at.desc())
                .limit(limit)
            )
        ).all()
        result.extend(
            AdminSessionResponse(
                id=row.id,
                kind="live",
                user_id=row.user_id,
                user_email=email,
                workspace_id=row.workspace_id,
                status=row.status,
                mode=row.interview_type,
                created_at=row.created_at,
                duration_seconds=row.duration_seconds,
            )
            for row, email in live_rows
        )
    if kind in ("all", "mock"):
        mock_rows = (
            await session.execute(
                select(MockSession, User.email)
                .join(User, User.id == MockSession.user_id)
                .order_by(MockSession.created_at.desc())
                .limit(limit)
            )
        ).all()
        result.extend(
            AdminSessionResponse(
                id=row.id,
                kind="mock",
                user_id=row.user_id,
                user_email=email,
                workspace_id=row.workspace_id,
                status=row.status,
                mode=row.mode,
                created_at=row.created_at,
                duration_seconds=row.duration_seconds,
            )
            for row, email in mock_rows
        )
    return sorted(result, key=lambda item: item.created_at, reverse=True)[:limit]


# ── AI settings, flags, and prompts ─────────────────────────────────


class AISettingsResponse(BaseModel):
    provider: str
    fast_model: str
    realtime_model: str
    reasoning_model: str
    daily_budget_usd: float
    budget_soft_threshold: float
    session_max_generations: int
    session_max_generations_per_minute: int
    session_max_duration_minutes: int
    groq_key_configured: bool
    anthropic_key_configured: bool


class AISettingsUpdate(BaseModel):
    provider: str = Field(pattern="^(stub|groq|anthropic)$")
    fast_model: str = Field(min_length=2, max_length=120)
    realtime_model: str = Field(min_length=2, max_length=120)
    reasoning_model: str = Field(min_length=2, max_length=120)
    daily_budget_usd: float = Field(gt=0, le=100_000)
    budget_soft_threshold: float = Field(gt=0, le=1)
    session_max_generations: int = Field(ge=1, le=500)
    session_max_generations_per_minute: int = Field(ge=1, le=60)
    session_max_duration_minutes: int = Field(ge=5, le=480)


def _ai_response(value: dict[str, Any]) -> AISettingsResponse:
    from verity.platform.config import settings

    return AISettingsResponse(
        **value,
        groq_key_configured=bool(settings.groq_api_key.get_secret_value()),
        anthropic_key_configured=bool(settings.anthropic_api_key.get_secret_value()),
    )


@admin_router.get("/settings/ai", response_model=AISettingsResponse)
async def get_ai_settings(principal: StaffDep) -> AISettingsResponse:
    require(principal, "ai.read")
    return _ai_response(await ai_settings())


@admin_router.put("/settings/ai", response_model=AISettingsResponse)
async def update_ai_settings(
    payload: AISettingsUpdate, principal: StaffDep, session: SessionDep
) -> AISettingsResponse:
    require(principal, "ai.write")
    value = payload.model_dump()
    row = (
        await session.execute(
            select(PlatformSetting).where(
                PlatformSetting.namespace == "ai", PlatformSetting.key == "gateway"
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = PlatformSetting(
            namespace="ai",
            key="gateway",
            value=value,
            description="Model routing, budgets, and per-session generation limits.",
            updated_by=principal.user_id,
        )
        session.add(row)
    else:
        row.value = value
        row.updated_by = principal.user_id
    await publish_ai_settings(value)
    await record(
        session,
        principal,
        action="ai.settings.update",
        resource_type="platform_setting",
        resource_id="ai.gateway",
        reason="Updated AI gateway configuration",
    )
    await session.flush()
    return _ai_response(value)


class FeatureFlagResponse(BaseModel):
    id: uuid.UUID
    key: str
    description: str
    enabled: bool
    rollout_percentage: int
    updated_at: dt.datetime


class FeatureFlagUpdate(BaseModel):
    description: str = Field(max_length=500)
    enabled: bool
    rollout_percentage: int = Field(ge=0, le=100)


def _flag_response(flag: FeatureFlag) -> FeatureFlagResponse:
    return FeatureFlagResponse(
        id=flag.id,
        key=flag.key,
        description=flag.description,
        enabled=flag.enabled,
        rollout_percentage=flag.rollout_percentage,
        updated_at=flag.updated_at,
    )


@admin_router.get("/flags", response_model=list[FeatureFlagResponse])
async def list_flags(principal: StaffDep, session: SessionDep) -> list[FeatureFlagResponse]:
    require(principal, "flags.read")
    await upsert_default_flags(session)
    await session.flush()
    rows = list((await session.execute(select(FeatureFlag).order_by(FeatureFlag.key))).scalars())
    for row in rows:
        await publish_flag(row)
    return [_flag_response(row) for row in rows]


@admin_router.put("/flags/{key}", response_model=FeatureFlagResponse)
async def update_flag(
    key: str, payload: FeatureFlagUpdate, principal: StaffDep, session: SessionDep
) -> FeatureFlagResponse:
    require(principal, "flags.write")
    flag = (
        await session.execute(select(FeatureFlag).where(FeatureFlag.key == key))
    ).scalar_one_or_none()
    if flag is None:
        raise AppError.not_found("Feature flag")
    flag.description = payload.description
    flag.enabled = payload.enabled
    flag.rollout_percentage = payload.rollout_percentage
    flag.updated_by = principal.user_id
    await publish_flag(flag)
    await record(
        session,
        principal,
        action="feature_flag.update",
        resource_type="feature_flag",
        resource_id=key,
        reason=f"Set enabled={payload.enabled}, rollout={payload.rollout_percentage}%",
    )
    await session.flush()
    await session.refresh(flag)
    return _flag_response(flag)


class PromptResponse(BaseModel):
    id: uuid.UUID | None
    prompt_id: str
    version: int
    task_class: str
    status: str
    system_template: str
    user_template: str
    variables: list[str]
    output_schema: dict[str, Any] | None
    notes: str
    eval_score: float | None


class PromptCreate(BaseModel):
    prompt_id: str = Field(pattern=r"^[a-z0-9_.-]+$", min_length=3, max_length=120)
    task_class: TaskClass
    system_template: str = Field(min_length=20, max_length=20_000)
    user_template: str = Field(min_length=10, max_length=30_000)
    variables: list[str] = Field(max_length=50)
    output_schema: dict[str, Any] | None = None
    notes: str = Field(default="", max_length=2_000)


def _prompt_response(row: PromptVersion) -> PromptResponse:
    return PromptResponse(
        id=row.id,
        prompt_id=row.prompt_id,
        version=row.version,
        task_class=row.task_class,
        status=row.status,
        system_template=row.system_template,
        user_template=row.user_template,
        variables=[str(item) for item in row.variables],
        output_schema=row.output_schema,
        notes=row.notes,
        eval_score=row.eval_score,
    )


@admin_router.get("/prompts", response_model=list[PromptResponse])
async def list_prompts(principal: StaffDep, session: SessionDep) -> list[PromptResponse]:
    require(principal, "prompts.read")
    rows = list(
        (
            await session.execute(
                select(PromptVersion).order_by(
                    PromptVersion.prompt_id, PromptVersion.version.desc()
                )
            )
        ).scalars()
    )
    stored = {(row.prompt_id, row.version) for row in rows}
    result = [_prompt_response(row) for row in rows]
    for prompt in registry.all():
        if (prompt.id, prompt.version) not in stored:
            result.append(
                PromptResponse(
                    id=None,
                    prompt_id=prompt.id,
                    version=prompt.version,
                    task_class=prompt.task_class,
                    status=prompt.status,
                    system_template=prompt.system,
                    user_template=prompt.user_template,
                    variables=list(prompt.variables),
                    output_schema=prompt.output_schema,
                    notes=prompt.notes,
                    eval_score=prompt.eval_score,
                )
            )
    return sorted(result, key=lambda item: (item.prompt_id, -item.version))


@admin_router.post("/prompts", response_model=PromptResponse, status_code=201)
async def create_prompt_version(
    payload: PromptCreate, principal: StaffDep, session: SessionDep
) -> PromptResponse:
    require(principal, "prompts.write")
    unknown = {
        field_name
        for _, field_name, _, _ in __import__("string").Formatter().parse(payload.user_template)
        if field_name and field_name not in payload.variables
    }
    if unknown:
        raise AppError.validation(
            "The template contains variables that were not declared.",
            fields=dict.fromkeys(sorted(unknown), "undeclared"),
        )
    latest = (
        await session.execute(
            select(func.coalesce(func.max(PromptVersion.version), 0)).where(
                PromptVersion.prompt_id == payload.prompt_id
            )
        )
    ).scalar_one()
    code_versions = [p.version for p in registry.all() if p.id == payload.prompt_id]
    version = max([int(latest), *code_versions], default=0) + 1
    row = PromptVersion(
        prompt_id=payload.prompt_id,
        version=version,
        task_class=payload.task_class,
        status=PromptStatus.DEVELOPMENT,
        system_template=payload.system_template,
        user_template=payload.user_template,
        variables=payload.variables,
        output_schema=payload.output_schema,
        notes=payload.notes,
        created_by=principal.user_id,
    )
    session.add(row)
    await session.flush()
    install_prompt(row)
    await record(
        session,
        principal,
        action="prompt.create_version",
        resource_type="prompt",
        resource_id=f"{row.prompt_id}@{row.version}",
        reason="Created development prompt version",
    )
    return _prompt_response(row)


@admin_router.post("/prompts/{prompt_id}/{version}/activate", response_model=PromptResponse)
async def activate_prompt(
    prompt_id: str,
    version: int,
    principal: StaffDep,
    session: SessionDep,
) -> PromptResponse:
    require(principal, "prompts.write")
    row = (
        await session.execute(
            select(PromptVersion).where(
                PromptVersion.prompt_id == prompt_id, PromptVersion.version == version
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise AppError.not_found("Prompt version")
    await session.execute(
        update(PromptVersion)
        .where(
            PromptVersion.prompt_id == prompt_id,
            PromptVersion.status == PromptStatus.PRODUCTION,
            PromptVersion.id != row.id,
        )
        .values(status=PromptStatus.ARCHIVED)
    )
    row.status = PromptStatus.PRODUCTION
    row.activated_at = dt.datetime.now(dt.UTC)
    install_prompt(row)
    registry.set_production(prompt_id, version)
    await record(
        session,
        principal,
        action="prompt.activate",
        resource_type="prompt",
        resource_id=f"{prompt_id}@{version}",
        reason="Activated prompt version",
    )
    await session.flush()
    return _prompt_response(row)


# ── Session diagnostics ──────────────────────────────────────────────


class SessionDiagnostics(BaseModel):
    session_id: uuid.UUID
    status: str
    generations: int
    metered_seconds: int
    last_event_seq: int
    #: Per-stage latencies so an on-call engineer can attribute a slow session.
    latencies: list[dict[str, Any]]
    grounding: dict[str, int]


@admin_router.get("/sessions/{session_id}/diagnostics", response_model=SessionDiagnostics)
async def session_diagnostics(
    session_id: uuid.UUID, principal: StaffDep, session: SessionDep
) -> SessionDiagnostics:
    """Metadata only — no transcript content, so no reason is required."""
    require(principal, "session.read_metadata")

    live = (
        await session.execute(select(LiveSession).where(LiveSession.id == session_id))
    ).scalar_one_or_none()
    if live is None:
        raise AppError.not_found("Session")

    suggestions = list(
        (
            await session.execute(select(AiSuggestion).where(AiSuggestion.session_id == session_id))
        ).scalars()
    )

    checked = sum(int(s.grounding.get("checked", 0)) for s in suggestions)
    downgraded = sum(int(s.grounding.get("downgraded", 0)) for s in suggestions)

    return SessionDiagnostics(
        session_id=live.id,
        status=live.status,
        generations=live.generation_count,
        metered_seconds=live.metered_seconds,
        last_event_seq=live.last_event_seq,
        latencies=[
            {"suggestion_id": str(s.id), "revision": s.revision, **s.latency_ms}
            for s in suggestions
        ],
        grounding={"checked": checked, "downgraded": downgraded},
    )


# ── Audit ────────────────────────────────────────────────────────────


class AuditEntry(BaseModel):
    id: uuid.UUID
    actor_id: uuid.UUID | None
    subject_user_id: uuid.UUID | None
    action: str
    resource_type: str
    resource_id: str | None
    reason: str | None
    case_id: str | None
    created_at: dt.datetime


@admin_router.get("/audit", response_model=list[AuditEntry])
async def read_audit(
    principal: StaffDep,
    session: SessionDep,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[AuditEntry]:
    require(principal, "audit.read")

    rows = list(
        (
            await session.execute(
                select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
            )
        ).scalars()
    )
    return [
        AuditEntry(
            id=row.id,
            actor_id=row.actor_id,
            subject_user_id=row.subject_user_id,
            action=row.action,
            resource_type=row.resource_type,
            resource_id=row.resource_id,
            reason=row.reason,
            case_id=row.case_id,
            created_at=row.created_at,
        )
        for row in rows
    ]


@admin_router.get("/me", response_model=Principal)
async def whoami(principal: StaffDep) -> Principal:
    return principal


ALL_ROLES = [r.value for r in StaffRole]
