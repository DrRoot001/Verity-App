"""Story Bank generation (PRD §12.5, FR-STORY-001..006).

Turns approved experience into reusable interview narratives. Three rules make
this trustworthy rather than a paraphrasing toy:

1. **Approved sources only.** A story is built from approved nodes and records
   which ones in ``source_evidence_ids`` (FR-STORY-001).
2. **Nothing is invented.** Every sentence is assembled from text the candidate
   already wrote or confirmed. Where a Result is genuinely absent the story is
   marked ``needs_detail`` and the user is asked, rather than the gap being
   filled with plausible fiction (FR-STORY-003).
3. **Suggested, not approved.** Generated stories are proposals; only the user
   makes one citable (FR-STORY-002).

Categorization is signal-based rather than model-based, which keeps it
explainable: a story is tagged ``crisis_recovery`` because its source text
contains outage or incident language, and that reason can be shown.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from verity.modules.candidate_graph.models import (
    Achievement,
    CandidateStory,
    Experience,
    NodeStatus,
    Project,
    StoryStatus,
)
from verity.platform.logging import get_logger

log = get_logger("candidate_graph.story_generator")

#: Words at 150 wpm — the pace a person actually speaks under interview
#: pressure (PRD FR-STORY-006).
WORDS_PER_MINUTE = 150
CONCISE_MAX_SECONDS = 90

#: Category signals. Ordered by specificity so "outage" wins crisis_recovery
#: over the broader technical_challenge.
CATEGORY_SIGNALS: dict[str, tuple[str, ...]] = {
    "crisis_recovery": (
        "outage",
        "incident",
        "downtime",
        "on-call",
        "oncall",
        "postmortem",
        "sev1",
        "sev 1",
        "rollback",
        "restored",
    ),
    "leadership": ("led", "leading", "managed a team", "headed", "directed", "spearheaded"),
    "mentoring": ("mentor", "coached", "onboarded", "taught", "trained"),
    "conflict": ("disagree", "conflict", "pushback", "tension", "resolved a dispute"),
    "failure": ("failed", "mistake", "went wrong", "regret", "misjudged"),
    "mistake_and_learning": ("learned", "lesson", "in hindsight", "retrospective"),
    "deadline_pressure": ("deadline", "under pressure", "tight timeline", "crunch", "ship date"),
    "ambiguity": ("ambiguous", "undefined", "unclear requirements", "zero to one", "greenfield"),
    "ownership": ("owned", "end-to-end", "took responsibility", "drove", "from scratch"),
    "customer_impact": ("customer", "user", "client", "adoption", "satisfaction", "churn"),
    "technical_challenge": (
        "scal",
        "latency",
        "performance",
        "distributed",
        "throughput",
        "migration",
        "refactor",
        "architecture",
    ),
    "collaboration": ("cross-functional", "partnered", "collaborated", "worked with"),
    "prioritization": ("prioritis", "prioritiz", "trade-off", "tradeoff", "roadmap", "scope"),
    "innovation": ("designed", "invented", "prototyp", "introduced", "pioneered"),
    "measurable_success": ("increased", "reduced", "improved", "cut", "grew", "saved"),
    "high_pressure": ("critical", "urgent", "escalat", "high stakes"),
    "scope_negotiation": ("descoped", "negotiated scope", "pushed back on scope"),
}

#: Phrases that separate the action from its outcome. Gerunds matter as much as
#: past tense here — "…, reducing MTTR to 9 minutes" is the single most common
#: shape of a quantified resume bullet.
_RESULT_MARKERS = (
    "resulting in",
    "which led to",
    "leading to",
    "reduced",
    "reducing",
    "increased",
    "increasing",
    "improved",
    "improving",
    "cut",
    "cutting",
    "saved",
    "saving",
    "grew",
    "growing",
    "delivered",
    "delivering",
    "shipped",
    "shipping",
    "achieved",
    "achieving",
    "enabled",
    "enabling",
)

_ACTION_VERBS = (
    "built",
    "designed",
    "led",
    "implemented",
    "migrated",
    "introduced",
    "refactored",
    "automated",
    "negotiated",
    "coordinated",
    "debugged",
)


@dataclass(slots=True)
class StoryCandidate:
    title: str
    categories: list[str]
    situation: str
    task: str | None
    actions: list[str]
    result: str | None
    skills_demonstrated: list[str]
    source_experience_ids: list[uuid.UUID]
    source_evidence_ids: list[uuid.UUID]
    confidence: float
    speak_time_seconds: int
    #: Why each category was assigned, so the tagging is inspectable.
    category_basis: dict[str, str] = field(default_factory=dict)

    @property
    def needs_detail(self) -> bool:
        """A story with no outcome cannot be told in an interview."""
        return not self.result


def categorize(text: str) -> tuple[list[str], dict[str, str]]:
    lowered = text.lower()
    categories: list[str] = []
    basis: dict[str, str] = {}
    for category, signals in CATEGORY_SIGNALS.items():
        hit = next((s for s in signals if s in lowered), None)
        if hit:
            categories.append(category)
            basis[category] = f"source text contains {hit!r}"
    return categories[:4], basis


def _estimate_speak_seconds(*parts: str | None) -> int:
    words = sum(len((p or "").split()) for p in parts)
    return max(15, round(words / WORDS_PER_MINUTE * 60))


def _split_result(statement: str) -> tuple[str, str | None]:
    """Separate what was done from what changed, using the candidate's words."""
    lowered = statement.lower()
    for marker in _RESULT_MARKERS:
        index = lowered.find(marker)
        if index > 0:
            return statement[:index].strip(" ,;"), statement[index:].strip(" ,;")
    return statement, None


