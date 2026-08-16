"""Mock interview orchestration and reporting (PRD §13, §27).

The loop this closes is the product's retention thesis: a session produces
measured weaknesses, weaknesses become preparation tasks, and the next round's
plan reflects what actually went wrong (PRD §11.4, FR-MOCK-022).
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from verity.ai.gateway import AIGateway, build_gateway
from verity.ai.prompts import registry
from verity.ai.providers.base import TaskClass
from verity.modules.sessions import delivery
from verity.modules.sessions.interviewer import (
    InterviewerState,
    TurnIntent,
    apply_turn,
    decide_next_turn,
    is_repeat,
    record_answer_score,
)
from verity.modules.sessions.models import (
    MockSession,
    QuestionAttempt,
    ReportStatus,
    SessionFeedback,
    SessionStatus,
    SpeakerRole,
    TranscriptSegment,
)
from verity.modules.workspace.service import WorkspaceService
from verity.platform.errors import AppError, ErrorCode
from verity.platform.logging import get_logger

log = get_logger("sessions.service")

RUBRIC_VERSION = "rubric@1"
MAX_QUESTIONS_PER_SESSION = 24
#: Retries would double-charge for a question the candidate already answered.
TURN_TIMEOUT_SECONDS = 20.0


@dataclass(slots=True)
class Turn:
    utterance: str
    intent: str
    expects_answer: bool
    sequence: int
    difficulty: int
    reason: str


class MockInterviewService:
    def __init__(self, session: AsyncSession, *, gateway: AIGateway | None = None) -> None:
        self._session = session
        self._workspaces = WorkspaceService(session)
        self._gateway = gateway or build_gateway()

    # ── Lifecycle ────────────────────────────────────────────────────

    async def create(
        self,
        *,
        user_id: uuid.UUID,
        workspace_id: uuid.UUID,
        mode: str,
        persona: str,
        difficulty: int = 3,
        duration_minutes: int = 20,
        live_feedback: bool = False,
    ) -> MockSession:
        bundle = await self._workspaces.context(user_id=user_id, workspace_id=workspace_id)

        session = MockSession(
            user_id=user_id,
            workspace_id=workspace_id,
            mode=mode,
            persona=persona,
            difficulty=difficulty,
            planned_duration_seconds=duration_minutes * 60,
            live_feedback=live_feedback,
            context_version=bundle.version,
            interviewer_state=InterviewerState(difficulty=difficulty).to_json(),
        )
        self._session.add(session)
        await self._session.flush()
        log.info("mock_session_created", session_id=str(session.id), mode=mode)
        return session

    async def get(self, *, user_id: uuid.UUID, session_id: uuid.UUID) -> MockSession:
        session = (
            await self._session.execute(
                select(MockSession).where(
                    MockSession.id == session_id,
                    MockSession.user_id == user_id,
                    MockSession.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if session is None:
            raise AppError.not_found("Session")
        return session

    # ── Conversation ─────────────────────────────────────────────────

    async def next_turn(self, *, user_id: uuid.UUID, session_id: uuid.UUID) -> Turn:
        session = await self.get(user_id=user_id, session_id=session_id)
        if session.status == SessionStatus.COMPLETED:
            raise AppError(ErrorCode.SESSION_NOT_ACTIVE, "This interview has already finished.")

        if session.status == SessionStatus.CREATED:
            session.status = SessionStatus.ACTIVE
            session.started_at = dt.datetime.now(dt.UTC)

        state = InterviewerState.from_json(session.interviewer_state)
        bundle = await self._workspaces.context(user_id=user_id, workspace_id=session.workspace_id)

        last_attempt = await self._last_attempt(session.id)
        elapsed = self._elapsed_seconds(session)

        decision = decide_next_turn(
            state=state,
            persona=session.persona,
            last_scores=last_attempt.scores if last_attempt else None,
            elapsed_seconds=elapsed,
            planned_seconds=session.planned_duration_seconds,
            max_questions=MAX_QUESTIONS_PER_SESSION,
        )

        if decision.intent is TurnIntent.WRAPUP:
            utterance = (
                "That's everything I wanted to cover. Thanks for your time — "
                "do you have questions for me?"
            )
        else:
            utterance = await self._generate_question(
                session=session,
                bundle=bundle,
                state=state,
                decision=decision,
                last_answer=(last_attempt.answer_text or "") if last_attempt else "",
            )

        state = apply_turn(state, decision, utterance)
        session.interviewer_state = state.to_json()

        sequence = state.question_count
        await self._append_transcript(session, SpeakerRole.INTERVIEWER, utterance)

        if decision.intent is not TurnIntent.WRAPUP:
            attempt = QuestionAttempt(
                user_id=user_id,
                session_id=session.id,
                session_kind="mock",
                workspace_id=session.workspace_id,
                sequence=await self._next_attempt_sequence(session.id),
                question_text=utterance,
                intent=str(decision.intent),
                category=session.mode,
                prompt_ref=registry.get("mock.interviewer.turn").ref,
            )
            self._session.add(attempt)

        await self._session.flush()
        return Turn(
            utterance=utterance,
            intent=str(decision.intent),
            expects_answer=decision.intent is not TurnIntent.WRAPUP,
            sequence=sequence,
            difficulty=decision.difficulty,
            reason=decision.reason,
        )

    async def _generate_question(
        self,
        *,
        session: MockSession,
        bundle: Any,
        state: InterviewerState,
        decision: Any,
        last_answer: str,
    ) -> str:
        prompt = registry.get("mock.interviewer.turn")
        themes = (
            ", ".join(str(t.get("value", "")) for t in bundle.opportunity.likely_themes[:5])
            or "general fit"
        )

        for attempt in range(2):
            result = await self._gateway.generate(
                task_class=TaskClass.MOCK_INTERVIEWER_TURN,
                messages=prompt.render(
                    role=bundle.opportunity.role_title,
                    company=bundle.opportunity.company_name,
                    stage=bundle.opportunity.stage,
                    persona=session.persona,
                    difficulty=decision.difficulty,
                    themes=themes,
                    asked="\n".join(f"- {q}" for q in state.asked[-8:]) or "(none yet)",
                    last_answer=last_answer or "(no answer yet)",
                ),
                json_schema=prompt.output_schema,
                max_output_tokens=300,
                temperature=0.6 + 0.1 * attempt,
                timeout_seconds=TURN_TIMEOUT_SECONDS,
                user_id=session.user_id,
                feature="mock_interview",
            )
            utterance = str(result.content.get("utterance", "")).strip()
            if utterance and not is_repeat(utterance, state.asked):
                return utterance
            log.info("interviewer_question_rejected", reason="repeat_or_empty", attempt=attempt)

        # Both attempts collided with something already asked; move on rather
        # than repeat (FR-MOCK-004).
        return "Let's move to a different area. What work are you proudest of in this role?"

    async def submit_answer(
        self, *, user_id: uuid.UUID, session_id: uuid.UUID, answer: str, duration_seconds: int
    ) -> dict[str, Any]:
        session = await self.get(user_id=user_id, session_id=session_id)
        attempt = await self._last_attempt(session.id)
        if attempt is None:
            raise AppError(ErrorCode.INVALID_STATE_TRANSITION, "There's no question to answer yet.")

        attempt.answer_text = answer
        attempt.duration_seconds = duration_seconds

        metrics = delivery.analyze(answer, duration_seconds)
        attempt.delivery = metrics.to_json()

        prompt = registry.get("mock.answer.rubric")
        result = await self._gateway.generate(
            task_class=TaskClass.RUBRIC_EVALUATE_ANSWER,
            messages=prompt.render(question=attempt.question_text, answer=answer),
            json_schema=prompt.output_schema,
            max_output_tokens=400,
            user_id=user_id,
            feature="mock_interview",
        )
        attempt.scores = dict(result.content)

        state = record_answer_score(
            InterviewerState.from_json(session.interviewer_state), attempt.scores
        )
        session.interviewer_state = state.to_json()

        await self._append_transcript(session, SpeakerRole.CANDIDATE, answer)
        await self._session.flush()

        # Live feedback is withheld unless explicitly enabled (FR-MOCK-007).
        return attempt.scores if session.live_feedback else {}

    # ── Completion and report ────────────────────────────────────────

    async def end(
        self, *, user_id: uuid.UUID, session_id: uuid.UUID, reason: str = "completed"
    ) -> SessionFeedback:
        session = await self.get(user_id=user_id, session_id=session_id)
        if session.status != SessionStatus.COMPLETED:
            session.status = SessionStatus.COMPLETED
            session.ended_at = dt.datetime.now(dt.UTC)
            session.duration_seconds = self._elapsed_seconds(session)
            session.end_reason = reason
            await self._session.flush()

        return await self.generate_report(user_id=user_id, session_id=session_id)

    async def generate_report(
        self, *, user_id: uuid.UUID, session_id: uuid.UUID
    ) -> SessionFeedback:
        """PRD §11.4: the report is produced automatically, not on request."""
        session = await self.get(user_id=user_id, session_id=session_id)
        existing = (
            await self._session.execute(
                select(SessionFeedback).where(
                    SessionFeedback.session_id == session_id,
                    SessionFeedback.session_kind == "mock",
                )
            )
        ).scalar_one_or_none()
        if existing is not None and existing.status == ReportStatus.READY:
            return existing

        attempts = list(
            (
                await self._session.execute(
                    select(QuestionAttempt)
                    .where(QuestionAttempt.session_id == session_id)
                    .order_by(QuestionAttempt.sequence)
                )
            ).scalars()
        )
        answered = [a for a in attempts if a.answer_text]

        report = existing or SessionFeedback(
            user_id=user_id,
            session_id=session_id,
            session_kind="mock",
            workspace_id=session.workspace_id,
            rubric_version=RUBRIC_VERSION,
        )

        dimension_scores = _average_dimensions(answered)
        report.dimension_scores = dimension_scores
        report.overall_score = (
            round(sum(dimension_scores.values()) / len(dimension_scores), 2)
            if dimension_scores
            else None
        )
        report.delivery_metrics = delivery.aggregate(
            [delivery.analyze(a.answer_text or "", a.duration_seconds) for a in answered]
        )
        report.per_question = [
            {
                "sequence": a.sequence,
                "question": a.question_text,
                "intent": a.intent,
                "scores": a.scores,
                "delivery": a.delivery,
            }
            for a in attempts
        ]

        if not answered:
            # A session with no answers has nothing to assess; say so rather
            # than manufacture a score.
            report.status = ReportStatus.PARTIAL
            report.summary = "No answers were recorded, so there's nothing to assess yet."
            report.strengths, report.weaknesses, report.recommendations = [], [], []
            self._session.add(report)
            await self._session.flush()
            return report

        await self._synthesize(report, session, answered)
        self._session.add(report)
        await self._session.flush()

        log.info(
            "mock_report_generated",
            session_id=str(session_id),
            answered=len(answered),
            overall=report.overall_score,
        )
        return report

    async def _synthesize(
        self, report: SessionFeedback, session: MockSession, answered: list[QuestionAttempt]
    ) -> None:
        bundle = await self._workspaces.context(
            user_id=session.user_id, workspace_id=session.workspace_id
        )
        prompt = registry.get("mock.session.report")
        transcript = "\n\n".join(
            f"Q{a.sequence}: {a.question_text}\nA: {a.answer_text}" for a in answered
        )
        scores = "\n".join(f"Q{a.sequence}: {a.scores}" for a in answered)

        try:
            result = await self._gateway.generate(
                task_class=TaskClass.SESSION_REPORT,
                messages=prompt.render(
                    role=bundle.opportunity.role_title,
                    company=bundle.opportunity.company_name,
                    transcript=transcript[:20_000],
                    scores=scores[:4_000],
                ),
                json_schema=prompt.output_schema,
                max_output_tokens=1200,
                user_id=session.user_id,
                feature="mock_report",
            )
        except AppError as exc:
            # A failed synthesis must not lose the measured half of the report.
            report.status = ReportStatus.PARTIAL
            report.summary = "Scores and delivery metrics are ready; the written summary failed."
            log.warning("report_synthesis_failed", code=str(exc.code))
            return

        content = result.content
        report.summary = str(content.get("summary", ""))
        report.strengths = list(content.get("strengths", []))
        report.weaknesses = _normalize_weaknesses(content.get("weaknesses", []), answered)
        report.recommendations = list(content.get("recommendations", []))
        report.prompt_ref = prompt.ref
        report.model = result.model
        report.status = ReportStatus.READY

    # ── Helpers ──────────────────────────────────────────────────────

    async def _append_transcript(
        self, session: MockSession, role: str, content: str
    ) -> TranscriptSegment:
        seq = (
            await self._session.execute(
                select(func.coalesce(func.max(TranscriptSegment.seq), 0) + 1).where(
                    TranscriptSegment.session_id == session.id
                )
            )
        ).scalar_one()
        segment = TranscriptSegment(
            user_id=session.user_id,
            session_id=session.id,
            session_kind="mock",
            seq=int(seq),
            speaker_role=role,
            content=content,
        )
        self._session.add(segment)
        return segment

    async def _next_attempt_sequence(self, session_id: uuid.UUID) -> int:
        value = (
            await self._session.execute(
                select(func.coalesce(func.max(QuestionAttempt.sequence), 0) + 1).where(
                    QuestionAttempt.session_id == session_id
                )
            )
        ).scalar_one()
        return int(value)

    async def _last_attempt(self, session_id: uuid.UUID) -> QuestionAttempt | None:
        return (
            await self._session.execute(
                select(QuestionAttempt)
                .where(QuestionAttempt.session_id == session_id)
                .order_by(QuestionAttempt.sequence.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    @staticmethod
    def _elapsed_seconds(session: MockSession) -> int:
        if session.started_at is None:
            return 0
        return int((dt.datetime.now(dt.UTC) - session.started_at).total_seconds())


def _average_dimensions(attempts: list[QuestionAttempt]) -> dict[str, float]:
    totals: dict[str, list[float]] = {}
    for attempt in attempts:
        for key, value in (attempt.scores or {}).items():
            if isinstance(value, (int, float)):
                totals.setdefault(key, []).append(float(value))
    return {k: round(sum(v) / len(v), 2) for k, v in totals.items() if v}


def _normalize_weaknesses(raw: list[Any], attempts: list[QuestionAttempt]) -> list[dict[str, Any]]:
    """Shape weaknesses so the preparation engine can consume them directly.

    FR-MOCK-022 requires structured `{dimension, theme, severity, evidence}` —
    a prose paragraph cannot be turned into a ranked task.
    """
    normalized: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        normalized.append(
            {
                "dimension": str(item.get("dimension", "general")),
                "theme": str(item.get("theme", "")),
                "severity": min(1.0, max(0.0, float(item.get("severity", 0.5)))),
                "evidence": str(item.get("evidence", "")),
                "attempt_count": len(attempts),
            }
        )
    return normalized
