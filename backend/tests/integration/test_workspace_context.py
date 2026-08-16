"""Interview Workspace and ContextBundle (PRD §11, AC-WS-001).

This is the architectural spine: one canonical opportunity context that every
consumer reads through. The tests here protect three properties:

- Context is entered once and reused (PP1/PP2, FR-WS-003).
- Only approved evidence reaches the bundle (FR-GRAPH-001).
- Rebinding a resume previews its impact before changing anything (FR-WS-002).
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from verity.modules.candidate_graph.models import (
    CandidateStory,
    Experience,
    NodeStatus,
    ProvenanceSource,
    Skill,
    StoryStatus,
)
from verity.modules.documents.service import ResumeIngestionService
from verity.modules.identity.models import User
from verity.modules.workspace.context import ContextResolver
from verity.modules.workspace.models import InterviewStage, WorkspaceContext
from verity.modules.workspace.service import WorkspaceService
from verity.platform.errors import AppError

pytestmark = pytest.mark.integration


JD_TEXT = """
Senior Backend Engineer

What you'll do
- Design and operate distributed services handling high traffic
- Partner with cross-functional stakeholders on the platform roadmap
- Participate in the on-call rotation and lead incident response

Requirements
- 5+ years of backend engineering experience
- Strong experience with Python and PostgreSQL
- Experience operating Kubernetes in production
- Track record owning services end-to-end