def _title_for(experience: Experience, seed: str) -> str:
    clause = re.split(r"[,.;]", seed)[0].strip()
    words = clause.split()
    if len(words) > 10:
        clause = " ".join(words[:10])
    return f"{clause} at {experience.effective('company_name')}"[:240]


class StoryGenerator:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def generate(self, user_id: uuid.UUID) -> list[StoryCandidate]:
        """Propose stories from every approved experience with detail to work from."""
        experiences = list(
            (
                await self._session.execute(
                    select(Experience).where(
                        Experience.user_id == user_id,
                        Experience.status == NodeStatus.APPROVED,
                        Experience.deleted_at.is_(None),
                    )
                )
            ).scalars()
        )
        if not experiences:
            return []

        achievements_by_experience: dict[uuid.UUID, list[Achievement]] = {}
        for achievement in (
            await self._session.execute(
                select(Achievement).where(
                    Achievement.user_id == user_id,
                    Achievement.status == NodeStatus.APPROVED,
                    Achievement.deleted_at.is_(None),
                )
            )
        ).scalars():
            key = achievement.experience_id
            if key is not None:
                achievements_by_experience.setdefault(key, []).append(achievement)

        projects_by_experience: dict[uuid.UUID, list[Project]] = {}
        for project in (
            await self._session.execute(
                select(Project).where(
                    Project.user_id == user_id,
                    Project.status == NodeStatus.APPROVED,
                    Project.deleted_at.is_(None),
                )
            )
        ).scalars():
            if project.experience_id is not None:
                projects_by_experience.setdefault(project.experience_id, []).append(project)

        candidates: list[StoryCandidate] = []
        for experience in experiences:
            candidates.extend(
                self._from_experience(
                    experience,
                    achievements_by_experience.get(experience.id, []),
                    projects_by_experience.get(experience.id, []),
                )
            )

        log.info("stories_generated", count=len(candidates), experiences=len(experiences))
        return candidates

    def _from_experience(
        self,
        experience: Experience,
        achievements: list[Achievement],
        projects: list[Project],
    ) -> list[StoryCandidate]:
        candidates: list[StoryCandidate] = []
        company = str(experience.effective("company_name"))
        role = str(experience.effective("title"))

        # One story per achievement: an achievement already is a situation with
        # an outcome, which is exactly the shape an interview answer needs.
        for achievement in achievements:
            action, result = _split_result(achievement.statement)
            categories, basis = categorize(f"{achievement.statement} {role}")
            if not categories:
                categories = [
                    "measurable_success" if achievement.has_quantified_metric else "ownership"
                ]

            metric_text = ", ".join(
                str(m.get("text", "")) for m in (achievement.metrics or []) if isinstance(m, dict)
            )
            situation = f"While working as {role} at {company}."
            result_text = result or (f"Measured impact: {metric_text}." if metric_text else None)

            candidates.append(
                StoryCandidate(
                    title=_title_for(experience, achievement.statement),
                    categories=categories,
                    situation=situation,
                    task=None,
                    actions=[action],
                    result=result_text,
                    skills_demonstrated=[],
                    source_experience_ids=[experience.id],
                    source_evidence_ids=[achievement.id],
                    confidence=0.7 if result_text else 0.45,
                    speak_time_seconds=_estimate_speak_seconds(situation, action, result_text),
                    category_basis=basis,
                )
            )

        # Bullets that read as actions but produced no achievement row still
        # describe real work; they become needs_detail prompts.
        covered = {a.statement for a in achievements}
        for bullet in experience.bullets or []:
            if bullet in covered or len(bullet.split()) < 6:
                continue
            if not any(verb in bullet.lower() for verb in _ACTION_VERBS):
                continue
            action, result = _split_result(bullet)
            categories, basis = categorize(f"{bullet} {role}")
            situation = f"While working as {role} at {company}."
            candidates.append(
                StoryCandidate(
                    title=_title_for(experience, bullet),
                    categories=categories or ["ownership"],
                    situation=situation,
                    task=None,
                    actions=[action],
                    result=result,
                    skills_demonstrated=[],
                    source_experience_ids=[experience.id],
                    source_evidence_ids=[experience.id],
                    confidence=0.4,
                    speak_time_seconds=_estimate_speak_seconds(situation, action, result),
                    category_basis=basis,
                )
            )

        for project in projects:
            text = f"{project.name}. {project.description or ''} {project.impact or ''}"
            categories, basis = categorize(text)
            situation = f"On {project.name} at {company}."
            candidates.append(
                StoryCandidate(
                    title=f"{project.name} at {company}"[:240],
                    categories=categories or ["technical_challenge"],
                    situation=situation,
                    task=project.role,
                    actions=[project.description] if project.description else [],
                    result=project.impact,
                    skills_demonstrated=list(project.technologies or []),
                    source_experience_ids=[experience.id],
                    source_evidence_ids=[project.id],
                    confidence=0.55,
                    speak_time_seconds=_estimate_speak_seconds(
                        situation, project.description, project.impact
                    ),
                    category_basis=basis,
                )
            )

        return candidates

    async def persist(
        self, user_id: uuid.UUID, candidates: list[StoryCandidate]
    ) -> list[CandidateStory]:
        """Write candidates as suggested stories, skipping ones already proposed."""
        existing_titles = {
            title.lower()
            for title in (
                await self._session.execute(
                    select(CandidateStory.title).where(
                        CandidateStory.user_id == user_id,
                        CandidateStory.deleted_at.is_(None),
                    )
                )
            ).scalars()
        }

        created: list[CandidateStory] = []
        for candidate in candidates:
            if candidate.title.lower() in existing_titles:
                continue
            existing_titles.add(candidate.title.lower())

            story = CandidateStory(
                user_id=user_id,
                title=candidate.title,
                categories=candidate.categories,
                situation=candidate.situation,
                task=candidate.task,
                actions=candidate.actions,
                result=candidate.result,
                skills_demonstrated=candidate.skills_demonstrated,
                source_experience_ids=candidate.source_experience_ids,
                source_evidence_ids=candidate.source_evidence_ids,
                confidence=candidate.confidence,
                speak_time_seconds=candidate.speak_time_seconds,
                # A story with no outcome is surfaced as a question to the user,
                # never approved with an invented result.
                status=(
                    StoryStatus.NEEDS_DETAIL if candidate.needs_detail else StoryStatus.SUGGESTED
                ),
            )
            self._session.add(story)
            created.append(story)

        await self._session.flush()
        return created


def coverage(stories: list[CandidateStory], themes: list[str]) -> dict[str, list[str]]:
    """Which interview themes an approved story already covers (FR-STORY-005).

    Uncovered high-probability themes are what the preparation engine turns
    into Critical tasks.
    """
    approved = [s for s in stories if s.status == StoryStatus.APPROVED]
    result: dict[str, list[str]] = {}
    for theme in themes:
        key = theme.replace("behavioral: ", "").strip()
        result[key] = [s.title for s in approved if key in (s.categories or [])]
    return result
