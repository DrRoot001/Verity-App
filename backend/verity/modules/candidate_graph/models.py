"""Candidate Intelligence models (PRD §12, §24.3).

This is the canonical, cross-opportunity record of who the candidate is. Every
downstream consumer — preparation, mock, copilot, documents — reads from here
via retrieval; none of them hold their own copy (PRD §9.4, §66).

Two invariants are enforced structurally rather than by convention:

1. **Provenance on every node.** Where a fact came from, which extraction
   produced it, and whether a human confirmed it (PRD §12.2).
2. **Only ``approved`` nodes are candidate fact.** Grounded Candidate Mode
   (PRD §12.6, FR-GRAPH-001) resolves evidence exclusively from approved rows,
   so an unreviewed extraction can never be asserted as the candidate's history.
"""

from __future__ import annotations

import datetime as dt
import uuid
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from verity.platform.db.base import (
    Base,
    SoftDeletable,
    Timestamped,
    UserOwned,
    UUIDPrimaryKey,
)


class NodeStatus(StrEnum):
    """Review lifecycle. Only APPROVED is usable as candidate fact."""

    DRAFT = "draft"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class ProvenanceSource(StrEnum):
    RESUME = "resume"
    USER_MANUAL = "user_manual"
    JD = "jd"
    SESSION_TRANSCRIPT = "session_transcript"
    USER_NOTE = "user_note"
    INFERENCE = "inference"


class StoryStatus(StrEnum):
    SUGGESTED = "suggested"
    APPROVED = "approved"
    REJECTED = "rejected"
    NEEDS_DETAIL = "needs_detail"


#: Story taxonomy (PRD §12.5). Original to Verity.
STORY_CATEGORIES: tuple[str, ...] = (
    "leadership",
    "conflict",
    "failure",
    "deadline_pressure",
    "ambiguity",
    "ownership",
    "customer_impact",
    "technical_challenge",
    "disagreement",
    "innovation",
    "prioritization",
    "mistake_and_learning",
    "collaboration",
    "high_pressure",
    "measurable_success",
    "scope_negotiation",
    "mentoring",
    "crisis_recovery",
)

_STATUS_VALUES = "'draft','pending_review','approved','rejected','superseded'"
_SOURCE_VALUES = "'resume','user_manual','jd','session_transcript','user_note','inference'"


class Provenanced:
    """Provenance columns shared by every graph node (PRD §12.2).

    ``ai_extracted`` and ``user_corrected`` are kept side by side rather than
    merged so a re-extraction can never silently overwrite a human edit
    (PRD PP6, FR-RES-006). The effective value of a field is
    ``user_corrected[path] ?? ai_extracted[path] ?? column``.
    """

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=NodeStatus.PENDING_REVIEW, index=True
    )
    source: Mapped[str] = mapped_column(String(24), nullable=False)
    source_ref: Mapped[str | None] = mapped_column(String(255), default=None)
    extracted_by: Mapped[str | None] = mapped_column(String(120), default=None)
    confidence: Mapped[float | None] = mapped_column(Numeric(4, 3), default=None)
    confirmed_at: Mapped[dt.datetime | None] = mapped_column(default=None)

    ai_extracted: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    user_corrected: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    @property
    def is_evidence(self) -> bool:
        """FR-GRAPH-001: only approved nodes may back a candidate_fact claim."""
        return self.status == NodeStatus.APPROVED

    def effective(self, field: str) -> Any:
        """User correction wins over extraction, which wins over the column."""
        if field in self.user_corrected:
            return self.user_corrected[field]
        if self.ai_extracted and field in self.ai_extracted:
            return self.ai_extracted[field]
        return getattr(self, field, None)


def _provenance_constraints(table: str) -> tuple[CheckConstraint, ...]:
    return (
        CheckConstraint(f"status IN ({_STATUS_VALUES})", name=f"{table}_status_valid"),
        CheckConstraint(f"source IN ({_SOURCE_VALUES})", name=f"{table}_source_valid"),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name=f"{table}_confidence_range",
        ),
    )


class CandidateProfile(Base, UUIDPrimaryKey, Timestamped, UserOwned):
    """Durable identity layer. Exactly one per user (PRD §66 canonical ownership)."""

    __tablename__ = "candidate_profiles"

    headline: Mapped[str | None] = mapped_column(String(300), default=None)
    summary: Mapped[str | None] = mapped_column(Text, default=None)

    experience_level: Mapped[str | None] = mapped_column(String(24), default=None)
    target_role: Mapped[str | None] = mapped_column(String(200), default=None)
    role_family: Mapped[str | None] = mapped_column(String(64), default=None)
    target_industries: Mapped[list[str]] = mapped_column(
        ARRAY(String(80)), nullable=False, server_default=text("'{}'")
    )
    goal: Mapped[str | None] = mapped_column(String(32), default=None)

    # Contact details are encrypted and redacted before any prompt (PRD FR-PRIV-001).
    contact_enc: Mapped[bytes | None] = mapped_column(default=None)

    # Form preferences only. Never facts (PRD FR-GRAPH-011).
    style_profile: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    __table_args__ = (
        UniqueConstraint("user_id", name="uq_candidate_profiles_user_id"),
        CheckConstraint(
            "experience_level IS NULL OR experience_level IN "
            "('student','entry','mid','senior','staff','manager','director','exec')",
            name="candidate_profiles_level_valid",
        ),
    )


