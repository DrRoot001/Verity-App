"""Source documents: resumes and job descriptions (PRD §24.3, §24.4).

A resume is a *source document*, not the candidate's identity — the Candidate
Graph owns that (PRD §66). Each upload creates an immutable version holding
three layers: ``raw_text``, ``ai_extracted`` and ``user_corrected``. Keeping all
three is what lets a re-upload be diffed and merged instead of overwriting a
human's edits (FR-RES-005/006).
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
    UniqueConstraint,
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


class ResumeStatus(StrEnum):
    """Drives the UI processing states (PRD §10.5)."""

    UPLOADING = "uploading"
    SCANNING = "scanning"
    EXTRACTING = "extracting"
    PARSING = "parsing"
    NEEDS_REVIEW = "needs_review"
    READY = "ready"
    FAILED_UNREADABLE = "failed_unreadable"
    FAILED_INFECTED = "failed_infected"
    FAILED_TOO_LARGE = "failed_too_large"
    SUPERSEDED = "superseded"


class SourceType(StrEnum):
    UPLOAD = "upload"
    PASTE = "paste"
    BUILDER = "builder"
    URL = "url"


class JobStatus(StrEnum):
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


class Resume(Base, UUIDPrimaryKey, Timestamped, SoftDeletable, UserOwned):
    __tablename__ = "resumes"

    label: Mapped[str] = mapped_column(String(160), nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))

    __table_args__ = (
        # At most one primary resume per user, ignoring deleted rows.
        Index(
            "uq_resumes_primary_per_user",
            "user_id",
            unique=True,
            postgresql_where=text("is_primary AND deleted_at IS NULL"),
        ),
    )


class ResumeVersion(Base, UUIDPrimaryKey, Timestamped, SoftDeletable, UserOwned):
    __tablename__ = "resume_versions"

    resume_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("resumes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_type: Mapped[str] = mapped_column(String(16), nullable=False)

    storage_key: Mapped[str | None] = mapped_column(String(512), default=None)
    mime_type: Mapped[str | None] = mapped_column(String(120), default=None)
    original_filename: Mapped[str | None] = mapped_column(String(255), default=None)
    byte_size: Mapped[int | None] = mapped_column(Integer, default=None)
    checksum_sha256: Mapped[bytes | None] = mapped_column(default=None)

    raw_text: Mapped[str | None] = mapped_column(Text, default=None)
    page_count: Mapped[int | None] = mapped_column(Integer, default=None)
    extraction_quality: Mapped[float | None] = mapped_column(Numeric(4, 3), default=None)
    extraction_warnings: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )

    #: Layered representation (FR-RES-005). ``user_corrected`` always wins.
    ai_extracted: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    user_corrected: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    status: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default=ResumeStatus.UPLOADING
    )
    failure_code: Mapped[str | None] = mapped_column(String(48), default=None)
    parsed_at: Mapped[dt.datetime | None] = mapped_column(default=None)

    __table_args__ = (
        UniqueConstraint("resume_id", "version", name="uq_resume_versions_resume_version"),
        CheckConstraint("version > 0", name="resume_versions_version_positive"),
        CheckConstraint(
            "extraction_quality IS NULL OR (extraction_quality >= 0 AND extraction_quality <= 1)",
            name="resume_versions_quality_range",
        ),
        Index(
            "ix_resume_versions_user_recent",
            "user_id",
            "created_at",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    @property
    def is_processed(self) -> bool:
        return self.status in (ResumeStatus.NEEDS_REVIEW, ResumeStatus.READY)


class Company(Base, UUIDPrimaryKey, Timestamped):
    """Shared across users — research is cached per company, not per user.

    Deliberately not ``UserOwned``: amortizing company research across everyone
    interviewing there is what keeps that feature affordable (PRD FR-DOC-022).
    """

    __tablename__ = "companies"

    canonical_name: Mapped[str] = mapped_column(String(200), nullable=False)
    domain: Mapped[str | None] = mapped_column(String(255), default=None)
    industry: Mapped[str | None] = mapped_column(String(120), default=None)
    size_band: Mapped[str | None] = mapped_column(String(32), default=None)
    hq_location: Mapped[str | None] = mapped_column(String(200), default=None)

    #: Sectioned research; every external claim carries source + retrieved_at
    #: so nothing stale is presented as current (PRD FR-DOC-020/021).
    profile: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    research_refreshed_at: Mapped[dt.datetime | None] = mapped_column(default=None)

    __table_args__ = (
        UniqueConstraint("canonical_name", "domain", name="uq_companies_name_domain"),
    )


class Job(Base, UUIDPrimaryKey, Timestamped, SoftDeletable, UserOwned):
    """An ingested job description (PRD §10.6).

    ``facts`` and ``inferences`` are separate columns, not one blob, because the
    UI must render them differently: a requirement quoted from the posting and a
    model's guess about interview themes are not the same kind of claim
    (FR-JD-002).
    """

    __tablename__ = "jobs"

    company_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("companies.id", ondelete="SET NULL"), default=None, index=True
    )
    company_name: Mapped[str | None] = mapped_column(String(200), default=None)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    seniority: Mapped[str | None] = mapped_column(String(32), default=None)
    location: Mapped[str | None] = mapped_column(String(200), default=None)
    remote_policy: Mapped[str | None] = mapped_column(String(32), default=None)

    source_type: Mapped[str] = mapped_column(String(16), nullable=False)
    source_url: Mapped[str | None] = mapped_column(String(1000), default=None)
    storage_key: Mapped[str | None] = mapped_column(String(512), default=None)
    raw_text: Mapped[str | None] = mapped_column(Text, default=None)

    facts: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    inferences: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=JobStatus.PROCESSING
    )

    __table_args__ = (
        Index(
            "ix_jobs_user_recent",
            "user_id",
            "created_at",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )
