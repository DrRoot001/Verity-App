"""Preparation plan generation (PRD §10.8, §11.4).

Reads the ContextBundle — never the graph or the JD directly (FR-WS-003) — and
turns its gaps into ranked, launchable tasks.

Regeneration preserves user state (FR-PREP-001): tasks are matched across
versions by ``dedupe_key``, so completing something and then re-running the
plan does not resurrect it as open work.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from verity.modules.candidate_graph.models import CandidateStory, StoryStatus
from verity.modules.candidate_graph.story_generator import StoryGenerator
from verity.modules.preparation.models import (
    PlanSection,
    PreparationPlan,
    PreparationTask,
    TaskStatus,
)
from verity.modules.preparation.planner import (
    Readiness,
    ScoredTask,
    readiness,
    schedule,
    score,
)
from verity.modules.workspace.context import ContextBundle
from verity.modules.workspace.models import Workspace
from verity.modules.workspace.service import WorkspaceService
from verity.platform.errors import AppError
from verity.platform.logging import get_logger

log = get_logger("preparation.service")

PLANNER_VERSION = "rule-planner@1"


@dataclass(slots=True)
class GeneratedPlan:
    plan: PreparationPlan
    tasks: list[PreparationTask]
    readiness: Readiness


class PreparationService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._workspaces = WorkspaceService(session)

    async def generate(self, *, user_id: uuid.UUID, workspace_id: uuid.UUID) -> GeneratedPlan:
        workspace = await self._workspaces.get(user_id=user_id, workspace_id=workspace_id)
        bundle = await self._workspaces.context(user_id=user_id, workspace_id=workspace_id)

        completed = await self._completed_keys(workspace_id)
        stories = list(
            (
                await self._session.execute(
                    select(CandidateStory).where(
                        CandidateStory.user_id == user_id,
                        CandidateStory.deleted_at.is_(None),
                    )
                )
            ).scalars()
        )

        candidates = self._build_tasks(bundle, stories)
        for task in candidates:
            score(task, workspace.interview_at)
        ordered = schedule(candidates, interview_at=workspace.interview_at)

        themes = [t.get("value", "") for t in bundle.opportunity.likely_themes]
        approved_stories = [s for s in stories if s.status == StoryStatus.APPROVED]
        covered = sum(
            1
            for theme in themes
            if any(
                theme.replace("behavioral: ", "") in (s.categories or []) for s in approved_stories
            )
        )

        computed = readiness(
            tasks=ordered,
            completed_keys=completed,
            match_score=None if bundle.match.computed_without_jd else bundle.match.overall_score,
            approved_story_count=len(approved_stories),
            covered_themes=covered,
            total_themes=len(themes),
            has_jd=bundle.opportunity.jd_present,
        )

        plan = await self._persist(workspace, bundle, ordered, completed, computed)
        workspace.readiness_score = computed.score
        workspace.readiness_computed_at = dt.datetime.now(dt.UTC)
        await self._session.flush()

        log.info(
            "preparation_plan_generated",
            workspace_id=str(workspace_id),
            tasks=len(ordered),
            readiness=computed.score,
        )
        return GeneratedPlan(plan=plan.plan, tasks=plan.tasks, readiness=computed)

    # ── Task construction ────────────────────────────────────────────

    def _build_tasks(
        self, bundle: ContextBundle, stories: list[CandidateStory]
    ) -> list[ScoredTask]:
        tasks: list[ScoredTask] = []
        tasks.extend(self._gap_tasks(bundle))
        tasks.extend(self._story_tasks(bundle, stories))
        tasks.extend(self._baseline_tasks(bundle))
        return tasks

    def _gap_tasks(self, bundle: ContextBundle) -> list[ScoredTask]:
        """Every unevidenced requirement becomes work (PRD §10.8 inputs)."""
        tasks: list[ScoredTask] = []
        for gap in bundle.match.gaps:
            must_have = gap.kind == "must_have"
            tasks.append(
                ScoredTask(
                    section=PlanSection.TECHNICAL if must_have else PlanSection.ROLE_KNOWLEDGE,
                    title=f"Prepare evidence for: {gap.requirement[:160]}",
                    detail=(
                        "No approved experience matches this requirement. Add an experience or "
                        "story that demonstrates it, or prepare how you'll address the gap."
                    ),
                    action={"type": "review_profile", "params": {"requirement": gap.requirement}},
                    estimated_minutes=25 if must_have else 15,
                    dedupe_key=f"gap:{gap.requirement[:120]}",
                    # A missing must-have is the single most costly gap.
                    gap_severity=1.0 if must_have else 0.55,
                    expected_frequency=0.9 if must_have else 0.5,
                    improvement_headroom=1.0,
                )
            )

        for requirement in bundle.match.requirements:
            if requirement.status != "partial":
                continue
            tasks.append(
                ScoredTask(
                    section=PlanSection.RESUME_DEEP_DIVE,
                    title=f"Strengthen your answer on: {requirement.requirement[:150]}",
                    detail=requirement.rationale,
                    action={
                        "type": "review_profile",
                        "params": {"requirement": requirement.requirement},
                    },
                    estimated_minutes=15,
                    dedupe_key=f"partial:{requirement.requirement[:120]}",
                    gap_severity=0.5,
                    expected_frequency=0.7 if requirement.kind == "must_have" else 0.4,
                    improvement_headroom=0.6,
                )
            )
        return tasks

    def _story_tasks(
        self, bundle: ContextBundle, stories: list[CandidateStory]
    ) -> list[ScoredTask]:
        """Uncovered themes and hollow stories (FR-STORY-003/005)."""
        tasks: list[ScoredTask] = []
        approved = [s for s in stories if s.status == StoryStatus.APPROVED]
        approved_categories = {c for s in approved for c in (s.categories or [])}

        for theme in bundle.opportunity.likely_themes:
            value = str(theme.get("value", ""))
            category = value.replace("behavioral: ", "").strip()
            if not category or category in approved_categories:
                continue
            confidence = float(theme.get("confidence", 0.5))
            tasks.append(
                ScoredTask(
                    section=PlanSection.BEHAVIORAL,
                    title=f"Prepare a story about {category.replace('_', ' ')}",
                    detail=f"Predicted from the posting: {theme.get('basis', '')}",
                    action={"type": "create_story", "params": {"category": category}},
                    estimated_minutes=20,
                    dedupe_key=f"story_theme:{category}",
                    gap_severity=0.8,
                    # The posting's own emphasis is the best frequency signal.
                    expected_frequency=confidence,
                    improvement_headroom=0.9,
                )
            )

        for story in stories:
            if story.status != StoryStatus.NEEDS_DETAIL:
                continue
            tasks.append(
                ScoredTask(
                    section=PlanSection.BEHAVIORAL,
                    title=f"Add the outcome to: {story.title[:140]}",
                    detail="This story has no result yet, so it isn't usable in an interview.",
                    action={"type": "edit_story", "params": {"story_id": str(story.id)}},
                    estimated_minutes=10,
                    dedupe_key=f"story_detail:{story.id}",
                    gap_severity=0.6,
                    expected_frequency=0.6,
                    improvement_headroom=0.85,
                )
            )
        return tasks

    def _baseline_tasks(self, bundle: ContextBundle) -> list[ScoredTask]:
        """Work that matters regardless of what the posting says."""
        tasks: list[ScoredTask] = []

        if bundle.candidate.pending_counts:
            pending = sum(bundle.candidate.pending_counts.values())
            tasks.append(
                ScoredTask(
                    section=PlanSection.RESUME_DEEP_DIVE,
                    title=f"Confirm {pending} extracted profile item(s)",
                    detail="Unconfirmed facts can't be cited during an interview.",
                    action={"type": "review_profile", "params": {}},
                    estimated_minutes=10,
                    dedupe_key="review_pending_profile",
                    gap_severity=0.9,
                    expected_frequency=1.0,
                    improvement_headroom=1.0,
                )
            )

        if not bundle.opportunity.jd_present:
            tasks.append(
                ScoredTask(
                    section=PlanSection.ROLE_KNOWLEDGE,
                    title="Add the job description",
                    detail="Without it, Verity can't tell you which requirements you evidence.",
                    action={"type": "attach_jd", "params": {}},
                    estimated_minutes=5,
                    dedupe_key="attach_jd",
                    gap_severity=1.0,
                    expected_frequency=1.0,
                    improvement_headroom=1.0,
                )
            )

        tasks.append(
            ScoredTask(
                section=PlanSection.REVERSE_QUESTIONS,
                title="Prepare questions to ask the interviewer",
                detail=f"Tailored to {bundle.opportunity.company_name} and this stage.",
                action={"type": "reverse_questions", "params": {}},
                estimated_minutes=15,
                dedupe_key="reverse_questions",
                gap_severity=0.4,
                expected_frequency=0.85,
                improvement_headroom=0.7,
            )
        )
        return tasks

    # ── Persistence ──────────────────────────────────────────────────

    async def _completed_keys(self, workspace_id: uuid.UUID) -> set[str]:
        rows = await self._session.execute(
            select(PreparationTask.dedupe_key).where(
                PreparationTask.workspace_id == workspace_id,
                PreparationTask.status.in_([TaskStatus.DONE, TaskStatus.DISMISSED]),
            )
        )
        return set(rows.scalars())

    async def _persist(
        self,
        workspace: Workspace,
        bundle: ContextBundle,
        tasks: list[ScoredTask],
        completed: set[str],
        computed: Readiness,
    ) -> GeneratedPlan:
        next_version = (
            await self._session.execute(
                select(func.coalesce(func.max(PreparationPlan.version), 0) + 1).where(
                    PreparationPlan.workspace_id == workspace.id,
                    PreparationPlan.round_index == workspace.round_index,
                )
            )
        ).scalar_one()

        plan = PreparationPlan(
            user_id=workspace.user_id,
            workspace_id=workspace.id,
            round_index=workspace.round_index,
            stage=workspace.stage,
            version=int(next_version),
            generated_by=PLANNER_VERSION,
            summary=computed.to_json(),
            context_version=bundle.version,
        )
        self._session.add(plan)
        await self._session.flush()

        # User-added tasks are carried into the new plan; a regeneration must
        # not silently discard work the user chose to track.
        carried = list(
            (
                await self._session.execute(
                    select(PreparationTask).where(
                        PreparationTask.workspace_id == workspace.id,
                        PreparationTask.source == "user",
                        PreparationTask.status.notin_([TaskStatus.DISMISSED]),
                    )
                )
            ).scalars()
        )

        rows: list[PreparationTask] = []
        for task in tasks:
            rows.append(
                PreparationTask(
                    user_id=workspace.user_id,
                    plan_id=plan.id,
                    workspace_id=workspace.id,
                    section=task.section,
                    title=task.title,
                    detail=task.detail,
                    priority=task.priority,
                    priority_score=task.priority_score,
                    score_breakdown=task.breakdown,
                    action=task.action,
                    estimated_minutes=task.estimated_minutes,
                    scheduled_for=task.scheduled_for,
                    source=task.source,
                    dedupe_key=task.dedupe_key,
                    # Completed work stays completed across regenerations.
                    status=TaskStatus.DONE if task.dedupe_key in completed else TaskStatus.OPEN,
                    completed_at=dt.datetime.now(dt.UTC) if task.dedupe_key in completed else None,
                )
            )
        for existing in carried:
            existing.plan_id = plan.id

        self._session.add_all(rows)
        await self._session.flush()
        return GeneratedPlan(plan=plan, tasks=rows + carried, readiness=computed)

    # ── Reads and mutations ──────────────────────────────────────────

    async def current_plan(
        self, *, user_id: uuid.UUID, workspace_id: uuid.UUID
    ) -> tuple[PreparationPlan, list[PreparationTask]] | None:
        await self._workspaces.get(user_id=user_id, workspace_id=workspace_id)
        plan = (
            await self._session.execute(
                select(PreparationPlan)
                .where(PreparationPlan.workspace_id == workspace_id)
                .order_by(PreparationPlan.round_index.desc(), PreparationPlan.version.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if plan is None:
            return None

        tasks = list(
            (
                await self._session.execute(
                    select(PreparationTask)
                    .where(PreparationTask.plan_id == plan.id)
                    .order_by(PreparationTask.priority_score.desc())
                )
            ).scalars()
        )
        return plan, tasks

    async def set_task_status(
        self, *, user_id: uuid.UUID, task_id: uuid.UUID, status: str
    ) -> PreparationTask:
        task = (
            await self._session.execute(
                select(PreparationTask).where(
                    PreparationTask.id == task_id, PreparationTask.user_id == user_id
                )
            )
        ).scalar_one_or_none()
        if task is None:
            raise AppError.not_found("Task")

        task.status = status
        task.completed_at = dt.datetime.now(dt.UTC) if status == TaskStatus.DONE else None
        await self._session.flush()
        return task

    async def generate_stories(self, user_id: uuid.UUID) -> list[CandidateStory]:
        generator = StoryGenerator(self._session)
        return await generator.persist(user_id, await generator.generate(user_id))


def task_to_json(task: PreparationTask) -> dict[str, Any]:
    return {
        "id": str(task.id),
        "section": task.section,
        "title": task.title,
        "detail": task.detail,
        "priority": task.priority,
        "priority_score": float(task.priority_score),
        "score_breakdown": task.score_breakdown,
        "action": task.action,
        "estimated_minutes": task.estimated_minutes,
        "scheduled_for": task.scheduled_for.isoformat() if task.scheduled_for else None,
        "status": task.status,
        "source": task.source,
    }
