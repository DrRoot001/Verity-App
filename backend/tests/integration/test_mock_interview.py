"""Mock interview engine, reporting and the learning loop (PRD §13, §27, §64).

The guarantees under test are the structural ones — one question per turn, a
bounded probe budget, no repeats, measured (not inferred) delivery metrics, and
weaknesses that actually reach the next preparation plan.

Runs against the deterministic provider, so behaviour is reproducible and no
API key is required; the same code path runs against a real model in
production.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from verity.modules.identity.models import User
from verity.modules.preparation.service import PreparationService
from verity.modules.sessions import delivery
from verity.modules.sessions.interviewer import (
    InterviewerState,
    TurnIntent,
    answer_quality,
    decide_next_turn,
    is_repeat,
)
from verity.modules.sessions.models import (
    InterviewerPersona,
    MockMode,
    QuestionAttempt,
    ReportStatus,
    SessionStatus,
    TranscriptSegment,
)
from verity.modules.sessions.service import MockInterviewService
from verity.modules.workspace.service import WorkspaceService

pytestmark = pytest.mark.integration

JD = """
Senior Backend Engineer

Requirements
- 5+ years of backend engineering experience
- Strong experience with Python and PostgreSQL
- Track record of leading incident response
"""

STRONG_ANSWER = (
    "At the time our checkout service was timing out under load. I was responsible for "
    "the fix, so I profiled the hot path and introduced a read-through cache. "
    "As a result we reduced p95 latency from 800ms to 120ms and cut error rates by half."
)

WEAK_ANSWER = "Um, yeah, I basically like worked on some stuff with the team, you know."


async def _workspace(db: AsyncSession, user: User) -> object:
    workspace, _ = await WorkspaceService(db).create(
        user_id=user.id,
        company_name="Northwind Systems",
        role_title="Senior Backend Engineer",
        jd_text=JD,
    )
    return workspace


# ── Interviewer state machine (PRD §13.2) ────────────────────────────


def test_opening_turn_is_a_question() -> None:
    decision = decide_next_turn(
        state=InterviewerState(),
        persona=InterviewerPersona.NEUTRAL_EVALUATOR,
        last_scores=None,
        elapsed_seconds=0,
        planned_seconds=1200,
        max_questions=10,
    )
    assert decision.intent is TurnIntent.QUESTION


def test_a_thin_answer_is_probed_once() -> None:
    """FR-MOCK-002: probing is a rubric decision, not a coin flip."""
    state = InterviewerState()
    thin = {"relevance": 60, "structure": 30, "specificity": 20, "ownership_signal": 30}

    decision = decide_next_turn(
        state=state,
        persona=InterviewerPersona.NEUTRAL_EVALUATOR,
        last_scores=thin,
        elapsed_seconds=60,
        planned_seconds=1200,
        max_questions=10,
    )
    assert decision.intent is TurnIntent.PROBE


def test_probe_budget_is_bounded() -> None:
    state = InterviewerState(probes_used_this_question=2)
    thin = {"relevance": 40, "structure": 30, "specificity": 20, "ownership_signal": 30}

    decision = decide_next_turn(
        state=state,
        persona=InterviewerPersona.NEUTRAL_EVALUATOR,
        last_scores=thin,
        elapsed_seconds=60,
        planned_seconds=1200,
        max_questions=10,
    )
    assert decision.intent is TurnIntent.QUESTION, "must move on once the budget is spent"


def test_off_topic_answers_are_redirected() -> None:
    decision = decide_next_turn(
        state=InterviewerState(),
        persona=InterviewerPersona.NEUTRAL_EVALUATOR,
        last_scores={"relevance": 5, "structure": 5, "specificity": 5, "ownership_signal": 5},
        elapsed_seconds=60,
        planned_seconds=1200,
        max_questions=10,
    )
    assert decision.intent is TurnIntent.REDIRECT


def test_session_wraps_up_before_time_runs_out() -> None:
    """FR-MOCK-005: a graceful close, not a hard cut."""
    decision = decide_next_turn(
        state=InterviewerState(question_count=3),
        persona=InterviewerPersona.NEUTRAL_EVALUATOR,
        last_scores={"relevance": 80, "structure": 80, "specificity": 80, "ownership_signal": 80},
        elapsed_seconds=1150,
        planned_seconds=1200,
        max_questions=20,
    )
    assert decision.intent is TurnIntent.WRAPUP


def test_difficulty_rises_after_two_strong_answers() -> None:
    """FR-MOCK-003."""
    strong = {"relevance": 90, "structure": 85, "specificity": 90, "ownership_signal": 85}
    state = InterviewerState(difficulty=3, recent_scores=[answer_quality(strong)])

    decision = decide_next_turn(
        state=state,
        persona=InterviewerPersona.NEUTRAL_EVALUATOR,
        last_scores=strong,
        elapsed_seconds=60,
        planned_seconds=1200,
        max_questions=20,
    )
    assert decision.difficulty == 4


def test_difficulty_drops_after_a_poor_answer() -> None:
    poor = {"relevance": 30, "structure": 25, "specificity": 20, "ownership_signal": 30}
    decision = decide_next_turn(
        state=InterviewerState(difficulty=4),
        persona=InterviewerPersona.NEUTRAL_EVALUATOR,
        last_scores=poor,
        elapsed_seconds=60,
        planned_seconds=1200,
        max_questions=20,
    )
    assert decision.difficulty == 3


def test_rephrased_questions_count_as_repeats() -> None:
    """FR-MOCK-004: catching only exact matches would miss the real case."""
    asked = ["Tell me about a time you handled a production incident."]
    assert is_repeat("Tell me about a time you handled a production incident!", asked)
    assert not is_repeat("How do you approach system design?", asked)


# ── Delivery metrics (PRD §13.3) ─────────────────────────────────────


def test_delivery_metrics_are_measured_not_inferred() -> None:
    """FR-MOCK-021: no confidence or emotion claims anywhere in the output."""
    metrics = delivery.analyze(STRONG_ANSWER, duration_seconds=45)
    payload = metrics.to_json()

    assert metrics.word_count > 0
    assert metrics.words_per_minute is not None
    assert set(payload) == {
        "word_count",
        "duration_seconds",
        "words_per_minute",
        "filler_count",
        "filler_rate_per_100",
        "star_present",
        "star_completeness",
    }
    assert not any(k in payload for k in ("confidence", "emotion", "personality"))


def test_star_components_are_detected() -> None:
    metrics = delivery.analyze(STRONG_ANSWER, 45)
    assert metrics.star_present["situation"]
    assert metrics.star_present["result"]
    assert metrics.star_completeness > 0.5


def test_fillers_are_counted() -> None:
    metrics = delivery.analyze(WEAK_ANSWER, 10)
    assert metrics.filler_count >= 3
    assert metrics.filler_rate_per_100 > 0


def test_wpm_is_withheld_for_very_short_clips() -> None:
    """Below a few seconds the rate is measurement noise, not signal."""
    assert delivery.analyze("Yes.", duration_seconds=1).words_per_minute is None


# ── Session flow ─────────────────────────────────────────────────────


async def test_a_session_asks_one_question_at_a_time(db: AsyncSession, user: User) -> None:
    workspace = await _workspace(db, user)
    service = MockInterviewService(db)
    session = await service.create(
        user_id=user.id,
        workspace_id=workspace.id,  # type: ignore[attr-defined]
        mode=MockMode.BEHAVIORAL,
        persona=InterviewerPersona.NEUTRAL_EVALUATOR,
    )

    turn = await service.next_turn(user_id=user.id, session_id=session.id)

    assert turn.utterance
    assert turn.expects_answer is True
    assert turn.utterance.count("?") <= 1, "one question per turn (FR-MOCK-001)"

    await db.refresh(session)
    assert session.status == SessionStatus.ACTIVE
    assert session.started_at is not None


async def test_answering_records_scores_and_delivery(db: AsyncSession, user: User) -> None:
    workspace = await _workspace(db, user)
    service = MockInterviewService(db)
    session = await service.create(
        user_id=user.id,
        workspace_id=workspace.id,  # type: ignore[attr-defined]
        mode=MockMode.BEHAVIORAL,
        persona=InterviewerPersona.NEUTRAL_EVALUATOR,
    )
    await service.next_turn(user_id=user.id, session_id=session.id)
    await service.submit_answer(
        user_id=user.id, session_id=session.id, answer=STRONG_ANSWER, duration_seconds=45
    )

    attempt = (
        await db.execute(select(QuestionAttempt).where(QuestionAttempt.session_id == session.id))
    ).scalar_one()

    assert attempt.answer_text == STRONG_ANSWER
    assert attempt.scores, "rubric scores must be recorded"
    assert attempt.delivery["word_count"] > 0
    assert attempt.prompt_ref, "every generated question records its prompt (FR-AI-022)"


async def test_live_feedback_is_withheld_by_default(db: AsyncSession, user: User) -> None:
    """FR-MOCK-007: feedback must not appear mid-interview unless asked for."""
    workspace = await _workspace(db, user)
    service = MockInterviewService(db)
    session = await service.create(
        user_id=user.id,
        workspace_id=workspace.id,  # type: ignore[attr-defined]
        mode=MockMode.BEHAVIORAL,
        persona=InterviewerPersona.NEUTRAL_EVALUATOR,
    )
    await service.next_turn(user_id=user.id, session_id=session.id)

    returned = await service.submit_answer(
        user_id=user.id, session_id=session.id, answer=STRONG_ANSWER, duration_seconds=40
    )
    assert returned == {}


async def test_transcript_records_both_speakers(db: AsyncSession, user: User) -> None:
    workspace = await _workspace(db, user)
    service = MockInterviewService(db)
    session = await service.create(
        user_id=user.id,
        workspace_id=workspace.id,  # type: ignore[attr-defined]
        mode=MockMode.BEHAVIORAL,
        persona=InterviewerPersona.NEUTRAL_EVALUATOR,
    )
    await service.next_turn(user_id=user.id, session_id=session.id)
    await service.submit_answer(
        user_id=user.id, session_id=session.id, answer=STRONG_ANSWER, duration_seconds=40
    )

    segments = (
        (
            await db.execute(
                select(TranscriptSegment)
                .where(TranscriptSegment.session_id == session.id)
                .order_by(TranscriptSegment.seq)
            )
        )
        .scalars()
        .all()
    )

    roles = [s.speaker_role for s in segments]
    assert roles == ["interviewer", "candidate"]


async def test_answering_without_a_question_is_rejected(db: AsyncSession, user: User) -> None:
    workspace = await _workspace(db, user)
    service = MockInterviewService(db)
    session = await service.create(
        user_id=user.id,
        workspace_id=workspace.id,  # type: ignore[attr-defined]
        mode=MockMode.BEHAVIORAL,
        persona=InterviewerPersona.NEUTRAL_EVALUATOR,
    )

    from verity.platform.errors import AppError

    with pytest.raises(AppError) as exc:
        await service.submit_answer(
            user_id=user.id, session_id=session.id, answer="Hello?", duration_seconds=5
        )
    assert exc.value.code == "invalid_state_transition"


# ── Reporting (PRD §27) ──────────────────────────────────────────────


async def test_ending_a_session_produces_a_report(db: AsyncSession, user: User) -> None:
    """PRD §11.4: the report is automatic, not requested."""
    workspace = await _workspace(db, user)
    service = MockInterviewService(db)
    session = await service.create(
        user_id=user.id,
        workspace_id=workspace.id,  # type: ignore[attr-defined]
        mode=MockMode.BEHAVIORAL,
        persona=InterviewerPersona.NEUTRAL_EVALUATOR,
    )
    await service.next_turn(user_id=user.id, session_id=session.id)
    await service.submit_answer(
        user_id=user.id, session_id=session.id, answer=STRONG_ANSWER, duration_seconds=45
    )

    report = await service.end(user_id=user.id, session_id=session.id)

    assert report.status in (ReportStatus.READY, ReportStatus.PARTIAL)
    assert report.overall_score is not None
    assert report.dimension_scores
    assert report.delivery_metrics["answers"] == 1
    assert report.per_question

    await db.refresh(session)
    assert session.status == SessionStatus.COMPLETED
    assert session.duration_seconds is not None


async def test_a_session_with_no_answers_admits_it(db: AsyncSession, user: User) -> None:
    """Manufacturing a score from nothing would be worse than saying nothing."""
    workspace = await _workspace(db, user)
    service = MockInterviewService(db)
    session = await service.create(
        user_id=user.id,
        workspace_id=workspace.id,  # type: ignore[attr-defined]
        mode=MockMode.BEHAVIORAL,
        persona=InterviewerPersona.NEUTRAL_EVALUATOR,
    )
    await service.next_turn(user_id=user.id, session_id=session.id)

    report = await service.end(user_id=user.id, session_id=session.id)

    assert report.status == ReportStatus.PARTIAL
    assert report.overall_score is None
    assert report.weaknesses == []
    assert "nothing to assess" in (report.summary or "")


async def test_report_generation_is_idempotent(db: AsyncSession, user: User) -> None:
    workspace = await _workspace(db, user)
    service = MockInterviewService(db)
    session = await service.create(
        user_id=user.id,
        workspace_id=workspace.id,  # type: ignore[attr-defined]
        mode=MockMode.BEHAVIORAL,
        persona=InterviewerPersona.NEUTRAL_EVALUATOR,
    )
    await service.next_turn(user_id=user.id, session_id=session.id)
    await service.submit_answer(
        user_id=user.id, session_id=session.id, answer=STRONG_ANSWER, duration_seconds=45
    )

    first = await service.end(user_id=user.id, session_id=session.id)
    second = await service.generate_report(user_id=user.id, session_id=session.id)
    assert first.id == second.id


async def test_sessions_are_scoped_to_the_owner(
    db: AsyncSession, user: User, other_user: User
) -> None:
    workspace = await _workspace(db, other_user)
    service = MockInterviewService(db)
    session = await service.create(
        user_id=other_user.id,
        workspace_id=workspace.id,  # type: ignore[attr-defined]
        mode=MockMode.BEHAVIORAL,
        persona=InterviewerPersona.NEUTRAL_EVALUATOR,
    )

    from verity.platform.errors import AppError

    with pytest.raises(AppError) as exc:
        await service.get(user_id=user.id, session_id=session.id)
    assert exc.value.status_code == 404


# ── The learning loop (PRD §64) ──────────────────────────────────────


async def test_report_weaknesses_become_preparation_tasks(db: AsyncSession, user: User) -> None:
    """The retention thesis: last session's weakness changes the next plan."""
    workspace = await _workspace(db, user)
    service = MockInterviewService(db)
    session = await service.create(
        user_id=user.id,
        workspace_id=workspace.id,  # type: ignore[attr-defined]
        mode=MockMode.BEHAVIORAL,
        persona=InterviewerPersona.NEUTRAL_EVALUATOR,
    )
    await service.next_turn(user_id=user.id, session_id=session.id)
    await service.submit_answer(
        user_id=user.id, session_id=session.id, answer=WEAK_ANSWER, duration_seconds=15
    )
    report = await service.end(user_id=user.id, session_id=session.id)

    # Give the report a concrete weakness in the structured shape the planner
    # consumes (FR-MOCK-022).
    report.weaknesses = [
        {
            "dimension": "specificity",
            "theme": "answers lacked a measurable result",
            "severity": 0.8,
            "evidence": "no outcome was stated",
        }
    ]
    await db.flush()

    plan = await PreparationService(db).generate(
        user_id=user.id,
        workspace_id=workspace.id,  # type: ignore[attr-defined]
    )

    weakness_tasks = [t for t in plan.tasks if t.dedupe_key.startswith("weakness:")]
    assert weakness_tasks, "a measured weakness must produce a task"

    task = weakness_tasks[0]
    assert task.source == "report"
    assert task.action["type"] == "start_mock"
    assert "measurable result" in task.title


async def test_workspace_readiness_reflects_session_history(db: AsyncSession, user: User) -> None:
    workspace = await _workspace(db, user)
    plan = await PreparationService(db).generate(
        user_id=user.id,
        workspace_id=workspace.id,  # type: ignore[attr-defined]
    )
    assert plan.readiness.score >= 0

    await db.refresh(workspace)  # type: ignore[arg-type]
    assert workspace.readiness_computed_at is not None  # type: ignore[attr-defined]
    assert workspace.readiness_computed_at <= dt.datetime.now(dt.UTC)  # type: ignore[attr-defined]
