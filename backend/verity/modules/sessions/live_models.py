"""Live session schema (PRD §24.6).

A live session is event-sourced. Every server→client event is appended to
``session_events`` with a monotonic ``seq``, which is what makes reconnection a
replay rather than a reset (PRD §26.6, NFR-RT-010): a client that drops for
twenty seconds reconnects with its last sequence number and receives exactly
what it missed.

``detected_questions`` and ``ai_suggestions`` are separate tables because a
question outlives any single generation — regenerating, shortening or switching
response mode produces another suggestion against the same question, and the
report needs all of them.
"""

from __future__ import annotations

import datetime as dt
import uuid
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from verity.platform.db.base import (
    Base,
    SoftDeletable,
    Timestamped,
    UserOwned,
    UUIDPrimaryKey,
)


class LiveSessionStatus(StrEnum):
    CREATED = "created"
    CONNECTING = "connecting"
    ACTIVE = "active"
    RECONNECTING = "reconnecting"
    DEGRADED = "degraded"
    PAUSED = "paused"
    ENDED = "ended"
    FAILED = "failed"


class QuestionLifecycle(StrEnum):
    """PRD §14.2. The order is the contract; skipping states is a bug."""

    DETECTED = "detected"
    ACCUMULATING = "accumulating"
    FINALIZED = "finalized"
    CLASSIFIED = "classified"
    RETRIEVING = "retrieving"
    GENERATING = "generating"
    DISPLAYED = "displayed"
    SUPERSEDED = "superseded"
    ANSWERED = "answered"
    DISMISSED = "dismissed"


class AudioRetention(StrEnum):
    """Default is NONE — interview audio is the most sensitive data we touch."""

    NONE = "none"
    SESSION = "session"
    THIRTY_DAYS = "30d"