Nice to have
- Experience with Kafka and event-driven architectures
- Exposure to Terraform and infrastructure as code
"""


async def _approved_experience(
    db: AsyncSession, user: User, *, title: str, company: str, bullets: list[str]
) -> Experience:
    row = Experience(
        user_id=user.id,
        company_name=company,
        title=title,
        bullets=bullets,
        start_month=dt.date(2019, 1, 1),
        is_current=True,
        status=NodeStatus.APPROVED,
        source=ProvenanceSource.RESUME,
        confirmed_at=dt.datetime.now(dt.UTC),
    )
    db.add(row)
    await db.flush()
    return row


async def _approved_skill(db: AsyncSession, user: User, name: str) -> Skill:
    row = Skill(
        user_id=user.id,
        canonical_name=name,
        raw_name=name,
        status=NodeStatus.APPROVED,
        source=ProvenanceSource.RESUME,
    )
    db.add(row)
    await db.flush()
    return row


# ── Creation and the skip paths ──────────────────────────────────────


async def test_workspace_needs_only_company_and_role(db: AsyncSession, user: User) -> None:
    """FR-WS-001: everything else is progressively enrichable."""
    workspace, bundle = await WorkspaceService(db).create(
        user_id=user.id, company_name="Northwind Systems", role_title="Senior Backend Engineer"
    )

    assert workspace.stage == InterviewStage.PREPARING
    assert bundle.version == 1
    assert bundle.opportunity.company_name == "Northwind Systems"
    assert bundle.inferred_opportunity is True
    assert "no_job_description" in bundle.warnings


async def test_match_is_not_invented_without_a_jd(db: AsyncSession, user: User) -> None:
    """PRD §8.2: with no JD, admit the gap rather than fabricate a score."""
    _, bundle = await WorkspaceService(db).create(
        user_id=user.id, company_name="Northwind", role_title="Engineer"
    )

    assert bundle.match.computed_without_jd is True
    assert bundle.match.overall_score == 0.0
    assert bundle.match.requirements == []


async def test_jd_attachment_populates_facts_and_inferences(db: AsyncSession, user: User) -> None:
    """FR-JD-002: facts and inferences stay separate objects."""
    service = WorkspaceService(db)
    workspace, _ = await service.create(
        user_id=user.id, company_name="Northwind", role_title="Senior Backend Engineer"
    )
    job = await service.attach_job_description(
        user_id=user.id, workspace=workspace, raw_text=JD_TEXT
    )

    assert job.facts["must_have"], "expected extracted requirements"
    assert "Python" in job.facts["technologies"]
    assert "Kubernetes" in job.facts["technologies"]

    seniority = job.inferences["seniority"]
    assert seniority["value"] == "senior"
    assert 0 < seniority["confidence"] <= 1
    assert seniority["basis"], "an inference must state its basis"

    # Facts carry their source span; inferences never masquerade as facts.
    assert all(r.get("source_span") for r in job.facts["must_have"])


# ── AC-WS-001: the bundle and its evidence ───────────────────────────


async def test_bundle_contains_only_approved_evidence(db: AsyncSession, user: User) -> None:
    """FR-GRAPH-001: a pending extraction must not reach any consumer."""
    await _approved_experience(
        db, user, title="Backend Engineer", company="Northwind", bullets=["Ran Kubernetes"]
    )
    pending = Experience(
        user_id=user.id,
        company_name="Ghost Corp",
        title="Principal Engineer",
        status=NodeStatus.PENDING_REVIEW,
        source=ProvenanceSource.RESUME,
    )
    db.add(pending)
    await db.flush()

    _, bundle = await WorkspaceService(db).create(
        user_id=user.id, company_name="Northwind", role_title="Engineer", jd_text=JD_TEXT
    )

    companies = {e["company"] for e in bundle.candidate.experiences}
    assert "Northwind" in companies
    assert "Ghost Corp" not in companies
    assert bundle.candidate.pending_counts.get("experience") == 1
    assert "pending_review_items" in bundle.warnings


async def test_match_binds_requirements_to_specific_evidence(db: AsyncSession, user: User) -> None:
    """FR-JD-005: a match explains itself with real records, not a percentage."""
    experience = await _approved_experience(
        db,
        user,
        title="Backend Engineer",
        company="Northwind",
        bullets=[
            "Operated Kubernetes clusters in production",
            "Owned Python services end-to-end with PostgreSQL",
        ],
    )
    for skill in ("Python", "PostgreSQL", "Kubernetes"):
        await _approved_skill(db, user, skill)

    _, bundle = await WorkspaceService(db).create(
        user_id=user.id,
        company_name="Northwind",
        role_title="Senior Backend Engineer",
        jd_text=JD_TEXT,
    )

    assert bundle.match.overall_score > 0
    assert bundle.match.strengths, "expected at least one satisfied requirement"

    evidence_ids = {e.id for r in bundle.match.requirements for e in r.evidence}
    assert str(experience.id) in evidence_ids

    # Every requirement states its status and why.
    assert all(r.rationale for r in bundle.match.requirements)
    assert bundle.match.formula["must_have"] > bundle.match.formula["nice_to_have"]


async def test_gaps_are_reported_when_evidence_is_absent(db: AsyncSession, user: User) -> None:
    await _approved_experience(
        db, user, title="Graphic Designer", company="Studio", bullets=["Brand illustration"]
    )

    _, bundle = await WorkspaceService(db).create(
        user_id=user.id,
        company_name="Northwind",
        role_title="Senior Backend Engineer",
        jd_text=JD_TEXT,
    )

    assert bundle.match.gaps, "unrelated experience must produce gaps"
    assert all(g.evidence == [] for g in bundle.match.gaps)
    assert all("No approved experience" in g.rationale for g in bundle.match.gaps)


async def test_story_index_carries_titles_not_bodies(db: AsyncSession, user: User) -> None:
    """The bundle must stay inside the realtime token budget (§20.4)."""
    story = CandidateStory(
        user_id=user.id,
        title="Recovering a failed migration",
        situation="A long situation paragraph that must not enter the bundle. " * 20,
        result="Restored service in eleven minutes",
        categories=["crisis_recovery"],
        status=StoryStatus.APPROVED,
    )
    db.add(story)
    await db.flush()

    _, bundle = await WorkspaceService(db).create(
        user_id=user.id, company_name="Northwind", role_title="Engineer"
    )

    entry = next(s for s in bundle.candidate.story_index if s["id"] == str(story.id))
    assert entry["title"] == "Recovering a failed migration"
    assert "situation" not in entry
    assert "must not enter the bundle" not in str(bundle.to_json())


async def test_bundle_never_contains_raw_resume_text(db: AsyncSession, user: User) -> None:
    """FR-AI-010: full documents may never enter the realtime context path."""
    sentinel = "UNIQUESENTINELSTRINGTHATSHOULDNOTAPPEAR"
    await ResumeIngestionService(db).ingest_text(
        user_id=user.id,
        raw_text=(
            "Jane Doe\n\nWORK EXPERIENCE\n\nEngineer at Acme\n2020 - Present\n"
            f"- Built systems {sentinel}\n\nTECHNICAL SKILLS\nPython, Go\n"
        ),
    )

    _, bundle = await WorkspaceService(db).create(
        user_id=user.id, company_name="Northwind", role_title="Engineer"
    )

    assert sentinel not in str(bundle.to_json())


# ── Caching and freshness ────────────────────────────────────────────


async def test_repeated_resolve_reuses_the_cached_bundle(db: AsyncSession, user: User) -> None:
    service = WorkspaceService(db)
    workspace, first = await service.create(
        user_id=user.id, company_name="Northwind", role_title="Engineer"
    )

    second = await service.context(user_id=user.id, workspace_id=workspace.id)
    assert second.version == first.version, "unchanged sources must not trigger a rebuild"


async def test_approving_new_evidence_refreshes_the_bundle(db: AsyncSession, user: User) -> None:
    """PRD §11.4: the user never manually synchronizes anything."""
    service = WorkspaceService(db)
    workspace, first = await service.create(
        user_id=user.id, company_name="Northwind", role_title="Engineer"
    )
    assert first.candidate.experiences == []

    await _approved_experience(
        db, user, title="Backend Engineer", company="Northwind", bullets=["Built services"]
    )

    refreshed = await service.context(user_id=user.id, workspace_id=workspace.id)
    assert refreshed.version > first.version
    assert len(refreshed.candidate.experiences) == 1


async def test_mark_stale_forces_a_rebuild(db: AsyncSession, user: User) -> None:
    service = WorkspaceService(db)
    workspace, first = await service.create(
        user_id=user.id, company_name="Northwind", role_title="Engineer"
    )

    await ContextResolver(db).mark_stale(workspace.id)
    rebuilt = await service.context(user_id=user.id, workspace_id=workspace.id)

    assert rebuilt.version > first.version
    record = (
        await db.execute(
            select(WorkspaceContext).where(WorkspaceContext.workspace_id == workspace.id)
        )
    ).scalar_one()
    assert record.stale is False


async def test_graph_change_invalidates_every_active_workspace(
    db: AsyncSession, user: User
) -> None:
    service = WorkspaceService(db)
    first, _ = await service.create(user_id=user.id, company_name="A", role_title="Engineer")
    second, _ = await service.create(user_id=user.id, company_name="B", role_title="Engineer")

    invalidated = await service.invalidate_for_user(user.id)
    assert invalidated == 2

    for workspace_id in (first.id, second.id):
        record = (
            await db.execute(
                select(WorkspaceContext).where(WorkspaceContext.workspace_id == workspace_id)
            )
        ).scalar_one()
        assert record.stale is True


# ── AC-WS-001: resume rebinding shows impact first ───────────────────


async def test_resume_rebind_previews_impact_without_committing(
    db: AsyncSession, user: User
) -> None:
    """FR-WS-002: the user sees the consequence before confirming."""
    ingestion = await ResumeIngestionService(db).ingest_text(
        user_id=user.id,
        raw_text=(
            "Jane Doe\n\nWORK EXPERIENCE\n\nBackend Engineer at Acme\n2020 - Present\n"
            "- Operated Kubernetes and Python services\n\nTECHNICAL SKILLS\nPython, k8s\n"
        ),
    )
    service = WorkspaceService(db)
    workspace, _ = await service.create(
        user_id=user.id,
        company_name="Northwind",
        role_title="Senior Backend Engineer",
        jd_text=JD_TEXT,
    )

    preview = await service.preview_resume_rebind(
        user_id=user.id, workspace_id=workspace.id, resume_version_id=ingestion.version.id
    )

    assert preview.new_version_id == ingestion.version.id
    assert preview.match_score_after >= 0
    # The preview must not have committed the binding.
    await db.refresh(workspace)
    assert workspace.resume_version_id is None


async def test_binding_a_resume_recomputes_context(db: AsyncSession, user: User) -> None:
    ingestion = await ResumeIngestionService(db).ingest_text(
        user_id=user.id,
        raw_text=(
            "Jane Doe\n\nWORK EXPERIENCE\n\nBackend Engineer at Acme\n2020 - Present\n"
            "- Built Python services\n\nTECHNICAL SKILLS\nPython\n"
        ),
    )
    service = WorkspaceService(db)
    workspace, before = await service.create(
        user_id=user.id, company_name="Northwind", role_title="Engineer", jd_text=JD_TEXT
    )

    after = await service.bind_resume(
        user_id=user.id, workspace_id=workspace.id, resume_version_id=ingestion.version.id
    )

    assert after.version > before.version
    await db.refresh(workspace)
    assert workspace.resume_version_id == ingestion.version.id


# ── Stages and lifecycle ─────────────────────────────────────────────


async def test_advancing_stage_opens_a_new_round(db: AsyncSession, user: User) -> None:
    service = WorkspaceService(db)
    workspace, _ = await service.create(
        user_id=user.id, company_name="Northwind", role_title="Engineer"
    )
    assert workspace.round_index == 1

    await service.advance_stage(
        user_id=user.id, workspace_id=workspace.id, stage=InterviewStage.TECHNICAL
    )
    await db.refresh(workspace)

    assert workspace.stage == InterviewStage.TECHNICAL
    assert workspace.round_index == 2


async def test_closed_workspace_rejects_further_stages(db: AsyncSession, user: User) -> None:
    service = WorkspaceService(db)
    workspace, _ = await service.create(
        user_id=user.id, company_name="Northwind", role_title="Engineer"
    )
    await service.advance_stage(
        user_id=user.id, workspace_id=workspace.id, stage=InterviewStage.REJECTED
    )

    with pytest.raises(AppError) as exc:
        await service.advance_stage(
            user_id=user.id, workspace_id=workspace.id, stage=InterviewStage.ONSITE
        )
    assert exc.value.code == "invalid_state_transition"


async def test_proctored_mode_disables_live_copilot(db: AsyncSession, user: User) -> None:
    """FR-PRIV-011: the product takes a position rather than deferring."""
    service = WorkspaceService(db)
    assisted, _ = await service.create(user_id=user.id, company_name="A", role_title="Engineer")
    proctored, _ = await service.create(
        user_id=user.id, company_name="B", role_title="Engineer", integrity_mode="proctored"
    )

    assert assisted.is_live_copilot_allowed is True
    assert proctored.is_live_copilot_allowed is False


# ── Tenant isolation ─────────────────────────────────────────────────


async def test_workspace_access_is_scoped_to_the_owner(
    db: AsyncSession, user: User, other_user: User
) -> None:
    workspace, _ = await WorkspaceService(db).create(
        user_id=other_user.id, company_name="Northwind", role_title="Engineer"
    )

    with pytest.raises(AppError) as exc:
        await WorkspaceService(db).get(user_id=user.id, workspace_id=workspace.id)
    assert exc.value.status_code == 404


async def test_context_of_another_user_is_not_reachable(
    db: AsyncSession, user: User, other_user: User
) -> None:
    workspace, _ = await WorkspaceService(db).create(
        user_id=other_user.id, company_name="Northwind", role_title="Engineer"
    )

    with pytest.raises(AppError):
        await WorkspaceService(db).context(user_id=user.id, workspace_id=workspace.id)


async def test_another_users_evidence_never_enters_the_bundle(
    db: AsyncSession, user: User, other_user: User
) -> None:
    await _approved_experience(
        db, other_user, title="Zebrafish Researcher", company="Acme Labs", bullets=["Research"]
    )

    _, bundle = await WorkspaceService(db).create(
        user_id=user.id, company_name="Northwind", role_title="Engineer"
    )

    assert bundle.candidate.experiences == []