class Experience(Base, UUIDPrimaryKey, Timestamped, SoftDeletable, UserOwned, Provenanced):
    __tablename__ = "experiences"

    company_name: Mapped[str] = mapped_column(String(200), nullable=False)
    company_id: Mapped[uuid.UUID | None] = mapped_column(default=None)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    employment_type: Mapped[str | None] = mapped_column(String(32), default=None)
    location: Mapped[str | None] = mapped_column(String(200), default=None)
    is_remote: Mapped[bool | None] = mapped_column(Boolean, default=None)

    # Month precision: resumes rarely state days, and inventing one is a fabrication.
    start_month: Mapped[dt.date | None] = mapped_column(Date, default=None)
    end_month: Mapped[dt.date | None] = mapped_column(Date, default=None)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    date_confidence: Mapped[float | None] = mapped_column(Numeric(4, 3), default=None)

    description: Mapped[str | None] = mapped_column(Text, default=None)
    bullets: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )
    seniority: Mapped[str | None] = mapped_column(String(32), default=None)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    __table_args__ = (
        *_provenance_constraints("experiences"),
        CheckConstraint(
            "end_month IS NULL OR start_month IS NULL OR end_month >= start_month",
            name="experiences_date_order",
        ),
        CheckConstraint(
            "NOT (is_current AND end_month IS NOT NULL)",
            name="experiences_current_has_no_end",
        ),
        Index(
            "ix_experiences_user_timeline",
            "user_id",
            "start_month",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_experiences_user_status",
            "user_id",
            "status",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )


class Project(Base, UUIDPrimaryKey, Timestamped, SoftDeletable, UserOwned, Provenanced):
    __tablename__ = "projects"

    experience_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("experiences.id", ondelete="SET NULL"), default=None, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    role: Mapped[str | None] = mapped_column(String(160), default=None)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    start_month: Mapped[dt.date | None] = mapped_column(Date, default=None)
    end_month: Mapped[dt.date | None] = mapped_column(Date, default=None)
    url: Mapped[str | None] = mapped_column(String(500), default=None)
    technologies: Mapped[list[str]] = mapped_column(
        ARRAY(String(80)), nullable=False, server_default=text("'{}'")
    )
    impact: Mapped[str | None] = mapped_column(Text, default=None)

    __table_args__ = (
        *_provenance_constraints("projects"),
        Index("ix_projects_user", "user_id", postgresql_where=text("deleted_at IS NULL")),
    )


class Skill(Base, UUIDPrimaryKey, Timestamped, SoftDeletable, UserOwned, Provenanced):
    """Normalized against a canonical dictionary; unmapped names kept verbatim."""

    __tablename__ = "skills"

    canonical_name: Mapped[str] = mapped_column(String(120), nullable=False)
    raw_name: Mapped[str] = mapped_column(String(120), nullable=False)
    category: Mapped[str | None] = mapped_column(String(48), default=None)
    proficiency: Mapped[str | None] = mapped_column(String(24), default=None)
    years_experience: Mapped[float | None] = mapped_column(Numeric(4, 1), default=None)
    last_used_year: Mapped[int | None] = mapped_column(Integer, default=None)

    __table_args__ = (
        *_provenance_constraints("skills"),
        UniqueConstraint("user_id", "canonical_name", name="uq_skills_user_canonical"),
    )


class SkillLink(Base):
    """Skill usage edges (skill ↔ experience/project), for graph expansion."""

    __tablename__ = "skill_links"

    skill_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("skills.id", ondelete="CASCADE"), primary_key=True
    )
    entity_type: Mapped[str] = mapped_column(String(24), primary_key=True)
    entity_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)

    __table_args__ = (
        CheckConstraint(
            "entity_type IN ('experience','project','achievement')",
            name="skill_links_entity_type_valid",
        ),
        Index("ix_skill_links_entity", "entity_type", "entity_id"),
    )


