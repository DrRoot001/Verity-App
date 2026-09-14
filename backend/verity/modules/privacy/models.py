"""Deletion pipeline state (PRD §24, §29.4).

The job row is the record of what was actually erased, per store. AC-PRIV-001
requires per-store timestamps, so ``stores`` is written incrementally as each
store confirms rather than stamped once at the end — a job that dies halfway
must show exactly how far it got.
"""

from __future__ import annotations

import datetime as dt
import uuid
from enum import StrEnum
from typing import Any

from sqlalchemy import CheckConstraint, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from verity.platform.config import settings
from verity.platform.db.base import Base, Timestamped, UUIDPrimaryKey

#: PRD FR-AUTH-009. Long enough to undo a rash decision, short enough to be a
#: real deletion promise. Overridable per environment.
GRACE_PERIOD_DAYS = settings.deletion_grace_days


class DeletionStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class DeletionJob(Base, UUIDPrimaryKey, Timestamped):
    __tablename__ = "deletion_jobs"

    #: No foreign key: the job outlives the user row it deletes.
    user_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=DeletionStatus.PENDING
    )
    requested_at: Mapped[dt.datetime] = mapped_column(server_default=text("now()"))
    execute_after: Mapped[dt.datetime] = mapped_column(nullable=False)
    completed_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    cancelled_at: Mapped[dt.datetime | None] = mapped_column(default=None)

    #: Per-store confirmation: {"postgres": {"at": ..., "rows": n}, ...}
    stores: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    attempts: Mapped[int] = mapped_column(nullable=False, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(Text, default=None)

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','running','completed','cancelled','failed')",
            name="deletion_jobs_status_valid",
        ),
        Index(
            "ix_deletion_jobs_due",
            "execute_after",
            postgresql_where=text("status = 'pending'"),
        ),
    )

    @property
    def is_cancellable(self) -> bool:
        return self.status == DeletionStatus.PENDING
