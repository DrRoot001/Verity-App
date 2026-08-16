"""WebSocket protocol (PRD §26).

Every server→client event carries a monotonic ``seq`` so a reconnecting client
can ask for exactly what it missed, and every event is validated against a
typed model so a protocol change is a compile-time concern rather than a
runtime surprise.

Audio arrives as binary frames rather than JSON: base64 in a JSON envelope
would add roughly a third to the byte count on the one path where latency is
the product (PRD §32).
"""

from __future__ import annotations

import datetime as dt
import struct
import uuid
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

PROTOCOL_VERSION = 1
SUBPROTOCOL = "verity.rt.v1"

#: Binary audio frame: [1B channel][8B timestamp_ms][opus payload].
_AUDIO_HEADER = struct.Struct(">BQ")
AUDIO_HEADER_BYTES = _AUDIO_HEADER.size


class ClientEvent(StrEnum):
    SESSION_START = "session.start"
    AUDIO_PAUSE = "audio.pause"
    AUDIO_RESUME = "audio.resume"
    TRANSCRIPT_TEXT = "transcript.text"
    QUESTION_MANUAL = "question.manual"
    QUESTION_DISMISS = "question.dismiss"
    RESPONSE_REGENERATE = "response.regenerate"
    RESPONSE_SHORTER = "response.shorter"
    RESPONSE_EXPAND = "response.expand"
    RESPONSE_MODE = "response.mode"
    RESPONSE_PIN = "response.pin"
    CONTEXT_UPDATE = "context.update"
    SESSION_MARK = "session.mark"
    SESSION_END = "session.end"
    ACK = "ack"
    PING = "ping"


class ServerEvent(StrEnum):
    SESSION_READY = "session.ready"
    STT_PARTIAL = "stt.partial"
    STT_FINAL = "stt.final"
    SPEAKER_DETECTED = "speaker.detected"
    QUESTION_DETECTED = "question.detected"
    QUESTION_FINALIZED = "question.finalized"
    QUESTION_CLASSIFIED = "question.classified"
    QUESTION_SUPERSEDED = "question.superseded"
    CONTEXT_READY = "context.ready"
    ANSWER_DELTA = "answer.delta"
    ANSWER_FIELD_COMPLETE = "answer.field_complete"
    ANSWER_COMPLETE = "answer.complete"
    ANSWER_CANCELLED = "answer.cancelled"
    USAGE_UPDATE = "usage.update"
    WARNING = "warning"
    ERROR = "error"
    SESSION_RECONNECTING = "session.reconnecting"
    SESSION_RESUMED = "session.resumed"
    SESSION_ENDED = "session.ended"
    PONG = "pong"


class WarningCode(StrEnum):
    """Advisory conditions. The session continues (PRD §34.1)."""

    STT_LAG = "stt_lag"
    POOR_AUDIO = "poor_audio"
    NO_AUDIO = "no_audio"
    DEGRADED_GENERATION = "degraded_generation"
    DEGRADED_RETRIEVAL = "degraded_retrieval"
    ENTITLEMENT_LOW = "entitlement_low"
    ENTITLEMENT_EXHAUSTED = "entitlement_exhausted"
    STT_FAILOVER = "stt_failover"
    MANUAL_MODE = "manual_mode"
    GENERATION_RATE_LIMITED = "generation_rate_limited"


class Envelope(BaseModel):
    """Every frame on the socket, in both directions."""

    v: int = PROTOCOL_VERSION
    event_id: str = Field(default_factory=lambda: f"evt_{uuid.uuid4().hex}")
    session_id: str
    seq: int = 0
    ts: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.UTC))
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)


# ── Client payloads ──────────────────────────────────────────────────


class SessionStartPayload(BaseModel):
    workspace_id: uuid.UUID
    interview_type: str = "general"
    response_mode: str = "balanced"
    language: str = "en-US"
    #: Present on reconnect; the server replays from here (PRD §26.6).
    resume_from_seq: int | None = None
    client: dict[str, Any] = Field(default_factory=dict)


class TranscriptTextPayload(BaseModel):
    """Text-mode transport.

    The desktop app streams audio; the web client and the test harness push
    already-transcribed utterances through this event. Both converge on the
    same utterance processor, so detection behaves identically.
    """

    channel: Literal["mic", "system"]
    content: str = Field(min_length=1, max_length=4000)
    is_final: bool = True
    start_ms: int = 0
    end_ms: int = 0
    confidence: float | None = None


