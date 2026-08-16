"""Live session engine (PRD §21, §26.6).

Lives in the sessions module rather than in ``verity/realtime`` because it
touches the database. ``verity/realtime`` stays pure infrastructure — protocol,
detector, memory, orchestrator, STT — so it can be unit-tested without Postgres
and reused by the desktop gateway unchanged.

Owns one session's state machine: sequence numbering, event persistence,
utterance processing, question lifecycle and metering.

The guarantee this file exists to keep is NFR-RT-010 — *no acknowledged
transcript segment is lost across a reconnect*. That is achieved by writing
every server→client event to ``session_events`` **before** it goes out, so a
reconnecting client can be replayed from its last sequence number. Emitting
first and persisting later would make the log lossy in exactly the crash that
matters.
"""

from __future__ import annotations

import datetime as dt
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from verity.modules.sessions.live_models import (
    DetectedQuestion,
    LiveSession,
    LiveSessionStatus,
    QuestionLifecycle,
    SessionEvent,
)
from verity.modules.sessions.models import SpeakerRole, TranscriptSegment
from verity.platform.cache import RedisRole, get_redis
from verity.platform.config import settings
from verity.platform.errors import AppError, ErrorCode
from verity.platform.logging import get_logger
from verity.platform.telemetry import stage_span
from verity.realtime import protocol
from verity.realtime.detector import Detection, detect, is_superseded_by
from verity.realtime.memory import ConversationMemory
from verity.realtime.stt import ChannelActivity

log = get_logger("realtime.engine")

#: Replay is capped so a client that was away for an hour does not receive an
#: hour of events; beyond this it restarts cleanly (PRD §26.6).
MAX_REPLAY_EVENTS = 500
#: Transcript rows are written in batches — a synchronous insert per utterance
#: would put the database in the latency-critical path (PRD §32.2).
TRANSCRIPT_FLUSH_SIZE = 20
TRANSCRIPT_FLUSH_MS = 2_000

MEMORY_TTL_SECONDS = 6 * 60 * 60


@dataclass(slots=True)
class PendingQuestion:
    """A question still accumulating speech (PRD §14.2)."""

    id: uuid.UUID
    content: str
    started_at_ms: int
    last_update_ms: int
    detection: Detection
    lifecycle: str = QuestionLifecycle.ACCUMULATING


@dataclass(slots=True)
class EngineState:
    memory: ConversationMemory = field(default_factory=ConversationMemory)
    activity: dict[str, ChannelActivity] = field(default_factory=dict)
    pending: PendingQuestion | None = None
    last_finalized: str | None = None
    generations_this_minute: list[float] = field(default_factory=list)

    def channel(self, name: str) -> ChannelActivity:
        return self.activity.setdefault(name, ChannelActivity())


