"""Interview Workspace (PRD §11, §24.4).

A Workspace is the canonical, persistent context object for one target
opportunity, and the mandatory scope for every interview-intelligence
operation. Preparation, mock interviews, the live copilot and document
generation all read from it — none of them reconstruct opportunity context on
their own (FR-WS-003).

It binds to a *specific* ``resume_version_id`` rather than "the latest resume"
(FR-WS-002), so a session's context is reproducible after the user uploads a
new CV, and changing the binding is an explicit action with a visible impact.
"""

from __future__ import annotations

import datetime as dt
import uuid
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from verity.platform.db.base import (
    Base,
    SoftDeletable,
    Timestamped,
    UserOwned,
    UUIDPrimaryKey,
)


class InterviewStage(StrEnum):
    """Stage transitions create rounds; context stays workspace-scoped."""

    PREPARING = "preparing"
    RECRUITER_SCREEN = "recruiter_screen"
    HIRING_MANAGER = "hiring_manager"
    TECHNICAL = "technical"
    ONSITE = "onsite"
    FINAL = "final"
    OFFER = "offer"
    REJECTED = "rejected"


class IntegrityMode(StrEnum):
    """PRD FR-PRIV-011.

    ``proctored`` disables live copilot, screen capture and copy actions. The
    product takes a position here rather than leaving it to the user's judgment
    in the moment.
    """

    ASSISTED = "assisted"
    PROCTORED = "proctored"


STAGE_VALUES = (
    "'preparing','recruiter_screen','hiring_manager','technical','onsite',"
    "'final','offer','rejected'"
)


class Workspace(Base, UUIDPrimaryKey, Timestamped, SoftDeletable, UserOwned):
    __tablename__ = "workspaces"

    company_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("companies.id", ondelete="SET NULL"), default=None, index=True
    )
    #: Denormalized deliberately (PRD §24.9): a workspace may target an
    #: undisclosed or renamed company that has no ``companies`` row.
    company_name: Mapped[str] = mapped_column(String(200), nullable=False)

    role_title: Mapped[str] = mapped_column(String(200), nullable=False)
    role_family: Mapped[str | None] = mapped_column(String(64), default=None)
    seniority: Mapped[str | None] = mapped_column(String(32), default=None)

    job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), default=None
    )
    resume_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("resume_versions.id", ondelete="SET NULL"), default=None
    )

    stage: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default=InterviewStage.PREPARING
    )
    round_index: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))

    interview_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    timezone: Mapped[str | None] = mapped_column(String(64), default=None)

    integrity_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=IntegrityMode.ASSISTED
    )

    settings: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    notes: Mapped[str | None] = mapped_column(Text, default=None)

    readiness_score: Mapped[float | None] = mapped_column(Numeric(5, 2), default=None)
    readiness_computed_at: Mapped[dt.datetime | None] = mapped_column(default=None)

    archived_at: Mapped[dt.datetime | None] = mapped_column(default=None)

    __table_args__ = (
        CheckConstraint(f"stage IN ({STAGE_VALUES})", name="workspaces_stage_valid"),
        CheckConstraint(
            "integrity_mode IN ('assisted','proctored')", name="workspaces_integrity_valid"
        ),
        CheckConstraint("round_index > 0", name="workspaces_round_positive"),
        CheckConstraint(
            "readiness_score IS NULL OR (readiness_score >= 0 AND readiness_score <= 100)",
            name="workspaces_readiness_range",
        ),
        Index(
            "ix_workspaces_user_upcoming",
            "user_id",
            "interview_at",
            postgresql_where=text("deleted_at IS NULL AND archived_at IS NULL"),
        ),
        Index(
            "ix_workspaces_user_recent",
            "user_id",
            "updated_at",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    @property
    def is_live_copilot_allowed(self) -> bool:
        """FR-PRIV-011: proctored sessions get no live assistance."""
        return self.integrity_mode == IntegrityMode.ASSISTED


class WorkspaceContext(Base, UUIDPrimaryKey, Timestamped, UserOwned):
    """Materialized ContextBundle (PRD FR-WS-003).

    Consumers read this rather than re-deriving context from the graph and JD
    on every call. ``version`` increments on every rebuild so a session can
    record exactly which context produced its guidance, and ``stale`` marks a
    bundle whose sources changed but whose rebuild has not landed yet — which
    is what lets a surface show a `processing` state instead of silently
    serving outdated context.
    """

    __tablename__ = "workspace_context"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, unique=True
    )

    bundle: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    match: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    stale: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    computed_at: Mapped[dt.datetime | None] = mapped_column(default=None)

    #: Which sources the bundle was built from, so a rebuild can be skipped when
    #: nothing relevant changed.
    source_fingerprint: Mapped[str | None] = mapped_column(String(64), default=None)


class StoryWorkspaceOverride(Base, Timestamped):
    """Per-workspace story tailoring (PRD §9.1).

    A story is global. A workspace may rank it differently or add a tailored
    opening, but must never mutate the source — that is what keeps the Story
    Bank a single canonical record rather than N drifting copies.
    """

    __tablename__ = "story_workspace_overrides"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    story_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("candidate_stories.id", ondelete="CASCADE"), primary_key=True
    )
    relevance: Mapped[float | None] = mapped_column(Numeric(4, 3), default=None)
    tailored_opening: Mapped[str | None] = mapped_column(Text, default=None)
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
