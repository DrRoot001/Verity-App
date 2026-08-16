"""Preparation ranking and readiness (PRD §10.8).

The ranking formula is deterministic and explainable by design:

    priority_score = 0.35 * gap_severity
                   + 0.25 * expected_frequency
                   + 0.20 * time_pressure
                   + 0.20 * improvement_headroom

Buckets: >=0.75 critical, 0.55-0.74 high, 0.35-0.54 medium, else optional.

A learned ranker would score better on paper and be impossible to explain, and
PRD FR-PREP-002 requires every task to show its contributing factors. The
weights live here as named constants so changing them is a visible act.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from typing import Any

from verity.modules.preparation.models import PlanSection, TaskPriority

WEIGHT_GAP_SEVERITY = 0.35
WEIGHT_EXPECTED_FREQUENCY = 0.25
WEIGHT_TIME_PRESSURE = 0.20
WEIGHT_IMPROVEMENT_HEADROOM = 0.20

CRITICAL_THRESHOLD = 0.75
HIGH_THRESHOLD = 0.55
MEDIUM_THRESHOLD = 0.35

#: Beyond this horizon, time pressure stops discriminating between tasks.
TIME_PRESSURE_HORIZON_DAYS = 21

#: Default daily study budget used when distributing tasks (FR-PREP-005).
DEFAULT_DAILY_MINUTES = 45


@dataclass(slots=True)
class ScoredTask:
    section: str
    title: str
    detail: str | None
    action: dict[str, Any]
    estimated_minutes: int
    dedupe_key: str
    gap_severity: float
    expected_frequency: float
    improvement_headroom: float
    source: str = "plan"
    scheduled_for: dt.date | None = None
    priority_score: float = 0.0
    priority: str = TaskPriority.OPTIONAL
    breakdown: dict[str, Any] = field(default_factory=dict)


def time_pressure(interview_at: dt.datetime | None, effort_minutes: int) -> float:
    """Urgency from the interview date and how long the task takes.

    With no date, effort alone decides — a two-hour task still deserves to be
    started before a ten-minute one (PRD FR-PREP-005 effort-based ordering).
    """
    effort_factor = min(1.0, effort_minutes / 120)
    if interview_at is None:
        return 0.35 + 0.25 * effort_factor

    days = (interview_at - dt.datetime.now(dt.UTC)).total_seconds() / 86_400
    if days <= 0:
        return 1.0
    # Decays smoothly rather than stepping, so ordering is stable day to day.
    proximity = math.exp(-days / (TIME_PRESSURE_HORIZON_DAYS / 2))
    return round(min(1.0, 0.15 + 0.85 * proximity + 0.1 * effort_factor), 4)


def score(task: ScoredTask, interview_at: dt.datetime | None) -> ScoredTask:
    pressure = time_pressure(interview_at, task.estimated_minutes)

    contributions = {
        "gap_severity": round(WEIGHT_GAP_SEVERITY * task.gap_severity, 4),
        "expected_frequency": round(WEIGHT_EXPECTED_FREQUENCY * task.expected_frequency, 4),
        "time_pressure": round(WEIGHT_TIME_PRESSURE * pressure, 4),
        "improvement_headroom": round(WEIGHT_IMPROVEMENT_HEADROOM * task.improvement_headroom, 4),
    }
    total = round(sum(contributions.values()), 4)

    task.priority_score = min(1.0, total)
    task.priority = (
        TaskPriority.CRITICAL
        if total >= CRITICAL_THRESHOLD
        else TaskPriority.HIGH
        if total >= HIGH_THRESHOLD
        else TaskPriority.MEDIUM
        if total >= MEDIUM_THRESHOLD
        else TaskPriority.OPTIONAL
    )
    task.breakdown = {
        "factors": {
            "gap_severity": task.gap_severity,
            "expected_frequency": task.expected_frequency,
            "time_pressure": pressure,
            "improvement_headroom": task.improvement_headroom,
        },
        "weights": {
            "gap_severity": WEIGHT_GAP_SEVERITY,
            "expected_frequency": WEIGHT_EXPECTED_FREQUENCY,
            "time_pressure": WEIGHT_TIME_PRESSURE,
            "improvement_headroom": WEIGHT_IMPROVEMENT_HEADROOM,
        },
        "contributions": contributions,
        "total": total,
    }
    return task


def schedule(
    tasks: list[ScoredTask],
    *,
    interview_at: dt.datetime | None,
    daily_minutes: int = DEFAULT_DAILY_MINUTES,
    today: dt.date | None = None,
) -> list[ScoredTask]:
    """Spread tasks across the days remaining, highest priority first.

    Scheduling backwards from the interview would leave the most important work
    latest; filling forward from today means a candidate who runs out of time
    has still done the things that mattered most.
    """
    start = today or dt.date.today()
    if interview_at is None:
        return tasks

    days_available = max(1, (interview_at.date() - start).days)
    ordered = sorted(tasks, key=lambda t: -t.priority_score)

    day_offset = 0
    used_today = 0
    for task in ordered:
        if used_today + task.estimated_minutes > daily_minutes and day_offset < days_available - 1:
            day_offset += 1
            used_today = 0
        task.scheduled_for = start + dt.timedelta(days=min(day_offset, days_available - 1))
        used_today += task.estimated_minutes
    return ordered


@dataclass(slots=True)
class Readiness:
    score: float
    drivers: list[dict[str, Any]]

    def to_json(self) -> dict[str, Any]:
        return {"score": self.score, "drivers": self.drivers}


def readiness(
    *,
    tasks: list[ScoredTask],
    completed_keys: set[str],
    match_score: float | None,
    approved_story_count: int,
    covered_themes: int,
    total_themes: int,
    has_jd: bool,
) -> Readiness:
    """Weighted coverage of the work that matters, with its drivers named.

    Deliberately not a prediction of outcome (PRD FR-PREP-004) — it measures
    how much of the identified preparation has been done, and says so.
    """
    drivers: list[dict[str, Any]] = []

    important = [t for t in tasks if t.priority in (TaskPriority.CRITICAL, TaskPriority.HIGH)]
    if important:
        done = sum(1 for t in important if t.dedupe_key in completed_keys)
        coverage = done / len(important)
        drivers.append(
            {
                "factor": "critical_task_completion",
                "value": round(coverage, 3),
                "weight": 0.45,
                "detail": f"{done} of {len(important)} critical or high tasks complete",
            }
        )
    else:
        coverage = 0.0
        drivers.append(
            {
                "factor": "critical_task_completion",
                "value": 0.0,
                "weight": 0.45,
                "detail": "No plan generated yet",
            }
        )

    match_component = (match_score or 0.0) / 100
    drivers.append(
        {
            "factor": "requirement_match",
            "value": round(match_component, 3),
            "weight": 0.30,
            "detail": (
                f"{round(match_score or 0)}% of stated requirements evidenced"
                if has_jd
                else "No job description — match not computed"
            ),
        }
    )

    story_component = (covered_themes / total_themes) if total_themes else 0.0
    drivers.append(
        {
            "factor": "story_coverage",
            "value": round(story_component, 3),
            "weight": 0.25,
            "detail": (
                f"{covered_themes} of {total_themes} likely themes have an approved story"
                if total_themes
                else f"{approved_story_count} approved stories, no themes identified yet"
            ),
        }
    )

    total = 0.45 * coverage + 0.30 * match_component + 0.25 * story_component
    return Readiness(score=round(min(100.0, total * 100), 1), drivers=drivers)


#: Sections whose absence is a risk worth flagging even without a JD.
BASELINE_SECTIONS: tuple[str, ...] = (
    PlanSection.BEHAVIORAL,
    PlanSection.RESUME_DEEP_DIVE,
    PlanSection.REVERSE_QUESTIONS,
)