class Achievement(Base, UUIDPrimaryKey, Timestamped, SoftDeletable, UserOwned, Provenanced):
    """A result claim, optionally quantified.

    ``metrics`` entries carry ``source_span`` offsets into the source document.
    FR-RES-010: a metric is recorded only when it is literally present in the
    source text — the extractor may not compute, round or infer one.
    """

    __tablename__ = "achievements"

    experience_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("experiences.id", ondelete="CASCADE"), default=None, index=True
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), default=None, index=True
    )

    statement: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str | None] = mapped_column(Text, default=None)
    outcome: Mapped[str | None] = mapped_column(Text, default=None)

    metrics: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    has_quantified_metric: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    metric_source_span: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)

    __table_args__ = (
        *_provenance_constraints("achievements"),
        CheckConstraint(
            "experience_id IS NOT NULL OR project_id IS NOT NULL",
            name="achievements_require_parent",
        ),
        Index(
            "ix_achievements_user_quantified",
            "user_id",
            "has_quantified_metric",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )


class Education(Base, UUIDPrimaryKey, Timestamped, SoftDeletable, UserOwned, Provenanced):
    __tablename__ = "education"

    institution: Mapped[str] = mapped_column(String(200), nullable=False)
    degree: Mapped[str | None] = mapped_column(String(160), default=None)
    field: Mapped[str | None] = mapped_column(String(160), default=None)
    start_year: Mapped[int | None] = mapped_column(Integer, default=None)
    end_year: Mapped[int | None] = mapped_column(Integer, default=None)
    grade: Mapped[str | None] = mapped_column(String(48), default=None)
    honors: Mapped[list[str]] = mapped_column(
        ARRAY(String(160)), nullable=False, server_default=text("'{}'")
    )

    __table_args__ = (
        *_provenance_constraints("education"),
        CheckConstraint(
            "end_year IS NULL OR start_year IS NULL OR end_year >= start_year",
            name="education_year_order",
        ),
    )


class Certification(Base, UUIDPrimaryKey, Timestamped, SoftDeletable, UserOwned, Provenanced):
    __tablename__ = "certifications"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    issuer: Mapped[str | None] = mapped_column(String(200), default=None)
    issued_on: Mapped[dt.date | None] = mapped_column(Date, default=None)
    expires_on: Mapped[dt.date | None] = mapped_column(Date, default=None)
    credential_id: Mapped[str | None] = mapped_column(String(160), default=None)

    __table_args__ = _provenance_constraints("certifications")


class CandidateStory(Base, UUIDPrimaryKey, Timestamped, SoftDeletable, UserOwned):
    """An approved, reusable interview narrative (PRD §12.5).

    Deliberately not ``Provenanced``: a story has its own review lifecycle
    (suggested → approved) and traces to the graph nodes it was built from via
    ``source_evidence_ids`` rather than to a source document. FR-STORY-001: a
    story may not contain a fact absent from that evidence set.
    """

    __tablename__ = "candidate_stories"

    title: Mapped[str] = mapped_column(String(240), nullable=False)
    categories: Mapped[list[str]] = mapped_column(
        ARRAY(String(40)), nullable=False, server_default=text("'{}'")
    )

    situation: Mapped[str | None] = mapped_column(Text, default=None)
    task: Mapped[str | None] = mapped_column(Text, default=None)
    actions: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )
    result: Mapped[str | None] = mapped_column(Text, default=None)
    metrics: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )

    skills_demonstrated: Mapped[list[str]] = mapped_column(
        ARRAY(String(120)), nullable=False, server_default=text("'{}'")
    )
    roles_relevant_to: Mapped[list[str]] = mapped_column(
        ARRAY(String(80)), nullable=False, server_default=text("'{}'")
    )

    # Traceability back into the graph. Denormalized as arrays rather than a join
    # table because retrieval always loads them with the story and never queries
    # the reverse direction (documented per PRD §24.9).
    source_experience_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(Uuid), nullable=False, server_default=text("'{}'")
    )
    source_evidence_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(Uuid), nullable=False, server_default=text("'{}'")
    )

    confidence: Mapped[float | None] = mapped_column(Numeric(4, 3), default=None)
    # Speakability is a product requirement, not a nicety (PRD FR-STORY-006).
    speak_time_seconds: Mapped[int | None] = mapped_column(Integer, default=None)
    variants: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=StoryStatus.SUGGESTED, index=True
    )
    usage_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_used_at: Mapped[dt.datetime | None] = mapped_column(default=None)

    __table_args__ = (
        CheckConstraint(
            "status IN ('suggested','approved','rejected','needs_detail')",
            name="candidate_stories_status_valid",
        ),
        CheckConstraint(
            "speak_time_seconds IS NULL OR speak_time_seconds > 0",
            name="candidate_stories_speak_time_positive",
        ),
        Index(
            "ix_candidate_stories_user_status",
            "user_id",
            "status",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("ix_candidate_stories_categories", "categories", postgresql_using="gin"),
    )

    @property
    def is_evidence(self) -> bool:
        return self.status == StoryStatus.APPROVED
