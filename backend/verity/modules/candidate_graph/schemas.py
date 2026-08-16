"""Candidate Graph API contracts (PRD §25.2 /profile)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

EntityTypeName = Literal[
    "experience", "project", "skill", "achievement", "education", "certification"
]

Confidence = Annotated[float, Field(ge=0, le=1)]


class ProvenanceResponse(BaseModel):
    """Where a fact came from and whether a human confirmed it (PRD §12.2)."""

    status: str
    source: str
    source_ref: str | None = None
    extracted_by: str | None = None
    confidence: Confidence | None = None
    confirmed_at: dt.datetime | None = None
    has_user_corrections: bool = False


class GraphNodeResponse(BaseModel):
    id: uuid.UUID
    entity_type: EntityTypeName
    label: str
    detail: dict[str, Any] = Field(default_factory=dict)
    provenance: ProvenanceResponse


class ReviewQueueResponse(BaseModel):
    """What the onboarding review step renders (PRD §10.3 step 7)."""

    pending: list[GraphNodeResponse]
    approved_count: int
    pending_count: int
    rejected_count: int


class ExperienceUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    company_name: str | None = Field(default=None, max_length=200)
    location: str | None = Field(default=None, max_length=200)
    start_month: dt.date | None = None
    end_month: dt.date | None = None
    is_current: bool | None = None
    bullets: list[str] | None = None


class SkillUpdate(BaseModel):
    canonical_name: str | None = Field(default=None, max_length=120)
    category: str | None = Field(default=None, max_length=48)
    proficiency: str | None = Field(default=None, max_length=24)
    years_experience: float | None = Field(default=None, ge=0, le=70)


class EducationUpdate(BaseModel):
    institution: str | None = Field(default=None, max_length=200)
    degree: str | None = Field(default=None, max_length=160)
    field: str | None = Field(default=None, max_length=160)
    start_year: int | None = Field(default=None, ge=1900, le=2100)
    end_year: int | None = Field(default=None, ge=1900, le=2100)


class AchievementUpdate(BaseModel):
    statement: str | None = Field(default=None, max_length=2000)
    action: str | None = Field(default=None, max_length=2000)
    outcome: str | None = Field(default=None, max_length=2000)


class BulkApproveRequest(BaseModel):
    """Bulk approve still marks each entity user-confirmed (PRD §10.3 step 7)."""

    entity_type: EntityTypeName
    ids: list[uuid.UUID] = Field(min_length=1, max_length=200)


class ApprovalResponse(BaseModel):
    approved: int
    indexed: int


class ProfileResponse(BaseModel):
    headline: str | None
    summary: str | None
    experience_level: str | None
    target_role: str | None
    years_experience: float | None
    approved_counts: dict[str, int]
    pending_counts: dict[str, int]


class StoryResponse(BaseModel):
    id: uuid.UUID
    title: str
    categories: list[str]
    situation: str | None
    task: str | None
    actions: list[str]
    result: str | None
    metrics: list[Any]
    skills_demonstrated: list[str]
    status: str
    speak_time_seconds: int | None
    source_evidence_ids: list[uuid.UUID]


class StoryUpsert(BaseModel):
    title: str = Field(max_length=240)
    categories: list[str] = Field(default_factory=list, max_length=8)
    situation: str | None = Field(default=None, max_length=4000)
    task: str | None = Field(default=None, max_length=4000)
    actions: list[str] = Field(default_factory=list, max_length=12)
    result: str | None = Field(default=None, max_length=4000)
    skills_demonstrated: list[str] = Field(default_factory=list, max_length=20)
    source_evidence_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20)
