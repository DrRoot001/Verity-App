"""ContextBundle: the single context path for every consumer (PRD FR-WS-003).

Preparation, mock interviews, the live copilot, reports and document generation
all call ``ContextResolver.resolve(workspace_id)``. None of them query the graph
or the JD directly. That is the whole architectural bet of the product: context
is entered once and reused everywhere (PP1, PP2), and one place is responsible
for keeping it coherent.

Two properties this file is responsible for:

- **Budgeting.** The bundle carries *structured summaries*, never raw resume or
  JD text. The realtime path has a token ladder (PRD §20.4) and full documents
  must never enter it (FR-AI-010).
- **Grounding.** Candidate evidence in the bundle comes from approved graph
  nodes only, so a consumer physically cannot cite an unreviewed extraction
  (FR-GRAPH-001).
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from verity.modules.candidate_graph.models import (
    Achievement,
    CandidateProfile,
    CandidateStory,
    Education,
    Experience,
    NodeStatus,
    Skill,
    StoryStatus,
)
from verity.modules.documents.models import Job
from verity.modules.workspace.matching import MatchResult, compute_match
from verity.modules.workspace.models import Workspace, WorkspaceContext
from verity.platform.errors import AppError
from verity.platform.logging import get_logger
from verity.platform.telemetry import stage_span

log = get_logger("workspace.context")

#: Caps on what enters the bundle. These exist so the bundle cannot grow into a
#: de facto copy of the resume; retrieval supplies depth on demand instead.
MAX_EXPERIENCES = 12
MAX_SKILLS = 60
MAX_STORIES = 25
MAX_ACHIEVEMENTS_PER_EXPERIENCE = 6


@dataclass(slots=True)
class CandidateSummary:
    """Approved candidate facts, in structured form."""

    headline: str | None = None
    experience_level: str | None = None
    years_experience: float | None = None
    experiences: list[dict[str, Any]] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    education: list[dict[str, Any]] = field(default_factory=list)
    story_index: list[dict[str, Any]] = field(default_factory=list)
    approved_counts: dict[str, int] = field(default_factory=dict)
    pending_counts: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class OpportunitySummary:
    company_name: str
    role_title: str
    seniority: str | None = None
    stage: str = "preparing"
    interview_at: str | None = None
    integrity_mode: str = "assisted"
    jd_present: bool = False
    must_have: list[str] = field(default_factory=list)
    nice_to_have: list[str] = field(default_factory=list)
    technologies: list[str] = field(default_factory=list)
    likely_themes: list[dict[str, Any]] = field(default_factory=list)
    competencies: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class ContextBundle:
    """What every consumer receives. Deliberately small and structured."""

    workspace_id: uuid.UUID
    user_id: uuid.UUID
    version: int
    candidate: CandidateSummary
    opportunity: OpportunitySummary
    match: MatchResult
    notes: str | None = None
    #: True when the bundle was built without a JD, so consumers can label
    #: everything downstream as inferred (PRD §8.2 skip paths).
    inferred_opportunity: bool = False
    warnings: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "candidate": {
                "headline": self.candidate.headline,
                "experience_level": self.candidate.experience_level,
                "years_experience": self.candidate.years_experience,
                "experiences": self.candidate.experiences,
                "skills": self.candidate.skills,
                "education": self.candidate.education,
                "story_index": self.candidate.story_index,
                "approved_counts": self.candidate.approved_counts,
                "pending_counts": self.candidate.pending_counts,
            },
            "opportunity": {
                "company_name": self.opportunity.company_name,
                "role_title": self.opportunity.role_title,
                "seniority": self.opportunity.seniority,
                "stage": self.opportunity.stage,
                "interview_at": self.opportunity.interview_at,
                "integrity_mode": self.opportunity.integrity_mode,
                "jd_present": self.opportunity.jd_present,
                "must_have": self.opportunity.must_have,
                "nice_to_have": self.opportunity.nice_to_have,
                "technologies": self.opportunity.technologies,
                "likely_themes": self.opportunity.likely_themes,
                "competencies": self.opportunity.competencies,
            },
            "notes": self.notes,
            "inferred_opportunity": self.inferred_opportunity,
            "warnings": self.warnings,
        }


class ContextResolver:
    """Builds and caches the bundle for a workspace."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def resolve(
        self, *, workspace_id: uuid.UUID, user_id: uuid.UUID, force_rebuild: bool = False
    ) -> ContextBundle:
        workspace = await self._require_workspace(workspace_id, user_id)
        record = (
            await self._session.execute(
                select(WorkspaceContext).where(WorkspaceContext.workspace_id == workspace_id)
            )
        ).scalar_one_or_none()

        fingerprint = await self._fingerprint(workspace)

        if (
            record is not None
            and not force_rebuild
            and not record.stale
            and record.source_fingerprint == fingerprint
            and record.bundle
        ):
            return _from_json(workspace, record)

        return await self.rebuild(workspace=workspace, existing=record, fingerprint=fingerprint)

    async def rebuild(
        self,
        *,
        workspace: Workspace,
        existing: WorkspaceContext | None = None,
        fingerprint: str | None = None,
    ) -> ContextBundle:
        with stage_span("context_rebuild", workspace_id=str(workspace.id)):
            candidate = await self._build_candidate_summary(workspace)
            opportunity, job = await self._build_opportunity_summary(workspace)
            match = await compute_match(
                self._session,
                user_id=workspace.user_id,
                job=job,
                opportunity_technologies=opportunity.technologies,
                must_have=opportunity.must_have,
                nice_to_have=opportunity.nice_to_have,
            )

        if existing is None:
            existing = (
                await self._session.execute(
                    select(WorkspaceContext).where(WorkspaceContext.workspace_id == workspace.id)
                )
            ).scalar_one_or_none()

        version = (existing.version + 1) if existing is not None else 1
        bundle = ContextBundle(
            workspace_id=workspace.id,
            user_id=workspace.user_id,
            version=version,
            candidate=candidate,
            opportunity=opportunity,
            match=match,
            notes=workspace.notes,
            inferred_opportunity=not opportunity.jd_present,
            warnings=_bundle_warnings(candidate, opportunity),
        )

        import datetime as dt

        if existing is None:
            existing = WorkspaceContext(
                workspace_id=workspace.id, user_id=workspace.user_id, version=version
            )
            self._session.add(existing)

        existing.bundle = bundle.to_json()
        existing.match = match.to_json()
        existing.version = version
        existing.stale = False
        existing.source_fingerprint = fingerprint or await self._fingerprint(workspace)
        existing.computed_at = dt.datetime.now(dt.UTC)
        await self._session.flush()

        log.info(
            "context_rebuilt",
            workspace_id=str(workspace.id),
            version=version,
            approved_experiences=candidate.approved_counts.get("experience", 0),
            match_score=match.overall_score,
        )
        return bundle

    async def mark_stale(self, workspace_id: uuid.UUID) -> None:
        """Flag a bundle for rebuild.

        Called by the propagation events in PRD §11.4. Marking rather than
        rebuilding inline keeps the triggering write fast; the surface shows a
        `processing` state until the rebuild lands.
        """
        record = (
            await self._session.execute(
                select(WorkspaceContext).where(WorkspaceContext.workspace_id == workspace_id)
            )
        ).scalar_one_or_none()
        if record is not None:
            record.stale = True
            await self._session.flush()

    # ── Builders ─────────────────────────────────────────────────────

    async def _build_candidate_summary(self, workspace: Workspace) -> CandidateSummary:
        user_id = workspace.user_id
        summary = CandidateSummary()

        profile = (
            await self._session.execute(
                select(CandidateProfile).where(CandidateProfile.user_id == user_id)
            )
        ).scalar_one_or_none()
        if profile is not None:
            summary.headline = profile.headline
            summary.experience_level = profile.experience_level

        experiences = list(
            (
                await self._session.execute(
                    select(Experience)
                    .where(
                        Experience.user_id == user_id,
                        Experience.status == NodeStatus.APPROVED,
                        Experience.deleted_at.is_(None),
                    )
                    .order_by(Experience.start_month.desc().nullslast())
                    .limit(MAX_EXPERIENCES)
                )
            ).scalars()
        )

        achievements_by_experience: dict[uuid.UUID, list[Achievement]] = {}
        if experiences:
            rows = (
                await self._session.execute(
                    select(Achievement).where(
                        Achievement.user_id == user_id,
                        Achievement.experience_id.in_([e.id for e in experiences]),
                        Achievement.status == NodeStatus.APPROVED,
                        Achievement.deleted_at.is_(None),
                    )
                )
            ).scalars()
            for row in rows:
                if row.experience_id is not None:
                    achievements_by_experience.setdefault(row.experience_id, []).append(row)

        total_months = 0
        for experience in experiences:
            achievements = achievements_by_experience.get(experience.id, [])
            summary.experiences.append(
                {
                    "id": str(experience.id),
                    "title": experience.effective("title"),
                    "company": experience.effective("company_name"),
                    "start": experience.start_month.isoformat() if experience.start_month else None,
                    "end": experience.end_month.isoformat() if experience.end_month else None,
                    "is_current": experience.is_current,
                    "achievements": [
                        {
                            "id": str(a.id),
                            "statement": a.statement,
                            "has_metric": a.has_quantified_metric,
                        }
                        for a in achievements[:MAX_ACHIEVEMENTS_PER_EXPERIENCE]
                    ],
                }
            )
            total_months += _months_between(experience)

        summary.years_experience = round(total_months / 12, 1) if total_months else None

        summary.skills = [
            s.canonical_name
            for s in (
                await self._session.execute(
                    select(Skill)
                    .where(
                        Skill.user_id == user_id,
                        Skill.status == NodeStatus.APPROVED,
                        Skill.deleted_at.is_(None),
                    )
                    .limit(MAX_SKILLS)
                )
            ).scalars()
        ]

        summary.education = [
            {"institution": e.institution, "degree": e.degree, "end_year": e.end_year}
            for e in (
                await self._session.execute(
                    select(Education).where(
                        Education.user_id == user_id,
                        Education.status == NodeStatus.APPROVED,
                        Education.deleted_at.is_(None),
                    )
                )
            ).scalars()
        ]

        # Titles and categories only. Full story bodies are retrieved on demand
        # so the bundle stays inside the realtime token budget.
        summary.story_index = [
            {
                "id": str(s.id),
                "title": s.title,
                "categories": list(s.categories or []),
                "speak_time_seconds": s.speak_time_seconds,
            }
            for s in (
                await self._session.execute(
                    select(CandidateStory)
                    .where(
                        CandidateStory.user_id == user_id,
                        CandidateStory.status == StoryStatus.APPROVED,
                        CandidateStory.deleted_at.is_(None),
                    )
                    .limit(MAX_STORIES)
                )
            ).scalars()
        ]

        summary.approved_counts = {
            "experience": len(summary.experiences),
            "skill": len(summary.skills),
            "story": len(summary.story_index),
            "education": len(summary.education),
        }
        summary.pending_counts = await self._pending_counts(user_id)
        return summary

    async def _pending_counts(self, user_id: uuid.UUID) -> dict[str, int]:
        """Surfaced so the UI can prompt review rather than silently ignoring."""
        counts: dict[str, int] = {}
        for label, model in (
            ("experience", Experience),
            ("skill", Skill),
            ("education", Education),
        ):
            rows = (
                await self._session.execute(
                    select(model.id).where(
                        model.user_id == user_id,
                        model.status == NodeStatus.PENDING_REVIEW,
                        model.deleted_at.is_(None),
                    )
                )
            ).all()
            if rows:
                counts[label] = len(rows)
        return counts

    async def _build_opportunity_summary(
        self, workspace: Workspace
    ) -> tuple[OpportunitySummary, Job | None]:
        summary = OpportunitySummary(
            company_name=workspace.company_name,
            role_title=workspace.role_title,
            seniority=workspace.seniority,
            stage=workspace.stage,
            interview_at=workspace.interview_at.isoformat() if workspace.interview_at else None,
            integrity_mode=workspace.integrity_mode,
        )

        if workspace.job_id is None:
            return summary, None

        job = (
            await self._session.execute(
                select(Job).where(Job.id == workspace.job_id, Job.deleted_at.is_(None))
            )
        ).scalar_one_or_none()
        if job is None:
            return summary, None

        facts = job.facts or {}
        inferences = job.inferences or {}

        summary.jd_present = True
        summary.must_have = [r["text"] for r in facts.get("must_have", [])]
        summary.nice_to_have = [r["text"] for r in facts.get("nice_to_have", [])]
        summary.technologies = list(facts.get("technologies", []))
        summary.likely_themes = [t for t in inferences.get("likely_themes", []) if t]
        summary.competencies = [c for c in inferences.get("behavioral_competencies", []) if c]

        if summary.seniority is None and (seniority := inferences.get("seniority")):
            summary.seniority = seniority.get("value")

        return summary, job

    # ── Freshness ────────────────────────────────────────────────────

    async def _fingerprint(self, workspace: Workspace) -> str:
        """Cheap signature of everything the bundle is derived from.

        Rebuilding only when this changes keeps repeated reads cheap while still
        guaranteeing a stale bundle is never served after its sources move.
        """
        approved = (
            await self._session.execute(
                select(Experience.id, Experience.updated_at).where(
                    Experience.user_id == workspace.user_id,
                    Experience.status == NodeStatus.APPROVED,
                    Experience.deleted_at.is_(None),
                )
            )
        ).all()
        stories = (
            await self._session.execute(
                select(CandidateStory.id, CandidateStory.updated_at).where(
                    CandidateStory.user_id == workspace.user_id,
                    CandidateStory.status == StoryStatus.APPROVED,
                    CandidateStory.deleted_at.is_(None),
                )
            )
        ).all()

        # Enumerate the fields the bundle is actually derived from rather than
        # leaning on ``updated_at``: that column is server-generated, so it is
        # expired after every UPDATE and reading it here would trigger a lazy
        # load in async context.
        parts = [
            str(workspace.job_id),
            str(workspace.resume_version_id),
            workspace.company_name,
            workspace.role_title,
            str(workspace.seniority),
            workspace.stage,
            str(workspace.round_index),
            workspace.integrity_mode,
            str(workspace.interview_at),
            str(workspace.notes),
            *(f"{row[0]}:{row[1]}" for row in sorted(approved, key=lambda r: str(r[0]))),
            *(f"{row[0]}:{row[1]}" for row in sorted(stories, key=lambda r: str(r[0]))),
        ]
        return hashlib.blake2b("|".join(parts).encode(), digest_size=16).hexdigest()

    async def _require_workspace(self, workspace_id: uuid.UUID, user_id: uuid.UUID) -> Workspace:
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