class LiveSessionEngine:
    """One instance per connected session."""

    def __init__(self, db: AsyncSession, session: LiveSession) -> None:
        self._db = db
        self.session = session
        self.state = EngineState()
        self._pending_segments: list[TranscriptSegment] = []
        self._last_flush = time.monotonic()
        self._seq = session.last_event_seq

    # ── Event sourcing (PRD §26.6) ───────────────────────────────────

    async def emit(self, event_type: protocol.ServerEvent, payload: Any) -> protocol.Envelope:
        """Persist, then return the envelope for delivery.

        Order matters: persisting first is what makes replay complete after an
        ungraceful disconnect.
        """
        self._seq += 1
        envelope = protocol.build(self.session.id, event_type, payload, self._seq)

        self._db.add(
            SessionEvent(
                session_id=self.session.id,
                seq=self._seq,
                type=str(event_type),
                payload=envelope.payload,
            )
        )
        self.session.last_event_seq = self._seq
        return envelope

    async def replay_from(self, seq: int) -> list[protocol.Envelope]:
        """Events the client missed, oldest first (NFR-RT-010)."""
        rows = list(
            (
                await self._db.execute(
                    select(SessionEvent)
                    .where(SessionEvent.session_id == self.session.id, SessionEvent.seq > seq)
                    .order_by(SessionEvent.seq)
                    .limit(MAX_REPLAY_EVENTS)
                )
            ).scalars()
        )
        log.info(
            "session_replay",
            session_id=str(self.session.id),
            from_seq=seq,
            replayed=len(rows),
        )
        return [
            protocol.Envelope(
                session_id=str(self.session.id),
                seq=row.seq,
                type=row.type,
                payload=row.payload,
            )
            for row in rows
        ]

    # ── Utterance processing ─────────────────────────────────────────

    async def ingest_utterance(
        self,
        *,
        channel: str,
        content: str,
        start_ms: int,
        end_ms: int,
        is_final: bool = True,
        confidence: float | None = None,
    ) -> list[protocol.Envelope]:
        """Process one transcribed utterance into events."""
        events: list[protocol.Envelope] = []
        role = SpeakerRole.INTERVIEWER if channel == "system" else SpeakerRole.CANDIDATE

        events.append(
            await self.emit(
                protocol.ServerEvent.STT_FINAL if is_final else protocol.ServerEvent.STT_PARTIAL,
                protocol.TranscriptPayload(
                    channel=channel,
                    speaker_role=str(role),
                    content=content,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    confidence=confidence,
                ),
            )
        )

        if not is_final:
            return events

        self._queue_segment(role, content, start_ms, end_ms, confidence)
        self.state.memory.add_turn(str(role), content, end_ms)

        if role is SpeakerRole.CANDIDATE:
            # The candidate answering closes the question that prompted it.
            self.state.channel("mic").is_active = True
            if self.state.pending is not None:
                self.state.pending = None
            return events

        events.extend(await self._process_interviewer_utterance(content, start_ms, end_ms))
        return events

    async def _process_interviewer_utterance(
        self, content: str, start_ms: int, end_ms: int, *, endpointed: bool = True
    ) -> list[protocol.Envelope]:
        events: list[protocol.Envelope] = []

        with stage_span("question_detection"):
            candidate_active = self.state.channel("mic").is_active
            merged = content
            if self.state.pending is not None and (
                start_ms - self.state.pending.last_update_ms
                <= protocol_accumulation_window(content)
            ):
                # Continued speech extends the same question rather than
                # starting a new one (PRD §14.2 ACCUMULATING).
                merged = f"{self.state.pending.content} {content}".strip()

            result = detect(
                content=merged,
                silence_ms=self.state.channel("system").silence_ms(end_ms),
                candidate_channel_active=candidate_active,
                previous_question=self.state.memory.last_question(),
                endpointed=endpointed,
            )

        if not result.is_detected:
            return events

        if self.state.pending is None:
            question = DetectedQuestion(
                user_id=self.session.user_id,
                session_id=self.session.id,
                workspace_id=self.session.workspace_id,
                content=merged,
                detection_confidence=result.confidence,
                signals=result.signals.to_json(),
                lifecycle_state=QuestionLifecycle.DETECTED,
                detected_at_ms=start_ms,
            )
            self._db.add(question)
            await self._db.flush()
            self.state.pending = PendingQuestion(
                id=question.id,
                content=merged,
                started_at_ms=start_ms,
                last_update_ms=end_ms,
                detection=result,
            )
            events.append(
                await self.emit(
                    protocol.ServerEvent.QUESTION_DETECTED,
                    protocol.QuestionPayload(
                        detected_question_id=str(question.id),
                        content=merged,
                        confidence=result.confidence,
                        signals=result.signals.to_json(),
                    ),
                )
            )
        else:
            self.state.pending.content = merged
            self.state.pending.last_update_ms = end_ms
            self.state.pending.detection = result

        if result.is_finalized:
            events.extend(await self._finalize(result, end_ms))

        return events

    async def _finalize(self, result: Detection, at_ms: int) -> list[protocol.Envelope]:
        pending = self.state.pending
        if pending is None:
            return []

        events: list[protocol.Envelope] = []

        if self.state.last_finalized and is_superseded_by(
            self.state.last_finalized, pending.content
        ):
            events.append(
                await self.emit(
                    protocol.ServerEvent.QUESTION_SUPERSEDED,
                    {"detected_question_id": str(pending.id)},
                )
            )

        question = await self._db.get(DetectedQuestion, pending.id)
        if question is not None:
            question.content = pending.content
            question.lifecycle_state = QuestionLifecycle.FINALIZED
            question.finalized_at_ms = at_ms
            question.detection_confidence = result.confidence
            question.signals = result.signals.to_json()
            question.category = result.utterance_class
            question.sub_parts = result.sub_parts
            await self._db.flush()

        self.state.last_finalized = pending.content
        self.state.memory.record_question(
            str(pending.id), pending.content, result.utterance_class, at_ms
        )

        events.append(
            await self.emit(
                protocol.ServerEvent.QUESTION_FINALIZED,
                protocol.QuestionPayload(
                    detected_question_id=str(pending.id),
                    content=pending.content,
                    confidence=result.confidence,
                    sub_parts=result.sub_parts,
                    category=result.utterance_class,
                    signals=result.signals.to_json(),
                ),
            )
        )

        if not result.should_generate:
            # Detected and shown, but not worth a model call (FR-RT-006).
            log.info(
                "generation_skipped",
                reason=result.reason,
                utterance_class=result.utterance_class,
            )

        self.state.pending = None
        return events

    async def manual_question(self, content: str) -> tuple[DetectedQuestion, protocol.Envelope]:
        """FR-RT-007: bypasses detection entirely."""
        question = DetectedQuestion(
            user_id=self.session.user_id,
            session_id=self.session.id,
            workspace_id=self.session.workspace_id,
            content=content.strip(),
            detection_confidence=1.0,
            signals={"manual": 1.0},
            lifecycle_state=QuestionLifecycle.FINALIZED,
            detected_at_ms=0,
            trigger="manual",
        )
        self._db.add(question)
        await self._db.flush()

        self.state.memory.record_question(str(question.id), content, "manual", 0)
        envelope = await self.emit(
            protocol.ServerEvent.QUESTION_FINALIZED,
            protocol.QuestionPayload(
                detected_question_id=str(question.id),
                content=content.strip(),
                confidence=1.0,
                signals={"manual": 1.0},
            ),
        )
        return question, envelope

    # ── Metering (PRD §33) ───────────────────────────────────────────

    def can_generate(self) -> tuple[bool, str | None]:
        """Per-session and per-minute caps, checked before any model call."""
        if self.session.generation_count >= settings.session_max_generations:
            return False, "session generation cap reached"

        now = time.monotonic()
        self.state.generations_this_minute = [
            t for t in self.state.generations_this_minute if now - t < 60
        ]
        if len(self.state.generations_this_minute) >= settings.session_max_generations_per_minute:
            return False, "generation rate limit"
        return True, None

    def record_generation(self) -> None:
        self.session.generation_count += 1
        self.state.generations_this_minute.append(time.monotonic())

    def add_metered_seconds(self, seconds: int) -> None:
        self.session.metered_seconds += seconds

    # ── Transcript persistence ───────────────────────────────────────

    def _queue_segment(
        self, role: str, content: str, start_ms: int, end_ms: int, confidence: float | None
    ) -> None:
        self._pending_segments.append(
            TranscriptSegment(
                user_id=self.session.user_id,
                session_id=self.session.id,
                session_kind="live",
                seq=len(self._pending_segments),  # replaced at flush
                speaker_role=role,
                content=content,
                start_ms=start_ms,
                end_ms=end_ms,
                confidence=confidence,
            )
        )

    async def flush_transcript(self, *, force: bool = False) -> int:
        elapsed_ms = (time.monotonic() - self._last_flush) * 1000
        if not self._pending_segments:
            return 0
        if (
            not force
            and len(self._pending_segments) < TRANSCRIPT_FLUSH_SIZE
            and elapsed_ms < TRANSCRIPT_FLUSH_MS
        ):
            return 0

        base = (
            await self._db.execute(
                select(func.coalesce(func.max(TranscriptSegment.seq), -1) + 1).where(
                    TranscriptSegment.session_id == self.session.id
                )
            )
        ).scalar_one()

        for offset, segment in enumerate(self._pending_segments):
            segment.seq = int(base) + offset
            self._db.add(segment)

        written = len(self._pending_segments)
        self._pending_segments.clear()
        self._last_flush = time.monotonic()
        await self._db.flush()
        return written

    # ── Memory persistence ───────────────────────────────────────────

    async def save_memory(self) -> None:
        import json

        await get_redis(RedisRole.SESSION).set(
            f"rtmem:{self.session.id}",
            json.dumps(self.state.memory.to_json()),
            ex=MEMORY_TTL_SECONDS,
        )

    async def load_memory(self) -> None:
        import json

        raw = await get_redis(RedisRole.SESSION).get(f"rtmem:{self.session.id}")
        if raw:
            payload = raw.decode() if isinstance(raw, bytes) else raw
            self.state.memory = ConversationMemory.from_json(json.loads(payload))

    # ── Lifecycle ────────────────────────────────────────────────────

    async def start(self, resume_from_seq: int | None) -> list[protocol.Envelope]:
        if self.session.status in (LiveSessionStatus.ENDED, LiveSessionStatus.FAILED):
            raise AppError(ErrorCode.SESSION_NOT_ACTIVE, "This session has ended.")

        replay: list[protocol.Envelope] = []
        if resume_from_seq is not None:
            await self.load_memory()
            replay = await self.replay_from(resume_from_seq)
            self.session.status = LiveSessionStatus.ACTIVE
        else:
            self.session.status = LiveSessionStatus.ACTIVE
            self.session.started_at = self.session.started_at or dt.datetime.now(dt.UTC)

        await self._db.flush()
        return replay

    async def end(self, reason: str = "completed") -> None:
        await self.flush_transcript(force=True)
        await self.save_memory()

        self.session.status = LiveSessionStatus.ENDED
        self.session.ended_at = dt.datetime.now(dt.UTC)
        if self.session.started_at is not None:
            self.session.duration_seconds = int(
                (self.session.ended_at - self.session.started_at).total_seconds()
            )
        await self._db.flush()
        log.info(
            "live_session_ended",
            session_id=str(self.session.id),
            reason=reason,
            duration=self.session.duration_seconds,
            generations=self.session.generation_count,
        )


def protocol_accumulation_window(content: str) -> int:
    from verity.realtime.detector import accumulation_window_ms

    words = len(content.split())
    # Approximate speech rate from utterance length; the detector uses it to
    # decide how long to keep accumulating.
    return accumulation_window_ms(None if words < 4 else float(words * 12))
