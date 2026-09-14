"""Live session report and coverage analysis (PRD §27, Phase 10).

A live report is not a scored assessment — nobody graded the candidate, and
inventing a score from a transcript we only partly heard would be dishonest.
What it can say truthfully is *coverage*: which requirements of this specific
role actually came up, and whether the candidate had evidence for them.

That framing is what makes the Learning Loop work. A requirement that was
probed with no approved evidence behind it is a real, actionable gap, and it
lands in ``SessionFeedback.weaknesses`` in the same shape the mock report uses,
so the preparation engine consumes it without knowing which kind of session
produced it.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from verity.modules.sessions.live_models import AiSuggestion, DetectedQuestion, LiveSession
from verity.modules.sessions.models import ReportStatus, SessionFeedback
from verity.modules.workspace.context import ContextBundle, ContextResolver
from verity.platform.errors import AppError
from verity.platform.logging import get_logger

log = get_logger("sessions.live_report")

RUBRIC_VERSION = "live-coverage-1"

#: Words too common to establish that a requirement was actually probed.
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "the",
        "with",
        "for",
        "of",
        "in",
        "on",
        "to",
        "or",
        "at",
        "years",
        "year",
        "experience",
        "strong",
        "using",
        "plus",
        "work",
        "working",
        "ability",
        "skills",
        "knowledge",
        "understanding",
        "including",
        "such",
    }
)


def _terms(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9+#.]{3,}", text.lower()) if w not in _STOPWORDS}


@dataclass(slots=True)
class RequirementCoverage:
    requirement: str
    kind: str
    probed: bool
    question_count: int
    grounded_points: int

    def to_json(self) -> dict[str, Any]:
        return {
            "requirement": self.requirement,
            "kind": self.kind,
            "probed": self.probed,
            "questions": self.question_count,
            "grounded_points": self.grounded_points,
        }


class LiveReportService:
    """Builds the report for a completed live session."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._context = ContextResolver(session)

    async def generate(self, *, user_id: uuid.UUID, session_id: uuid.UUID) -> SessionFeedback:
        """Idempotent: a second call returns the report already stored."""
        live = (
            await self._session.execute(
                select(LiveSession).where(
                    LiveSession.id == session_id,
                    LiveSession.user_id == user_id,
                    LiveSession.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if live is None:
            raise AppError.not_found("Session")

        existing = (
            await self._session.execute(
                select(SessionFeedback).where(
                    SessionFeedback.session_id == session_id,
                    SessionFeedback.session_kind == "live",
                )
            )
        ).scalar_one_or_none()
        if existing is not None and existing.status == ReportStatus.READY:
            return existing

        questions = list(
            (
                await self._session.execute(
                    select(DetectedQuestion)
                    .where(DetectedQuestion.session_id == session_id)
                    .order_by(DetectedQuestion.detected_at_ms)
                )
            ).scalars()
        )
        suggestions = list(
            (
                await self._session.execute(
                    select(AiSuggestion).where(AiSuggestion.session_id == session_id)
                )
            ).scalars()
        )

        bundle = await self._context.resolve(user_id=user_id, workspace_id=live.workspace_id)

        report = existing or SessionFeedback(
            user_id=user_id,
            session_id=session_id,
            session_kind="live",
            workspace_id=live.workspace_id,
            rubric_version=RUBRIC_VERSION,
        )

        by_question: dict[uuid.UUID, list[AiSuggestion]] = {}
        for suggestion in suggestions:
            by_question.setdefault(suggestion.detected_question_id, []).append(suggestion)

        coverage = _coverage(bundle, questions, by_question)

        report.per_question = [
            {
                "content": q.content,
                "category": q.category,
                "trigger": q.trigger,
                "detected_at_ms": q.detected_at_ms,
                "guided": bool(by_question.get(q.id)),
                "grounding": _latest(by_question.get(q.id, [])).grounding
                if by_question.get(q.id)
                else {},
                "latency_ms": _latest(by_question.get(q.id, [])).latency_ms
                if by_question.get(q.id)
                else {},
            }
            for q in questions
        ]
        report.coverage = [c.to_json() for c in coverage]
        report.delivery_metrics = {
            "questions_detected": len(questions),
            "questions_guided": len(by_question),
            "manual_questions": sum(1 for q in questions if q.trigger == "manual"),
            "session_seconds": live.duration_seconds or 0,
            "degraded_answers": sum(1 for s in suggestions if s.degraded),
        }
        report.dimension_scores = {
            "requirement_coverage": _pct(sum(1 for c in coverage if c.probed), len(coverage)),
            "evidence_backing": _pct(
                sum(1 for c in coverage if c.probed and c.grounded_points > 0),
                sum(1 for c in coverage if c.probed),
            ),
        }
        report.overall_score = (
            round(sum(report.dimension_scores.values()) / 2, 2) if coverage else None
        )

        report.strengths = [
            {
                "theme": c.requirement,
                "evidence": f"Came up {c.question_count}x with {c.grounded_points} "
                "grounded point(s) from your approved history.",
            }
            for c in coverage
            if c.probed and c.grounded_points > 0
        ][:6]

        # The loop: probed with nothing behind it is the gap worth practising.
        report.weaknesses = [
            {
                "theme": c.requirement,
                "dimension": "evidence",
                "severity": 0.8 if c.kind == "must_have" else 0.5,
                "detail": (
                    "This came up in the interview and your approved history had "
                    "nothing concrete to back it."
                ),
            }
            for c in coverage
            if c.probed and c.grounded_points == 0
        ][:8]

        report.recommendations = [
            {
                "action": f"Add a story covering {c.requirement}",
                "why": "It was asked about and you had no evidence on file.",
            }
            for c in coverage
            if c.probed and c.grounded_points == 0
        ][:5]

        report.summary = _summary(coverage, len(questions))
        report.status = ReportStatus.READY if questions else ReportStatus.PARTIAL
        if not questions:
            report.summary = "No questions were detected, so there is nothing to report yet."

        self._session.add(report)
        await self._session.flush()

        log.info(
            "live_report_generated",
            session_id=str(session_id),
            questions=len(questions),
            probed=sum(1 for c in coverage if c.probed),
        )
        return report


def _latest(suggestions: list[AiSuggestion]) -> AiSuggestion:
    return max(suggestions, key=lambda s: s.revision)


def _pct(numerator: int, denominator: int) -> float:
    return round(100.0 * numerator / denominator, 2) if denominator else 0.0


def _coverage(
    bundle: ContextBundle,
    questions: list[DetectedQuestion],
    by_question: dict[uuid.UUID, list[AiSuggestion]],
) -> list[RequirementCoverage]:
    requirements: list[tuple[str, str]] = [
        *((r, "must_have") for r in bundle.opportunity.must_have),
        *((r, "nice_to_have") for r in bundle.opportunity.nice_to_have),
        *((t, "technology") for t in bundle.opportunity.technologies),
    ]

    question_terms = [(q, _terms(q.content)) for q in questions]

    results: list[RequirementCoverage] = []
    for text, kind in requirements:
        needed = _terms(text)
        if not needed:
            continue

        matched = [q for q, terms in question_terms if needed & terms]
        grounded = 0
        for question in matched:
            for suggestion in by_question.get(question.id, []):
                grounded += sum(
                    1
                    for point in suggestion.content.get("key_points", [])
                    if isinstance(point, dict)
                    and point.get("claim_type") == "candidate_fact"
                    and point.get("evidence_ids")
                )

        results.append(
            RequirementCoverage(
                requirement=text,
                kind=kind,
                probed=bool(matched),
                question_count=len(matched),
                grounded_points=grounded,
            )
        )
    return results


def _summary(coverage: list[RequirementCoverage], questions: int) -> str:
    if not coverage:
        return (
            f"{questions} question(s) were handled. Add a job description to this "
            "workspace to see which requirements were actually covered."
        )
    probed = [c for c in coverage if c.probed]
    unbacked = [c for c in probed if c.grounded_points == 0]
    parts = [
        f"{questions} question(s) detected, covering {len(probed)} of "
        f"{len(coverage)} tracked requirements."
    ]
    if unbacked:
        parts.append(
            f"{len(unbacked)} of those had no evidence in your approved history — "
            "they are now in your prep plan."
        )
    return " ".join(parts)
