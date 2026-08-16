"""Preparation plans and tasks (PRD §10.8, §24.5).

A plan is round-scoped; context stays workspace-scoped (FR-WS-004). Tasks are
actionable objects carrying a launch target, never prose — a task the user
cannot start from is advice, not a plan (FR-PREP-003).

``score_breakdown`` is stored alongside ``priority_score`` because the PRD
requires every task to explain why it is ranked where it is (FR-PREP-002). An
opaque ordering would train users to ignore it.
"""

from __future__ import annotations

import datetime as dt
import uuid
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from verity.platform.db.base import Base, Timestamped, UserOwned, UUIDPrimaryKey


class TaskPriority(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    OPTIONAL = "optional"


class TaskStatus(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    DISMISSED = "dismissed"


class TaskSource(StrEnum):
    PLAN = "plan"
    REPORT = "report"
    USER = "user"


class PlanSection(StrEnum):
    """PRD §10.8 sections."""

    ROLE_KNOWLEDGE = "role_knowledge"
    COMPANY = "company"
    BEHAVIORAL = "behavioral"
    TECHNICAL = "technical"
    RESUME_DEEP_DIVE = "resume_deep_dive"
    PROJECT_DEEP_DIVE = "project_deep_dive"
    SYSTEM_DESIGN = "system_design"
    CODING = "coding"
    REVERSE_QUESTIONS = "reverse_questions"
    RISK_AREAS = "risk_areas"


class PreparationPlan(Base, UUIDPrimaryKey, Timestamped, UserOwned):
    __tablename__ = "preparation_plans"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    round_index: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    stage: Mapped[str] = mapped_column(String(24), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))

    generated_by: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Readiness and its drivers, so the score can always be explained.
    summary: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    #: Bundle version the plan was computed from, so a stale plan is detectable.
    context_version: Mapped[int | None] = mapped_column(Integer, default=None)

    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "round_index", "version", name="uq_preparation_plans_round_version"
        ),
        CheckConstraint("round_index > 0", name="preparation_plans_round_positive"),
    )


class PreparationTask(Base, UUIDPrimaryKey, Timestamped, UserOwned):
    __tablename__ = "preparation_tasks"

    plan_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("preparation_plans.id", ondelete="CASCADE"), nullable=False, index=True
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )

    section: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, default=None)

    priority: Mapped[str] = mapped_column(String(16), nullable=False)
    priority_score: Mapped[float] = mapped_column(Numeric(6, 4), nullable=False)
    #: The four weighted factors and their contributions (FR-PREP-002).
    score_breakdown: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    #: {type, params} — every task is launchable (FR-PREP-003).
    action: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    estimated_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("15")
    )
    scheduled_for: Mapped[dt.date | None] = mapped_column(Date, default=None)

    source: Mapped[str] = mapped_column(String(16), nullable=False, server_default=TaskSource.PLAN)
    #: Stable identity across regenerations, so completing a task survives a
    #: plan rebuild (FR-PREP-001).
    dedupe_key: Mapped[str] = mapped_column(String(200), nullable=False)

    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=TaskStatus.OPEN)
    completed_at: Mapped[dt.datetime | None] = mapped_column(default=None)

    __table_args__ = (
        CheckConstraint(
            "priority IN ('critical','high','medium','optional')",
            name="preparation_tasks_priority_valid",
        ),
        CheckConstraint(
            "status IN ('open','in_progress','done','dismissed')",
            name="preparation_tasks_status_valid",
        ),
        CheckConstraint(
            "priority_score >= 0 AND priority_score <= 1",
            name="preparation_tasks_score_range",
        ),
        CheckConstraint("estimated_minutes > 0", name="preparation_tasks_minutes_positive"),
        UniqueConstraint("plan_id", "dedupe_key", name="uq_preparation_tasks_plan_dedupe"),
        Index(
            "ix_preparation_tasks_workspace_ranked",
            "workspace_id",
            "status",
            "priority_score",
        ),
    )

    @property
    def is_open(self) -> bool:
        return self.status in (TaskStatus.OPEN, TaskStatus.IN_PROGRESS)