class LiveSession(Base, UUIDPrimaryKey, Timestamped, SoftDeletable, UserOwned):
    __tablename__ = "live_sessions"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    device_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("devices.id", ondelete="SET NULL"), default=None
    )

    interview_type: Mapped[str] = mapped_column(String(24), nullable=False)
    integrity_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="assisted"
    )
    language: Mapped[str] = mapped_column(String(20), nullable=False, server_default="en-US")
    response_mode: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default="balanced"
    )

    stt_provider: Mapped[str | None] = mapped_column(String(48), default=None)
    llm_route: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=LiveSessionStatus.CREATED
    )
    degraded_reason: Mapped[str | None] = mapped_column(String(64), default=None)

    audio_retention: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=AudioRetention.NONE
    )

    started_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    ended_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, default=None)

    #: Highest emitted event sequence, so a reconnect knows where to resume.
    last_event_seq: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    context_version: Mapped[int | None] = mapped_column(Integer, default=None)

    #: Billable seconds, metered as audio is received (PRD §33).
    metered_seconds: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    generation_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    __table_args__ = (
        CheckConstraint(
            "integrity_mode IN ('assisted','proctored')", name="live_sessions_integrity_valid"
        ),
        CheckConstraint(
            "audio_retention IN ('none','session','30d')",
            name="live_sessions_retention_valid",
        ),
        Index(
            "ix_live_sessions_user_recent",
            "user_id",
            "created_at",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    @property
    def is_live(self) -> bool:
        return self.status in (
            LiveSessionStatus.ACTIVE,
            LiveSessionStatus.RECONNECTING,
            LiveSessionStatus.DEGRADED,
        )


class SessionEvent(Base, UUIDPrimaryKey):
    """Append-only recovery log (PRD §26.6). Retained 30 days."""

    __tablename__ = "session_events"

    session_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    type: Mapped[str] = mapped_column(String(48), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[dt.datetime] = mapped_column(server_default=text("now()"), nullable=False)

    __table_args__ = (
        UniqueConstraint("session_id", "seq", name="uq_session_events_session_seq"),
        Index("ix_session_events_replay", "session_id", "seq"),
    )


class SessionSpeaker(Base, UUIDPrimaryKey, UserOwned):
    """Who is speaking (PRD §14.7).

    Channel is the primary separator: the desktop app captures microphone and
    system audio separately, which is far more reliable than diarization.
    """

    __tablename__ = "session_speakers"

    session_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    channel: Mapped[str] = mapped_column(String(8), nullable=False)
    provider_speaker_tag: Mapped[str | None] = mapped_column(String(32), default=None)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    label: Mapped[str | None] = mapped_column(String(64), default=None)
    first_seen_at: Mapped[dt.datetime | None] = mapped_column(default=None)

    __table_args__ = (
        UniqueConstraint(
            "session_id", "channel", "provider_speaker_tag", name="uq_session_speakers_identity"
        ),
        CheckConstraint("channel IN ('mic','system')", name="session_speakers_channel_valid"),
    )


class DetectedQuestion(Base, UUIDPrimaryKey, Timestamped, UserOwned):
    __tablename__ = "detected_questions"

    session_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    session_kind: Mapped[str] = mapped_column(String(8), nullable=False, server_default="live")
    workspace_id: Mapped[uuid.UUID] = mapped_column(nullable=False)

    content: Mapped[str] = mapped_column(Text, nullable=False)
    speaker_id: Mapped[uuid.UUID | None] = mapped_column(default=None)

    category: Mapped[str | None] = mapped_column(String(32), default=None)
    framework: Mapped[str | None] = mapped_column(String(24), default=None)
    #: Multi-part questions are decomposed rather than answered as one blob
    #: (PRD FR-RT-003).
    sub_parts: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )

    detection_confidence: Mapped[float] = mapped_column(Numeric(4, 3), nullable=False)
    classification_confidence: Mapped[float | None] = mapped_column(Numeric(4, 3), default=None)
    #: Which signals fired, so a false detection is diagnosable.
    signals: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    lifecycle_state: Mapped[str] = mapped_column(String(16), nullable=False)
    detected_at_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    finalized_at_ms: Mapped[int | None] = mapped_column(Integer, default=None)

    trigger: Mapped[str] = mapped_column(String(8), nullable=False, server_default="auto")

    __table_args__ = (
        CheckConstraint("trigger IN ('auto','manual')", name="detected_questions_trigger_valid"),
        CheckConstraint(
            "detection_confidence >= 0 AND detection_confidence <= 1",
            name="detected_questions_confidence_range",
        ),
        Index("ix_detected_questions_timeline", "session_id", "detected_at_ms"),
    )


class AiSuggestion(Base, UUIDPrimaryKey, Timestamped, UserOwned):
    """One generated answer object (PRD §14.5, §24.6)."""

    __tablename__ = "ai_suggestions"

    detected_question_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("detected_questions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)

    response_mode: Mapped[str] = mapped_column(String(24), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    revision_reason: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default="initial"
    )

    #: The answer schema object, with claim types on every key point.
    content: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    evidence_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(String(64)), nullable=False, server_default=text("'{}'")
    )
    #: Validator output — violations downgraded, never silently rendered.
    grounding: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    prompt_ref: Mapped[str | None] = mapped_column(String(120), default=None)
    model: Mapped[str | None] = mapped_column(String(120), default=None)
    provider: Mapped[str | None] = mapped_column(String(48), default=None)

    #: Per-stage timings, so the §32 budgets are measurable per question.
    latency_ms: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    degraded: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    degraded_reason: Mapped[str | None] = mapped_column(String(64), default=None)

    input_tokens: Mapped[int | None] = mapped_column(Integer, default=None)
    output_tokens: Mapped[int | None] = mapped_column(Integer, default=None)
    cost_usd: Mapped[float | None] = mapped_column(Numeric(12, 6), default=None)

    shown_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))

    __table_args__ = (
        UniqueConstraint(
            "detected_question_id", "revision", name="uq_ai_suggestions_question_revision"
        ),
    )
