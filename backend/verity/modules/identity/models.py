"""Identity domain models (PRD §24.2).

Tables: ``users``, ``accounts``, ``devices``, ``sessions`` (auth sessions —
distinct from interview sessions, which live in the sessions module).
"""

from __future__ import annotations

import datetime as dt
import uuid
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from verity.platform.db.base import Base, SoftDeletable, Timestamped, UUIDPrimaryKey

# RFC 5321 caps the addr-spec at 320 characters.
EMAIL_MAX_LENGTH = 320


def normalize_email(raw: str) -> str:
    """Canonical storage form for an address.

    Email local-parts are technically case-sensitive but no mainstream provider
    treats them that way, and case-variant duplicates are an account-takeover
    footgun. Normalizing at the boundary (rather than relying on a ``citext``
    column) keeps the comparison rule explicit, portable across Postgres
    distributions, and identical in application code and SQL.
    """
    return raw.strip().casefold()


class UserStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    PENDING_DELETION = "pending_deletion"
    DELETED = "deleted"


class AuthProvider(StrEnum):
    PASSWORD = "password"
    GOOGLE = "google"
    APPLE = "apple"


class DevicePlatform(StrEnum):
    WEB = "web"
    MACOS = "macos"
    WINDOWS = "windows"
    IOS = "ios"
    ANDROID = "android"


DEFAULT_ONBOARDING_STATE: dict[str, Any] = {"step": 1, "completed": [], "skipped": []}


class User(Base, UUIDPrimaryKey, Timestamped, SoftDeletable):
    __tablename__ = "users"

    organization_id: Mapped[uuid.UUID | None] = mapped_column(default=None)

    # Stored pre-normalized by normalize_email(); the CHECK makes that invariant
    # enforceable by the database rather than by convention.
    email: Mapped[str] = mapped_column(String(EMAIL_MAX_LENGTH), nullable=False, unique=True)
    email_verified_at: Mapped[dt.datetime | None] = mapped_column(default=None)

    # Null for OAuth-only accounts (PRD FR-AUTH-012 account linking).
    password_hash: Mapped[str | None] = mapped_column(Text, default=None)

    full_name: Mapped[str | None] = mapped_column(String(200), default=None)

    # PRD PP17: locale/timezone are first-class, never English/UTC assumptions.
    locale: Mapped[str] = mapped_column(String(20), nullable=False, server_default="en-US")
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, server_default="UTC")
    interview_locale: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="en-US"
    )

    # PRD §38.4 data residency — determines storage routing at login.
    data_region: Mapped[str] = mapped_column(String(8), nullable=False, server_default="us")

    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=UserStatus.ACTIVE
    )

    # PRD FR-AUTH-010: onboarding is resumable from any device.
    onboarding_state: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text('\'{"step": 1, "completed": [], "skipped": []}\'::jsonb'),
    )

    mfa_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    # Envelope-encrypted at the application layer (PRD §28.1).
    mfa_secret_enc: Mapped[bytes | None] = mapped_column(LargeBinary, default=None)

    deletion_requested_at: Mapped[dt.datetime | None] = mapped_column(default=None)

    __table_args__ = (
        CheckConstraint(
            "status IN ('active','suspended','pending_deletion','deleted')",
            name="users_status_valid",
        ),
        CheckConstraint("data_region IN ('us','eu')", name="users_data_region_valid"),
        CheckConstraint("email = lower(email)", name="users_email_normalized"),
        CheckConstraint("position('@' in email) > 1", name="users_email_shape"),
        Index("ix_users_status_active", "status", postgresql_where=text("deleted_at IS NULL")),
        Index(
            "ix_users_deletion_requested",
            "deletion_requested_at",
            postgresql_where=text("deletion_requested_at IS NOT NULL"),
        ),
    )

    @property
    def is_verified(self) -> bool:
        return self.email_verified_at is not None

    @property
    def can_use_ai(self) -> bool:
        """PRD FR-AUTH-001: verification gates AI operations, not browsing."""
        return self.is_verified and self.status == UserStatus.ACTIVE


class Account(Base, UUIDPrimaryKey):
    """Federated identity link (PRD FR-AUTH-002/003/012)."""

    __tablename__ = "accounts"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_account_id: Mapped[str] = mapped_column(String(255), nullable=False)
    email_at_provider: Mapped[str | None] = mapped_column(String(EMAIL_MAX_LENGTH), default=None)
    created_at: Mapped[dt.datetime] = mapped_column(server_default=text("now()"), nullable=False)

    __table_args__ = (
        UniqueConstraint("provider", "provider_account_id", name="uq_accounts_provider_identity"),
        CheckConstraint(
            "provider IN ('password','google','apple')", name="accounts_provider_valid"
        ),
    )


class Device(Base, UUIDPrimaryKey):
    """Trusted-device registry (PRD FR-AUTH-006)."""

    __tablename__ = "devices"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    platform: Mapped[str] = mapped_column(String(16), nullable=False)
    app_version: Mapped[str | None] = mapped_column(String(32), default=None)
    trusted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    push_token: Mapped[str | None] = mapped_column(String(512), default=None)
    # Coarse location only, for the "new sign-in" notification. Never a raw IP.
    last_ip_city: Mapped[str | None] = mapped_column(String(120), default=None)
    last_seen_at: Mapped[dt.datetime] = mapped_column(server_default=text("now()"), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(server_default=text("now()"), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "platform IN ('web','macos','windows','ios','android')", name="devices_platform_valid"
        ),
        Index("ix_devices_user_last_seen", "user_id", "last_seen_at"),
    )


class AuthSession(Base, UUIDPrimaryKey):
    """Refresh-token family (PRD FR-AUTH-005, §28.2 reuse detection).

    One row per issued refresh token. Rotation inserts a new row sharing the
    ``family_id``; presenting a revoked token from a live family means theft, so
    the whole family is revoked.
    """

    __tablename__ = "sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    device_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("devices.id", ondelete="SET NULL"), default=None
    )
    family_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    refresh_token_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, unique=True)

    issued_at: Mapped[dt.datetime] = mapped_column(server_default=text("now()"), nullable=False)
    expires_at: Mapped[dt.datetime] = mapped_column(nullable=False)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    revoked_reason: Mapped[str | None] = mapped_column(String(64), default=None)

    ip_hash: Mapped[bytes | None] = mapped_column(LargeBinary, default=None)
    user_agent: Mapped[str | None] = mapped_column(String(512), default=None)

    __table_args__ = (
        Index(
            "ix_sessions_user_active",
            "user_id",
            "expires_at",
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )

    def is_active(self, now: dt.datetime) -> bool:
        return self.revoked_at is None and self.expires_at > now
