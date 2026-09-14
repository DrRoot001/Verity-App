"""Admin roles and audit trail (PRD §18, §28.4).

Two rules shape this module:

1. **Least privilege by role.** Support can read a user's account state but not
   their transcript; reading interview content requires an explicit reason and
   leaves a record the *user* can see (FR-ADMIN-002).
2. **Every privileged action is audited, immutably.** The audit table is
   append-only — there is no update path in the service — because an audit log
   an operator can edit is not an audit log.
"""

from __future__ import annotations

import datetime as dt
import uuid
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from verity.platform.db.base import Base, Timestamped, UUIDPrimaryKey


class StaffRole(StrEnum):
    """PRD §18.2 FR-ADMIN-001."""

    SUPPORT = "support"
    BILLING_OPS = "billing_ops"
    AI_OPS = "ai_ops"
    SECURITY = "security"
    ADMIN = "admin"


#: What each role may do. Deliberately explicit rather than hierarchical: a
#: billing operator having AI-ops powers "because it is a higher tier" is how
#: least privilege quietly erodes.
ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    StaffRole.SUPPORT: frozenset(
        {"user.read", "session.read_metadata", "session.read_content", "support.write"}
    ),
    StaffRole.BILLING_OPS: frozenset({"user.read", "billing.read", "billing.write"}),
    StaffRole.AI_OPS: frozenset(
        {"ai.read", "ai.write", "flags.read", "flags.write", "prompts.read", "prompts.write"}
    ),
    StaffRole.SECURITY: frozenset({"user.read", "audit.read", "user.suspend"}),
    StaffRole.ADMIN: frozenset(
        {
            "user.read",
            "user.suspend",
            "user.delete",
            "billing.read",
            "billing.write",
            "ai.read",
            "ai.write",
            "flags.read",
            "flags.write",
            "prompts.read",
            "prompts.write",
            "audit.read",
            "metrics.read",
            "support.write",
            "session.read_metadata",
            "staff.read",
            "staff.write",
        }
    ),
}

#: Actions that additionally require a stated reason and a support case
#: (FR-ADMIN-002). Reading someone's interview is not a routine lookup.
REASON_REQUIRED: frozenset[str] = frozenset({"session.read_content", "user.delete", "user.suspend"})


class StaffMember(Base, UUIDPrimaryKey, Timestamped):
    """A staff grant. Separate from ``users`` so a customer account cannot be
    escalated by flipping a column on it."""

    __tablename__ = "staff_members"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(24), nullable=False)
    granted_by: Mapped[uuid.UUID | None] = mapped_column(default=None)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(default=None)

    __table_args__ = (
        UniqueConstraint("user_id", "role", name="uq_staff_members_user_role"),
        CheckConstraint(
            "role IN ('support','billing_ops','ai_ops','security','admin')",
            name="staff_members_role_valid",
        ),
    )

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None

    @property
    def permissions(self) -> frozenset[str]:
        return ROLE_PERMISSIONS.get(self.role, frozenset())


class AuditLog(Base, UUIDPrimaryKey):
    """Append-only record of privileged actions (PRD §28.4)."""

    __tablename__ = "audit_logs"

    actor_type: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(default=None)
    subject_user_id: Mapped[uuid.UUID | None] = mapped_column(default=None)

    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(48), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(64), default=None)

    #: Why. Mandatory for anything in REASON_REQUIRED.
    reason: Mapped[str | None] = mapped_column(Text, default=None)
    case_id: Mapped[str | None] = mapped_column(String(64), default=None)

    ip_hash: Mapped[bytes | None] = mapped_column(default=None)
    audit_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[dt.datetime] = mapped_column(server_default=text("now()"), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "actor_type IN ('user','admin','system')", name="audit_logs_actor_type_valid"
        ),
        Index("ix_audit_logs_subject", "subject_user_id", "created_at"),
        Index("ix_audit_logs_actor", "actor_id", "created_at"),
        Index("ix_audit_logs_action", "action", "created_at"),
    )


class PlatformSetting(Base, UUIDPrimaryKey, Timestamped):
    """Non-secret runtime configuration editable by AI/platform operators.

    Provider credentials deliberately do not live here. The row stores model
    selection, budgets, and operational limits; keys remain in the deployment
    secret store and are only exposed as configured/not-configured booleans.
    """

    __tablename__ = "platform_settings"

    namespace: Mapped[str] = mapped_column(String(32), nullable=False)
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    updated_by: Mapped[uuid.UUID | None] = mapped_column(default=None)

    __table_args__ = (
        UniqueConstraint("namespace", "key", name="uq_platform_settings_namespace_key"),
        Index("ix_platform_settings_namespace", "namespace"),
    )


class FeatureFlag(Base, UUIDPrimaryKey, Timestamped):
    """Database-backed rollout switch with an explicit percentage."""

    __tablename__ = "feature_flags"

    key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    rollout_percentage: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("100")
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(default=None)

    __table_args__ = (
        CheckConstraint(
            "rollout_percentage >= 0 AND rollout_percentage <= 100",
            name="feature_flags_rollout_range",
        ),
    )


class PromptVersion(Base, UUIDPrimaryKey, Timestamped):
    """Immutable prompt version; activation changes rollout state, not content."""

    __tablename__ = "prompt_versions"

    prompt_id: Mapped[str] = mapped_column(String(120), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    task_class: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="development")
    system_template: Mapped[str] = mapped_column(Text, nullable=False)
    user_template: Mapped[str] = mapped_column(Text, nullable=False)
    variables: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    output_schema: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    notes: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    eval_score: Mapped[float | None] = mapped_column(Float, default=None)
    created_by: Mapped[uuid.UUID | None] = mapped_column(default=None)
    activated_at: Mapped[dt.datetime | None] = mapped_column(default=None)

    __table_args__ = (
        UniqueConstraint("prompt_id", "version", name="uq_prompt_versions_id_version"),
        CheckConstraint(
            "status IN ('development','staging','production','archived')",
            name="prompt_versions_status_valid",
        ),
        Index("ix_prompt_versions_lookup", "prompt_id", "status", "version"),
    )