class ManualQuestionPayload(BaseModel):
    """FR-RT-007: always available, and bypasses detection entirely."""

    content: str = Field(min_length=1, max_length=2000)
    response_mode: str | None = None


class ResponseAdjustPayload(BaseModel):
    suggestion_id: uuid.UUID
    mode: str | None = None
    hint: str | None = Field(default=None, max_length=500)


class ContextUpdatePayload(BaseModel):
    notes: str | None = Field(default=None, max_length=4000)
    speaker_labels: dict[str, str] = Field(default_factory=dict)
    focus_hint: str | None = Field(default=None, max_length=200)


class AckPayload(BaseModel):
    last_seq: int


# ── Server payloads ──────────────────────────────────────────────────


class SessionReadyPayload(BaseModel):
    stt_provider: str
    model_tier: str
    features: dict[str, bool] = Field(default_factory=dict)
    entitlement: dict[str, Any] = Field(default_factory=dict)
    resumed_from_seq: int | None = None


class TranscriptPayload(BaseModel):
    segment_id: str | None = None
    channel: str
    speaker_role: str
    content: str
    start_ms: int
    end_ms: int
    confidence: float | None = None


class QuestionPayload(BaseModel):
    detected_question_id: str
    content: str
    confidence: float
    sub_parts: list[str] = Field(default_factory=list)
    category: str | None = None
    framework: str | None = None
    signals: dict[str, Any] = Field(default_factory=dict)
    #: The detector's gate (FR-RT-002). Carried on the event so the transport
    #: cannot re-decide it — a consumer that ignores this generates on small
    #: talk, which is the exact failure the detector exists to prevent.
    should_generate: bool = False
    reason: str = ""


class EvidencePayload(BaseModel):
    detected_question_id: str
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    took_ms: float = 0.0
    degraded: bool = False


class AnswerDeltaPayload(BaseModel):
    suggestion_id: str
    field: str
    index: int | None = None
    text_delta: str


class AnswerFieldPayload(BaseModel):
    suggestion_id: str
    field: str
    value: Any


class AnswerCompletePayload(BaseModel):
    suggestion_id: str
    detected_question_id: str
    content: dict[str, Any]
    evidence_ids: list[str] = Field(default_factory=list)
    degraded: bool = False
    latency_ms: dict[str, float] = Field(default_factory=dict)


class WarningPayload(BaseModel):
    code: WarningCode
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)


class UsagePayload(BaseModel):
    live_minutes_remaining: float | None = None
    generations_used: int = 0
    metered_seconds: int = 0


# ── Binary audio framing ─────────────────────────────────────────────

CHANNEL_MIC = 0
CHANNEL_SYSTEM = 1
_CHANNEL_NAMES = {CHANNEL_MIC: "mic", CHANNEL_SYSTEM: "system"}


class AudioFrame(BaseModel):
    channel: Literal["mic", "system"]
    timestamp_ms: int
    payload: bytes


def encode_audio_frame(channel: str, timestamp_ms: int, payload: bytes) -> bytes:
    channel_id = CHANNEL_MIC if channel == "mic" else CHANNEL_SYSTEM
    return _AUDIO_HEADER.pack(channel_id, timestamp_ms) + payload


def decode_audio_frame(data: bytes) -> AudioFrame:
    if len(data) < AUDIO_HEADER_BYTES:
        raise ValueError("audio frame is shorter than its header")
    channel_id, timestamp_ms = _AUDIO_HEADER.unpack(data[:AUDIO_HEADER_BYTES])
    name = _CHANNEL_NAMES.get(channel_id)
    if name is None:
        raise ValueError(f"unknown audio channel {channel_id}")
    return AudioFrame(
        channel=name,
        timestamp_ms=timestamp_ms,
        payload=data[AUDIO_HEADER_BYTES:],
    )


def build(
    session_id: uuid.UUID | str,
    event_type: ServerEvent,
    payload: BaseModel | dict[str, Any],
    seq: int,
) -> Envelope:
    return Envelope(
        session_id=str(session_id),
        seq=seq,
        type=str(event_type),
        payload=payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload,
    )
