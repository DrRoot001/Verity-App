"""Story Bank generation and the preparation engine (PRD §10.8, §12.5).

The properties under test are the ones that make a plan trustworthy: ranking is
explainable, gaps drive the ordering, completing work survives regeneration,
and a story is never invented or approved hollow.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from verity.modules.candidate_graph.models import (
    Achievement,
    CandidateStory,
    Experience,
    NodeStatus,
    ProvenanceSource,
    Skill,
    StoryStatus,
)
from verity.modules.candidate_graph.story_generator import StoryGenerator, categorize
from verity.modules.identity.models import User
from verity.modules.preparation.models import PreparationTask, TaskPriority, TaskStatus
from verity.modules.preparation.planner import (
    ScoredTask,
    readiness,
    schedule,
    score,
    time_pressure,
)
from verity.modules.preparation.service import PreparationService
from verity.modules.workspace.service import WorkspaceService

pytestmark = pytest.mark.integration

JD = """
Senior Backend Engineer

Requirements
- 5+ years of backend engineering experience
- Strong experience with Python and PostgreSQL
- Experience operating Kubernetes in production
- Track record of leading incident response and on-call rotations

Nice to have
- Experience with Kafka and event-driven architectures
"""


async def _approved_experience(db: AsyncSession, user: User, **kw: object) -> Experience:
    row = Experience(
        user_id=user.id,
        company_name=kw.pop("company", "Northwind"),  # type: ignore[arg-type]
        title=kw.pop("title", "Backend Engineer"),  # type: ignore[arg-type]
        start_month=dt.date(2020, 1, 1),
        is_current=True,
        status=NodeStatus.APPROVED,
        source=ProvenanceSource.RESUME,
        **kw,  # type: ignore[arg-type]
    )
    db.add(row)
    await db.flush()
    return row


# ── Ranking formula (PRD §10.8) ──────────────────────────────────────


def _task(**kw: float | str | int) -> ScoredTask:
    return ScoredTask(
        section="behavioral",
        title=str(kw.get("title", "Task")),
        detail=None,
        action={"type": "noop", "params": {}},
        estimated_minutes=int(kw.get("minutes", 15)),
        dedupe_key=str(kw.get("key", "k")),
        gap_severity=float(kw.get("gap", 0.5)),
        expected_frequency=float(kw.get("freq", 0.5)),
        improvement_headroom=float(kw.get("headroom", 0.5)),
    )


def test_a_missing_must_have_close_to_the_interview_is_critical() -> None:
    soon = dt.datetime.now(dt.UTC) + dt.timedelta(days=2)
    scored = score(_task(gap=1.0, freq=0.9, headroom=1.0), soon)

    assert scored.priority == TaskPriority.CRITICAL
    assert scored.priority_score >= 0.75


def test_a_minor_task_with_no_deadline_is_optional() -> None:
    scored = score(_task(gap=0.2, freq=0.2, headroom=0.2), None)
    assert scored.priority == TaskPriority.OPTIONAL


def test_every_task_explains_its_own_ranking() -> None:
    """FR-PREP-002: no opaque ordering."""
    scored = score(_task(gap=0.8, freq=0.6, headroom=0.7), None)
    breakdown = scored.breakdown

    assert set(breakdown["factors"]) == {
        "gap_severity",
        "expected_frequency",
        "time_pressure",
        "improvement_headroom",
    }
    assert breakdown["weights"]["gap_severity"] == 0.35
    assert abs(sum(breakdown["contributions"].values()) - breakdown["total"]) < 1e-6


def test_time_pressure_rises_as_the_interview_approaches() -> None:
    far = time_pressure(dt.datetime.now(dt.UTC) + dt.timedelta(days=30), 15)
    near = time_pressure(dt.datetime.now(dt.UTC) + dt.timedelta(days=1), 15)
    past = time_pressure(dt.datetime.now(dt.UTC) - dt.timedelta(hours=1), 15)

    assert far < near < past
    assert past == 1.0


def test_effort_orders_tasks_when_no_date_is_known() -> None:
    """FR-PREP-005: without a schedule, fall back to effort-based ordering."""
    assert time_pressure(None, 120) > time_pressure(None, 10)


def test_schedule_fills_forward_from_today() -> None:
    """The most important work must land first, not nearest the interview."""
    interview = dt.datetime.now(dt.UTC) + dt.timedelta(days=5)
    tasks = [score(_task(key=f"k{i}", minutes=30, gap=i / 10), interview) for i in range(6)]

    ordered = schedule(tasks, interview_at=interview, daily_minutes=45)

    assert ordered[0].priority_score >= ordered[-1].priority_score
    assert ordered[0].scheduled_for == dt.date.today()
    assert ordered[-1].scheduled_for is not None
    assert ordered[-1].scheduled_for >= ordered[0].scheduled_for


def test_readiness_names_its_drivers() -> None:
    """FR-PREP-004: a score with no explanation is not usable feedback."""
    tasks = [score(_task(key="a", gap=1.0, freq=1.0, headroom=1.0), None)]
    result = readiness(
        tasks=tasks,
        completed_keys=set(),
        match_score=40.0,
        approved_story_count=0,
        covered_themes=0,
        total_themes=4,
        has_jd=True,
    )

    assert 0 <= result.score <= 100
    factors = {d["factor"] for d in result.drivers}
    assert factors == {"critical_task_completion", "requirement_match", "story_coverage"}
    assert all(d["detail"] for d in result.drivers)


def test_readiness_rises_when_important_work_is_done() -> None:
    tasks = [score(_task(key="a", gap=1.0, freq=1.0, headroom=1.0), None)]
    before = readiness(
        tasks=tasks,
        completed_keys=set(),
        match_score=50.0,
        approved_story_count=0,
        covered_themes=1,
        total_themes=2,
        has_jd=True,
    )
    after = readiness(
        tasks=tasks,
        completed_keys={"a"},
        match_score=50.0,
        approved_story_count=0,
        covered_themes=1,
        total_themes=2,
        has_jd=True,
    )
    assert after.score > before.score


# ── Story generation (PRD §12.5) ─────────────────────────────────────


async def test_stories_are_generated_from_approved_achievements(
    db: AsyncSession, user: User
) -> None:
    experience = await _approved_experience(db, user, title="Site Reliability Engineer")
    db.add(
        Achievement(
            user_id=user.id,
            experience_id=experience.id,
            statement="Led incident response for a payments outage, reducing MTTR from 42 minutes to 9 minutes",
            has_quantified_metric=True,
            status=NodeStatus.APPROVED,
            source=ProvenanceSource.RESUME,
        )
    )
    await db.flush()

    candidates = await StoryGenerator(db).generate(user.id)

    assert candidates
    story = candidates[0]
    assert experience.id in story.source_experience_ids
    assert story.result is not None
    assert story.speak_time_seconds > 0
    assert "crisis_recovery" in story.categories


async def test_generated_stories_are_never_auto_approved(db: AsyncSession, user: User) -> None:
    """FR-STORY-002: only the user makes a story citable."""
    experience = await _approved_experience(db, user)
    db.add(
        Achievement(
            user_id=user.id,
            experience_id=experience.id,
            statement="Built a deployment pipeline, reducing release time",
            status=NodeStatus.APPROVED,
            source=ProvenanceSource.RESUME,
        )
    )
    await db.flush()

    generator = StoryGenerator(db)
    created = await generator.persist(user.id, await generator.generate(user.id))

    assert created
    assert all(s.status != StoryStatus.APPROVED for s in created)


async def test_a_story_without_an_outcome_is_marked_needs_detail(
    db: AsyncSession, user: User
) -> None:
    """FR-STORY-003: ask for the result rather than inventing one."""
    await _approved_experience(db, user, bullets=["Implemented a service mesh across the platform"])

    generator = StoryGenerator(db)
    created = await generator.persist(user.id, await generator.generate(user.id))

    needs_detail = [s for s in created if s.status == StoryStatus.NEEDS_DETAIL]
    assert needs_detail, "a bullet with no measurable outcome must ask for one"
    assert all(s.result is None for s in needs_detail)


async def test_story_text_comes_only_from_source_material(db: AsyncSession, user: User) -> None:
    """Nothing in a generated story may be absent from what the user wrote."""
    experience = await _approved_experience(db, user, company="Northwind")
    statement = "Migrated the billing service, reducing error rates"
    db.add(
        Achievement(
            user_id=user.id,
            experience_id=experience.id,
            statement=statement,
            status=NodeStatus.APPROVED,
            source=ProvenanceSource.RESUME,
        )
    )
    await db.flush()

    candidates = await StoryGenerator(db).generate(user.id)
    story = next(c for c in candidates if "Migrated" in c.title)

    reconstructed = " ".join([*story.actions, story.result or ""])
    assert "Migrated the billing service" in reconstructed
    assert "Northwind" in story.situation


async def test_regeneration_does_not_duplicate_stories(db: AsyncSession, user: User) -> None:
    experience = await _approved_experience(db, user)
    db.add(
        Achievement(
            user_id=user.id,
            experience_id=experience.id,
            statement="Cut build times, improving developer throughput",
            status=NodeStatus.APPROVED,
            source=ProvenanceSource.RESUME,
        )
    )
    await db.flush()

    generator = StoryGenerator(db)
    first = await generator.persist(user.id, await generator.generate(user.id))
    second = await generator.persist(user.id, await generator.generate(user.id))

    assert first
    assert second == []


def test_categorization_states_its_basis() -> None:
    categories, basis = categorize("Led the response to a production outage")
    assert "crisis_recovery" in categories
    assert "outage" in basis["crisis_recovery"]


# ── Plan generation ──────────────────────────────────────────────────


async def test_gaps_become_ranked_tasks(db: AsyncSession, user: User) -> None:
    workspace, _ = await WorkspaceService(db).create(
        user_id=user.id,
        company_name="Northwind",
        role_title="Senior Backend Engineer",
        jd_text=JD,
        interview_at=dt.datetime.now(dt.UTC) + dt.timedelta(days=3),
    )

    result = await PreparationService(db).generate(user_id=user.id, workspace_id=workspace.id)

    assert result.tasks
    titles = [t.title for t in result.tasks]
    assert any("Prepare evidence for" in t for t in titles)
    assert any(t.priority == TaskPriority.CRITICAL for t in result.tasks)
    assert all(t.action.get("type") for t in result.tasks), "every task must be launchable"
    assert all(t.score_breakdown for t in result.tasks)


async def test_plan_prioritises_must_have_gaps_over_optional_work(
    db: AsyncSession, user: User
) -> None:
    workspace, _ = await WorkspaceService(db).create(
        user_id=user.id,
        company_name="Northwind",
        role_title="Senior Backend Engineer",
        jd_text=JD,
        interview_at=dt.datetime.now(dt.UTC) + dt.timedelta(days=2),
    )

    result = await PreparationService(db).generate(user_id=user.id, workspace_id=workspace.id)
    ordered = sorted(result.tasks, key=lambda t: -float(t.priority_score))

    assert "Prepare evidence for" in ordered[0].title or "Confirm" in ordered[0].title


async def test_missing_jd_produces_a_critical_task(db: AsyncSession, user: User) -> None:
    workspace, _ = await WorkspaceService(db).create(
        user_id=user.id, company_name="Northwind", role_title="Engineer"
    )

    result = await PreparationService(db).generate(user_id=user.id, workspace_id=workspace.id)

    jd_task = next(t for t in result.tasks if t.dedupe_key == "attach_jd")
    assert jd_task.priority in (TaskPriority.CRITICAL, TaskPriority.HIGH)


async def test_completed_work_survives_regeneration(db: AsyncSession, user: User) -> None:
    """FR-PREP-001: regeneration preserves user state."""
    workspace, _ = await WorkspaceService(db).create(
        user_id=user.id, company_name="Northwind", role_title="Engineer", jd_text=JD
    )
    service = PreparationService(db)
    first = await service.generate(user_id=user.id, workspace_id=workspace.id)

    target = next(t for t in first.tasks if t.dedupe_key == "reverse_questions")
    await service.set_task_status(user_id=user.id, task_id=target.id, status=TaskStatus.DONE)

    second = await service.generate(user_id=user.id, workspace_id=workspace.id)
    regenerated = next(t for t in second.tasks if t.dedupe_key == "reverse_questions")

    assert regenerated.status == TaskStatus.DONE
    assert regenerated.id != target.id, "a new plan version creates new task rows"


async def test_readiness_is_recorded_on_the_workspace(db: AsyncSession, user: User) -> None:
    workspace, _ = await WorkspaceService(db).create(
        user_id=user.id, company_name="Northwind", role_title="Engineer", jd_text=JD
    )
    result = await PreparationService(db).generate(user_id=user.id, workspace_id=workspace.id)

    await db.refresh(workspace)
    assert workspace.readiness_score is not None
    assert float(workspace.readiness_score) == result.readiness.score
    assert workspace.readiness_computed_at is not None


async def test_approved_story_removes_its_theme_task(db: AsyncSession, user: User) -> None:
    """The loop closes: approving a story drops the task that asked for it."""
    workspace, _ = await WorkspaceService(db).create(
        user_id=user.id,
        company_name="Northwind",
        role_title="Senior Backend Engineer",
        jd_text=JD,
    )
    service = PreparationService(db)
    before = await service.generate(user_id=user.id, workspace_id=workspace.id)

    theme_tasks = [t for t in before.tasks if t.dedupe_key.startswith("story_theme:")]
    assert theme_tasks, "expected uncovered themes to produce tasks"
    category = theme_tasks[0].dedupe_key.split(":", 1)[1]

    db.add(
        CandidateStory(
            user_id=user.id,
            title=f"A story about {category}",
            categories=[category],
            situation="Something happened",
            result="It was resolved",
            status=StoryStatus.APPROVED,
        )
    )
    await db.flush()

    after = await service.generate(user_id=user.id, workspace_id=workspace.id)
    remaining = {t.dedupe_key for t in after.tasks}
    assert f"story_theme:{category}" not in remaining


async def test_plan_is_scoped_to_the_owner(db: AsyncSession, user: User, other_user: User) -> None:
    workspace, _ = await WorkspaceService(db).create(
        user_id=other_user.id, company_name="Northwind", role_title="Engineer"
    )
    from verity.platform.errors import AppError

    with pytest.raises(AppError):
        await PreparationService(db).generate(user_id=user.id, workspace_id=workspace.id)


async def test_approved_evidence_reduces_gap_tasks(db: AsyncSession, user: User) -> None:
    experience = await _approved_experience(
        db,
        user,
        title="Backend Engineer",
        bullets=[
            "Operated Kubernetes clusters in production",
            "Owned Python services end-to-end with PostgreSQL",
        ],
    )
    for name in ("Python", "PostgreSQL", "Kubernetes"):
        db.add(
            Skill(
                user_id=user.id,
                canonical_name=name,
                raw_name=name,
                status=NodeStatus.APPROVED,
                source=ProvenanceSource.RESUME,
            )
        )
    await db.flush()
    assert experience.id

    workspace, _ = await WorkspaceService(db).create(
        user_id=user.id,
        company_name="Northwind",
        role_title="Senior Backend Engineer",
        jd_text=JD,
    )
    result = await PreparationService(db).generate(user_id=user.id, workspace_id=workspace.id)

    gap_tasks = [t for t in result.tasks if t.dedupe_key.startswith("gap:")]
    all_requirements = 5
    assert len(gap_tasks) < all_requirements, "evidenced requirements must not become gaps"


async def test_tasks_persist_and_are_readable(db: AsyncSession, user: User) -> None:
    workspace, _ = await WorkspaceService(db).create(
        user_id=user.id, company_name="Northwind", role_title="Engineer", jd_text=JD
    )
    service = PreparationService(db)
    await service.generate(user_id=user.id, workspace_id=workspace.id)

    current = await service.current_plan(user_id=user.id, workspace_id=workspace.id)
    assert current is not None
    plan, tasks = current

    stored = (
        (await db.execute(select(PreparationTask).where(PreparationTask.plan_id == plan.id)))
        .scalars()
        .all()
    )
    assert len(stored) == len(tasks)
    assert tasks == sorted(tasks, key=lambda t: -float(t.priority_score))