def _months_between(experience: Experience) -> int:
    import datetime as dt

    if experience.start_month is None:
        return 0
    end = experience.end_month or (dt.date.today() if experience.is_current else None)
    if end is None:
        return 0
    return max(
        0,
        (end.year - experience.start_month.year) * 12 + (end.month - experience.start_month.month),
    )


def _bundle_warnings(candidate: CandidateSummary, opportunity: OpportunitySummary) -> list[str]:
    warnings: list[str] = []
    if not candidate.experiences:
        warnings.append("no_approved_experience")
    if candidate.pending_counts:
        warnings.append("pending_review_items")
    if not opportunity.jd_present:
        warnings.append("no_job_description")
    if not candidate.story_index:
        warnings.append("no_approved_stories")
    return warnings


def _from_json(workspace: Workspace, record: WorkspaceContext) -> ContextBundle:
    payload = record.bundle
    candidate_data = payload.get("candidate", {})
    opportunity_data = payload.get("opportunity", {})
    return ContextBundle(
        workspace_id=workspace.id,
        user_id=workspace.user_id,
        version=record.version,
        candidate=CandidateSummary(**candidate_data),
        opportunity=OpportunitySummary(**opportunity_data),
        match=MatchResult.from_json(record.match),
        notes=payload.get("notes"),
        inferred_opportunity=payload.get("inferred_opportunity", False),
        warnings=payload.get("warnings", []),
    )
