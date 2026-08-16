"""Live session engine (PRD §21, §26.6, AC-RT-010).

The guarantee under test is NFR-RT-010: no acknowledged transcript segment is
lost across a reconnect. Everything else in the realtime plane can degrade
gracefully; losing a candidate's transcript mid-interview cannot.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from verity.modules.identity.models import User
from verity.modules.sessions.live_engine import LiveSessionEngine
from verity.modules.sessions.live_models import (
    DetectedQuestion,
    LiveSession,
    LiveSessionStatus,
    QuestionLifecycle,
    SessionEvent,
)
from verity.modules.sessions.models import TranscriptSegment
from verity.modules.workspace.service import WorkspaceService
from verity.platform.config import settings
from verity.realtime import protocol
from verity.realtime.stt import (
    SILENCE_RMS_THRESHOLD,
    ChannelActivity,
    SttRouter,
    TextPassthroughSTT,
    frame_rms,
)

pytestmark = pytest.mark.integration

JD = """
Senior Backend Engineer

Requirements
- 5+ years of backend engineering experience
- Experience operating Kubernetes in production
"""


async def _engine(db: AsyncSession, user: User) -> LiveSessionEngine:
    workspace, _ = await WorkspaceService(db).create(
        user_id=user.id,
        company_name="Northwind Systems",
        role_title="Senior Backend Engineer",
        jd_text=JD,
    )
    session = LiveSession(
        user_id=user.id,
        workspace_id=workspace.id,
        interview_type="general",
    )
    db.add(session)
    await db.flush()
    return LiveSessionEngine(db, session)


# ── Event sourcing and replay (AC-RT-010) ────────────────────────────


async def test_every_emitted_event_is_persisted_before_delivery(
    db: AsyncSession, user: User
) -> None:
    engine = await _engine(db, user)
    await engine.start(resume_from_seq=None)

    await engine.ingest_utterance(
        channel="system", content="Tell me about yourself.", start_ms=0, end_ms=1500
    )

    stored = (
        (
            await db.execute(
                select(SessionEvent)
                .where(SessionEvent.session_id == engine.session.id)
                .order_by(SessionEvent.seq)
            )
        )
        .scalars()
        .all()
    )

    assert stored, "events must be durable before they reach the client"
    assert [e.seq for e in stored] == list(range(1, len(stored) + 1))


async def test_sequence_numbers_are_monotonic_across_utterances(
    db: AsyncSession, user: User
) -> None:
    engine = await _engine(db, user)
    await engine.start(resume_from_seq=None)

    seqs: list[int] = []
    for i in range(5):
        events = await engine.ingest_utterance(
            channel="system",
            content=f"Question number {i}?",
            start_ms=i * 2000,
            end_ms=i * 2000 + 1500,
        )
        seqs.extend(e.seq for e in events)

    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs), "sequence numbers must be unique"


async def test_reconnect_replays_exactly_what_was_missed(db: AsyncSession, user: User) -> None:
    """AC-RT-010: the client resumes from its last sequence number."""
    engine = await _engine(db, user)
    await engine.start(resume_from_seq=None)

    await engine.ingest_utterance(
        channel="system", content="First question here?", start_ms=0, end_ms=1500
    )
    acknowledged_seq = engine.session.last_event_seq

    # Client is offline for these.
    await engine.ingest_utterance(
        channel="mic", content="My answer to the first one.", start_ms=2000, end_ms=6000
    )
    await engine.ingest_utterance(
        channel="system", content="And what happened next?", start_ms=7000, end_ms=9000
    )

    replayed = await engine.replay_from(acknowledged_seq)

    assert replayed, "the client must receive what it missed"
    assert all(e.seq > acknowledged_seq for e in replayed)
    assert [e.seq for e in replayed] == sorted(e.seq for e in replayed)

    contents = [e.payload.get("content", "") for e in replayed]
    assert any("My answer to the first one." in c for c in contents)
    assert any("And what happened next?" in c for c in contents)


async def test_no_acknowledged_transcript_is_lost_across_a_reconnect(
    db: AsyncSession, user: User
) -> None:
    """The core guarantee, checked against persisted transcript rows."""
    engine = await _engine(db, user)
    await engine.start(resume_from_seq=None)

    spoken = [
        ("system", "Tell me about a production incident."),
        ("mic", "We had a payments outage last quarter."),
        ("system", "What did you do first?"),
        ("mic", "I paged the on-call and rolled back the deploy."),
    ]
    for index, (channel, content) in enumerate(spoken):
        await engine.ingest_utterance(
            channel=channel, content=content, start_ms=index * 5000, end_ms=index * 5000 + 4000
        )

    # Simulate the drop: force the pending batch to disk, as the disconnect
    # handler does.
    await engine.flush_transcript(force=True)

    # Reconnect with a brand-new engine over the same session row.
    resumed = LiveSessionEngine(db, engine.session)
    await resumed.start(resume_from_seq=0)

    stored = (
        (
            await db.execute(
                select(TranscriptSegment)
                .where(TranscriptSegment.session_id == engine.session.id)
                .order_by(TranscriptSegment.seq)
            )
        )
        .scalars()
        .all()
    )

    assert [s.content for s in stored] == [content for _, content in spoken]
    assert [s.seq for s in stored] == list(range(len(spoken)))


async def test_resuming_restores_conversation_memory(db: AsyncSession, user: User) -> None:
    engine = await _engine(db, user)
    await engine.start(resume_from_seq=None)
    await engine.ingest_utterance(
        channel="system", content="Tell me about your last role.", start_ms=0, end_ms=2000
    )
    await engine.save_memory()

    resumed = LiveSessionEngine(db, engine.session)
    await resumed.start(resume_from_seq=engine.session.last_event_seq)

    assert resumed.state.memory.last_question() is not None
    assert resumed.state.memory.verbatim


async def test_an_ended_session_cannot_be_resumed(db: AsyncSession, user: User) -> None:
    engine = await _engine(db, user)
    await engine.start(resume_from_seq=None)
    await engine.end()

    from verity.platform.errors import AppError

    resumed = LiveSessionEngine(db, engine.session)
    with pytest.raises(AppError) as exc:
        await resumed.start(resume_from_seq=0)
    assert exc.value.code == "session_not_active"


# ── Detection through the engine ─────────────────────────────────────


async def test_a_finalized_question_is_recorded_and_emitted(db: AsyncSession, user: User) -> None:
    engine = await _engine(db, user)
    await engine.start(resume_from_seq=None)

    events = await engine.ingest_utterance(
        channel="system",
        content="Tell me about a time you led an incident response.",
        start_ms=0,
        end_ms=4000,
    )

    types = [e.type for e in events]
    assert str(protocol.ServerEvent.QUESTION_FINALIZED) in types

    question = (
        await db.execute(
            select(DetectedQuestion).where(DetectedQuestion.session_id == engine.session.id)
        )
    ).scalar_one()

    assert question.lifecycle_state == QuestionLifecycle.FINALIZED
    assert question.finalized_at_ms is not None
    assert question.signals, "signals must be stored so a detection is diagnosable"


async def test_small_talk_does_not_create_a_question(db: AsyncSession, user: User) -> None:
    """The detector runs, but nothing worth answering is recorded."""
    engine = await _engine(db, user)
    await engine.start(resume_from_seq=None)

    await engine.ingest_utterance(
        channel="system", content="Hi, how are you today?", start_ms=0, end_ms=2000
    )

    questions = (
        (
            await db.execute(
                select(DetectedQuestion).where(DetectedQuestion.session_id == engine.session.id)
            )
        )
        .scalars()
        .all()
    )

    assert all(not q.category or q.category != "question" for q in questions)


async def test_candidate_speech_is_attributed_to_the_candidate(
    db: AsyncSession, user: User
) -> None:
    engine = await _engine(db, user)
    await engine.start(resume_from_seq=None)

    await engine.ingest_utterance(
        channel="mic", content="I led that migration end to end.", start_ms=0, end_ms=3000
    )
    await engine.flush_transcript(force=True)

    segment = (
        await db.execute(
            select(TranscriptSegment).where(TranscriptSegment.session_id == engine.session.id)
        )
    ).scalar_one()
    assert segment.speaker_role == "candidate"


async def test_manual_questions_bypass_detection(db: AsyncSession, user: User) -> None:
    """FR-RT-007: always available, full confidence, no signals required."""
    engine = await _engine(db, user)
    await engine.start(resume_from_seq=None)

    question, envelope = await engine.manual_question("Why do you want this role?")

    assert question.trigger == "manual"
    assert float(question.detection_confidence) == 1.0
    assert question.lifecycle_state == QuestionLifecycle.FINALIZED
    assert envelope.type == str(protocol.ServerEvent.QUESTION_FINALIZED)


# ── Metering (PRD §33) ───────────────────────────────────────────────


async def test_generation_is_capped_per_session(db: AsyncSession, user: User) -> None:
    engine = await _engine(db, user)
    engine.session.generation_count = settings.session_max_generations

    allowed, reason = engine.can_generate()
    assert allowed is False
    assert reason is not None and "cap" in reason


async def test_generation_is_rate_limited_per_minute(db: AsyncSession, user: User) -> None:
    """A pathological session must not consume unbounded spend."""
    engine = await _engine(db, user)
    for _ in range(settings.session_max_generations_per_minute):
        engine.record_generation()

    allowed, reason = engine.can_generate()
    assert allowed is False
    assert reason == "generation rate limit"


async def test_ending_a_session_records_duration_and_flushes(db: AsyncSession, user: User) -> None:
    engine = await _engine(db, user)
    await engine.start(resume_from_seq=None)
    await engine.ingest_utterance(
        channel="system", content="A question to persist?", start_ms=0, end_ms=2000
    )

    await engine.end(reason="completed")

    assert engine.session.status == LiveSessionStatus.ENDED
    assert engine.session.duration_seconds is not None

    stored = (
        (
            await db.execute(
                select(TranscriptSegment).where(TranscriptSegment.session_id == engine.session.id)
            )
        )
        .scalars()
        .all()
    )
    assert stored, "ending must flush the pending transcript batch"


# ── Voice activity detection ─────────────────────────────────────────


def test_rms_separates_speech_from_silence() -> None:
    silence = (0).to_bytes(2, "little", signed=True) * 320
    speech = (6000).to_bytes(2, "little", signed=True) * 320

    assert frame_rms(silence) < SILENCE_RMS_THRESHOLD
    assert frame_rms(speech) > SILENCE_RMS_THRESHOLD


def test_hangover_prevents_a_breath_becoming_a_boundary() -> None:
    activity = ChannelActivity()
    activity.update(rms=5000, timestamp_ms=0)
    assert activity.is_active

    activity.update(rms=10, timestamp_ms=100)
    assert activity.is_active, "a short pause must not end the turn"

    activity.update(rms=10, timestamp_ms=500)
    assert not activity.is_active


def test_silence_duration_is_measured_from_its_start() -> None:
    activity = ChannelActivity()
    activity.update(rms=5000, timestamp_ms=0)
    activity.update(rms=10, timestamp_ms=1000)

    assert activity.silence_ms(1700) == 700


# ── STT routing and failover (PRD §20.3, §34.1) ──────────────────────


async def test_router_falls_back_to_manual_mode_when_all_providers_fail() -> None:
    class AlwaysFails:
        name = "failing"

        def capabilities(self) -> object:
            return TextPassthroughSTT().capabilities()

        async def transcribe(self, channel: str, pcm: bytes, timestamp_ms: int) -> list[object]:
            from verity.platform.errors import AppError, ErrorCode

            raise AppError(ErrorCode.STT_UNAVAILABLE, "down")

    router = SttRouter([AlwaysFails()])  # type: ignore[list-item]

    for _ in range(3):
        await router.transcribe("system", b"\x00" * 640, 0)

    assert router.manual_mode is True


async def test_router_advances_to_the_next_provider_before_giving_up() -> None:
    class AlwaysFails:
        name = "failing"

        def capabilities(self) -> object:
            return TextPassthroughSTT().capabilities()

        async def transcribe(self, channel: str, pcm: bytes, timestamp_ms: int) -> list[object]:
            from verity.platform.errors import AppError, ErrorCode

            raise AppError(ErrorCode.STT_UNAVAILABLE, "down")

    router = SttRouter([AlwaysFails(), TextPassthroughSTT()])  # type: ignore[list-item]

    for _ in range(3):
        await router.transcribe("system", b"\x00" * 640, 0)

    assert router.active.name == "text-passthrough"
    assert router.manual_mode is False
