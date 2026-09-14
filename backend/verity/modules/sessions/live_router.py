"""Live session HTTP + WebSocket transport (PRD §25.2, §26).

The socket is authenticated by a **single-use, 60-second, session-bound ticket**
rather than by an access token in the query string (PRD §26.1): query strings
land in proxy logs and browser history, so a long-lived credential must never
travel that way.

The connection holds one ``LiveSessionEngine`` for its lifetime — that is where
per-connection state lives (pending question, channel activity) — but rebinds a
fresh database session per inbound message. Holding one transaction open for a
45-minute interview would pin a connection and turn any slow query into a
session-wide stall.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from verity.modules.identity.dependencies import CurrentUser, SessionDep, VerifiedUser
from verity.modules.sessions.copilot import CopilotEngine
from verity.modules.sessions.live_engine import LiveSessionEngine
from verity.modules.sessions.live_models import (
    AiSuggestion,
    DetectedQuestion,
    LiveSession,
)
from verity.modules.sessions.live_report import LiveReportService
from verity.modules.workspace.service import WorkspaceService
from verity.platform.cache import RedisRole, get_redis
from verity.platform.db.session import transaction
from verity.platform.errors import AppError, ErrorCode
from verity.platform.logging import get_logger
from verity.platform.runtime_config import feature_enabled
from verity.platform.security import generate_token, hash_token
from verity.realtime import protocol
from verity.realtime.stt import build_stt_router, frame_rms

log = get_logger("sessions.live_router")

live_router = APIRouter(prefix="/v1/live-sessions", tags=["live-sessions"])
rt_router = APIRouter(tags=["realtime"])

TICKET_TTL_SECONDS = 60


class LiveSessionCreate(BaseModel):
    workspace_id: uuid.UUID
    interview_type: str = Field(default="general", max_length=24)
    response_mode: str = Field(default="balanced", max_length=24)
    language: str = Field(default="en-US", max_length=20)


class LiveSessionResponse(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    status: str
    interview_type: str
    response_mode: str
    integrity_mode: str
    last_event_seq: int


class TicketResponse(BaseModel):
    ticket: str
    expires_in: int
    url: str


@live_router.post("", response_model=LiveSessionResponse, status_code=status.HTTP_201_CREATED)
async def create_live_session(
    payload: LiveSessionCreate, principal: VerifiedUser, session: SessionDep
) -> LiveSessionResponse:
    if not await feature_enabled("live_copilot", user_id=principal.user.id):
        raise AppError(ErrorCode.FORBIDDEN, "Live Copilot is temporarily unavailable.")
    workspace = await WorkspaceService(session).get(
        user_id=principal.user.id, workspace_id=payload.workspace_id
    )
    if not workspace.is_live_copilot_allowed:
        # FR-PRIV-011: the product takes a position rather than deferring.
        raise AppError(
            ErrorCode.FORBIDDEN,
            "Live assistance is disabled for this workspace because it is marked proctored.",
            detail={"integrity_mode": workspace.integrity_mode},
        )

    bundle = await WorkspaceService(session).context(
        user_id=principal.user.id, workspace_id=payload.workspace_id
    )

    live = LiveSession(
        user_id=principal.user.id,
        workspace_id=payload.workspace_id,
        interview_type=payload.interview_type,
        response_mode=payload.response_mode,
        language=payload.language,
        integrity_mode=workspace.integrity_mode,
        context_version=bundle.version,
    )
    session.add(live)
    await session.flush()
    return _response(live)


@live_router.get("/{session_id}", response_model=LiveSessionResponse)
async def get_live_session(
    session_id: uuid.UUID, principal: CurrentUser, session: SessionDep
) -> LiveSessionResponse:
    return _response(await _require_session(session, principal.user.id, session_id))


@live_router.post("/{session_id}/ticket", response_model=TicketResponse)
async def issue_ticket(
    session_id: uuid.UUID, principal: CurrentUser, session: SessionDep
) -> TicketResponse:
    """Mint a single-use socket ticket (PRD §26.1)."""
    live = await _require_session(session, principal.user.id, session_id)

    token = generate_token(24)
    await get_redis(RedisRole.SESSION).set(
        f"wsticket:{token.digest.hex()}",
        f"{principal.user.id}:{live.id}",
        ex=TICKET_TTL_SECONDS,
    )
    return TicketResponse(
        ticket=token.plaintext,
        expires_in=TICKET_TTL_SECONDS,
        url=f"/v1/rt/live/{live.id}",
    )


async def _redeem_ticket(ticket: str, session_id: uuid.UUID) -> uuid.UUID | None:
    """Redeem once. A replayed ticket is refused because the key is deleted."""
    redis = get_redis(RedisRole.SESSION)
    key = f"wsticket:{hash_token(ticket).hex()}"
    raw = await redis.get(key)
    if raw is None:
        return None
    await redis.delete(key)

    value = raw.decode() if isinstance(raw, bytes) else raw
    user_part, _, session_part = value.partition(":")
    if session_part != str(session_id):
        return None
    return uuid.UUID(user_part)


@rt_router.websocket("/v1/rt/live/{session_id}")
async def realtime_socket(websocket: WebSocket, session_id: uuid.UUID) -> None:
    ticket = websocket.query_params.get("ticket", "")
    user_id = await _redeem_ticket(ticket, session_id) if ticket else None
    if user_id is None:
        await websocket.close(code=4401, reason="invalid or expired ticket")
        return

    await websocket.accept(subprotocol=protocol.SUBPROTOCOL)
    engine: LiveSessionEngine | None = None
    # One router per connection: providers buffer per channel, so sharing it
    # across sessions would splice two interviews into one utterance.
    stt = build_stt_router()

    try:
        while True:
            message = await websocket.receive()

            if message.get("type") == "websocket.disconnect":
                break

            if (payload := message.get("bytes")) is not None:
                # Audio frames from the desktop capture. The router batches them
                # into utterances and returns text; from there the path is
                # identical to the web client's, so detection and guidance
                # behave the same whichever client is speaking.
                try:
                    frame = protocol.decode_audio_frame(payload)
                except ValueError:
                    continue
                if engine is None:
                    continue

                engine.state.channel(frame.channel).update(
                    rms=frame_rms(frame.payload), timestamp_ms=frame.timestamp_ms
                )
                try:
                    results = await stt.transcribe(frame.channel, frame.payload, frame.timestamp_ms)
                except Exception as exc:
                    log.warning("stt_failed", error_type=type(exc).__name__)
                    continue

                for result in results:
                    envelope = protocol.Envelope(
                        session_id=session_id,
                        type=protocol.ClientEvent.TRANSCRIPT_TEXT,
                        payload={
                            "channel": result.channel,
                            "content": result.content,
                            "is_final": True,
                            "start_ms": result.start_ms,
                            "end_ms": result.end_ms,
                            "confidence": result.confidence,
                        },
                    )
                    engine = await _handle(websocket, envelope, engine, user_id, session_id)
                continue

            text = message.get("text")
            if not text:
                continue

            envelope = protocol.Envelope.model_validate_json(text)
            engine = await _handle(websocket, envelope, engine, user_id, session_id)

            if envelope.type == str(protocol.ClientEvent.SESSION_END):
                break

    except WebSocketDisconnect:
        log.info("live_socket_disconnected", session_id=str(session_id))
    except Exception as exc:
        log.error("live_socket_error", error_type=type(exc).__name__, exc_info=exc)
    finally:
        if engine is not None:
            async with transaction() as db:
                engine.bind(db, await _reload(db, engine.session.id))
                await engine.flush_transcript(force=True)
                await engine.save_memory()


async def _handle(
    websocket: WebSocket,
    envelope: protocol.Envelope,
    engine: LiveSessionEngine | None,
    user_id: uuid.UUID,
    session_id: uuid.UUID,
) -> LiveSessionEngine | None:
    """One inbound message, in its own transaction."""
    async with transaction() as db:
        live = await _require_session(db, user_id, session_id)

        if engine is None:
            engine = LiveSessionEngine(db, live)
        else:
            engine.bind(db, live)

        outbound: list[protocol.Envelope] = []

        match envelope.type:
            case protocol.ClientEvent.SESSION_START:
                start = protocol.SessionStartPayload.model_validate(envelope.payload)
                replay = await engine.start(resume_from_seq=start.resume_from_seq)
                outbound.extend(replay)
                outbound.append(
                    await engine.emit(
                        protocol.ServerEvent.SESSION_READY,
                        protocol.SessionReadyPayload(
                            stt_provider="text-passthrough",
                            model_tier="standard",
                            features={"diarization": False},
                            resumed_from_seq=start.resume_from_seq,
                        ),
                    )
                )

            case protocol.ClientEvent.TRANSCRIPT_TEXT:
                payload = protocol.TranscriptTextPayload.model_validate(envelope.payload)
                events = await engine.ingest_utterance(
                    channel=payload.channel,
                    content=payload.content,
                    start_ms=payload.start_ms,
                    end_ms=payload.end_ms,
                    is_final=payload.is_final,
                    confidence=payload.confidence,
                )
                outbound.extend(events)
                outbound.extend(await _maybe_guide(db, engine, events, websocket))

            case protocol.ClientEvent.QUESTION_MANUAL:
                manual = protocol.ManualQuestionPayload.model_validate(envelope.payload)
                question, event = await engine.manual_question(manual.content)
                outbound.append(event)
                outbound.extend(await _guide(db, engine, question, manual.response_mode, websocket))

            case protocol.ClientEvent.SESSION_END:
                await engine.end(reason="client_ended")
                # PRD §27: the report is produced automatically. Generating it
                # here means it is already waiting when the user opens it.
                await LiveReportService(db).generate(user_id=user_id, session_id=session_id)
                outbound.append(
                    await engine.emit(
                        protocol.ServerEvent.SESSION_ENDED,
                        {
                            "reason": "client_ended",
                            "duration_seconds": engine.session.duration_seconds,
                        },
                    )
                )

            case protocol.ClientEvent.PING:
                outbound.append(await engine.emit(protocol.ServerEvent.PONG, {}))

            case _:
                log.info("unhandled_client_event", type=envelope.type)

        await engine.flush_transcript()

    for event in outbound:
        await websocket.send_text(event.model_dump_json())
    return engine


async def _maybe_guide(
    db: AsyncSession,
    engine: LiveSessionEngine,
    events: list[protocol.Envelope],
    websocket: WebSocket,
) -> list[protocol.Envelope]:
    """Generate only for a finalized question the detector judged worth it.

    The gate travels on the event rather than being re-derived here. A
    transport that re-decides generates on small talk — the exact failure the
    detector exists to prevent (FR-RT-002).
    """
    finalized = [
        e
        for e in events
        if e.type == str(protocol.ServerEvent.QUESTION_FINALIZED)
        and e.payload.get("should_generate")
    ]
    if not finalized:
        return []

    question_id = uuid.UUID(finalized[-1].payload["detected_question_id"])
    question = await db.get(DetectedQuestion, question_id)
    if question is None:
        return []
    return await _guide(db, engine, question, None, websocket)


async def _guide(
    db: AsyncSession,
    engine: LiveSessionEngine,
    question: DetectedQuestion,
    mode: str | None,
    websocket: WebSocket,
) -> list[protocol.Envelope]:
    allowed, reason = await engine.can_generate()
    if not allowed:
        return [
            await engine.emit(
                protocol.ServerEvent.WARNING,
                protocol.WarningPayload(
                    code=protocol.WarningCode.GENERATION_RATE_LIMITED,
                    message="Guidance is paused briefly to stay within your session limits.",
                    detail={"reason": reason or ""},
                ),
            )
        ]

    bundle = await WorkspaceService(db).context(
        user_id=engine.session.user_id, workspace_id=engine.session.workspace_id
    )
    # The fast lane finishes about a second before the full answer. It is written
    # to the socket the moment it lands, so the candidate reads a direction while
    # the interviewer is still finishing their sentence instead of watching a
    # spinner (PRD §20.3, §32.1).
    #
    # This one frame is deliberately *not* part of the sequenced event log: it
    # carries no seq and is never replayed. Persisting it would need the database
    # session that the generation is already holding, and it is a display hint —
    # the authoritative content arrives in ``answer.complete`` a moment later.
    pending: asyncio.Queue[str] = asyncio.Queue()

    async def _flush_direction() -> None:
        direction = await pending.get()
        await websocket.send_text(
            protocol.Envelope(
                session_id=engine.session.id,
                type=protocol.ServerEvent.ANSWER_FIELD_COMPLETE,
                seq=0,
                payload={
                    "detected_question_id": str(question.id),
                    "field": "answer_direction",
                    "value": direction,
                },
            ).model_dump_json()
        )

    courier = asyncio.create_task(_flush_direction())
    result = await CopilotEngine(db, on_direction=pending.put_nowait).guide(
        question=question,
        bundle=bundle,
        memory=engine.state.memory,
        response_mode=mode or engine.session.response_mode,
    )
    engine.record_generation()
    courier.cancel()

    return [
        await engine.emit(
            protocol.ServerEvent.ANSWER_COMPLETE,
            protocol.AnswerCompletePayload(
                suggestion_id=str(result.suggestion.id),
                detected_question_id=str(question.id),
                content=result.answer.to_json(),
                evidence_ids=result.answer.cited_evidence_ids,
                degraded=result.degraded,
                latency_ms=result.latency_ms,
                grounding=result.grounding.to_json(),
            ),
        ),
    ]


@live_router.get("", response_model=list[LiveSessionResponse])
async def list_live_sessions(
    principal: CurrentUser, session: SessionDep, limit: int = 25
) -> list[LiveSessionResponse]:
    """Session history (PRD §27)."""
    rows = list(
        (
            await session.execute(
                select(LiveSession)
                .where(
                    LiveSession.user_id == principal.user.id,
                    LiveSession.deleted_at.is_(None),
                )
                .order_by(LiveSession.created_at.desc())
                .limit(min(limit, 100))
            )
        ).scalars()
    )
    return [_response(row) for row in rows]


@live_router.get("/{session_id}/report", response_model=dict[str, Any])
async def get_live_report(
    session_id: uuid.UUID, principal: CurrentUser, session: SessionDep
) -> dict[str, Any]:
    """The coverage report. Generated on first read, then served from storage."""
    report = await LiveReportService(session).generate(
        user_id=principal.user.id, session_id=session_id
    )
    return {
        "session_id": str(report.session_id),
        "status": report.status,
        "summary": report.summary,
        "overall_score": float(report.overall_score) if report.overall_score else None,
        "dimension_scores": report.dimension_scores,
        "coverage": report.coverage,
        "metrics": report.delivery_metrics,
        "strengths": report.strengths,
        "weaknesses": report.weaknesses,
        "recommendations": report.recommendations,
        "questions": report.per_question,
        "rubric_version": report.rubric_version,
    }


@live_router.get("/{session_id}/timeline", response_model=dict[str, Any])
async def get_timeline(
    session_id: uuid.UUID, principal: CurrentUser, session: SessionDep
) -> dict[str, Any]:
    """Questions, guidance and grounding for the session report (PRD §28)."""
    await _require_session(session, principal.user.id, session_id)

    questions = list(
        (
            await session.execute(
                select(DetectedQuestion)
                .where(DetectedQuestion.session_id == session_id)
                .order_by(DetectedQuestion.detected_at_ms)
            )
        ).scalars()
    )
    suggestions = list(
        (
            await session.execute(select(AiSuggestion).where(AiSuggestion.session_id == session_id))
        ).scalars()
    )
    by_question: dict[uuid.UUID, list[AiSuggestion]] = {}
    for suggestion in suggestions:
        by_question.setdefault(suggestion.detected_question_id, []).append(suggestion)

    return {
        "questions": [
            {
                "id": str(q.id),
                "content": q.content,
                "category": q.category,
                "confidence": float(q.detection_confidence),
                "trigger": q.trigger,
                "detected_at_ms": q.detected_at_ms,
                "suggestions": [
                    {
                        "id": str(s.id),
                        "revision": s.revision,
                        "mode": s.response_mode,
                        "content": s.content,
                        "grounding": s.grounding,
                        "latency_ms": s.latency_ms,
                    }
                    for s in sorted(by_question.get(q.id, []), key=lambda s: s.revision)
                ],
            }
            for q in questions
        ]
    }


async def _require_session(
    db: AsyncSession, user_id: uuid.UUID, session_id: uuid.UUID
) -> LiveSession:
    live = (
        await db.execute(
            select(LiveSession).where(
                LiveSession.id == session_id,
                LiveSession.user_id == user_id,
                LiveSession.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if live is None:
        raise AppError.not_found("Live session")
    return live


async def _reload(db: AsyncSession, session_id: uuid.UUID) -> LiveSession:
    live = await db.get(LiveSession, session_id)
    if live is None:
        raise AppError.not_found("Live session")
    return live


def _response(live: LiveSession) -> LiveSessionResponse:
    return LiveSessionResponse(
        id=live.id,
        workspace_id=live.workspace_id,
        status=live.status,
        interview_type=live.interview_type,
        response_mode=live.response_mode,
        integrity_mode=live.integrity_mode,
        last_event_seq=live.last_event_seq,
    )
