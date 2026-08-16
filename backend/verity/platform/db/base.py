"""Declarative base and shared column mixins (PRD §24.1 conventions).

Every user-owned table carries ``user_id`` and a nullable ``organization_id``
(reserved for B2B per PRD PP20), timestamps, and — for user content — a
``deleted_at`` soft-delete column. Mixins exist so those conventions cannot be
forgotten table by table.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import DateTime, ForeignKey, MetaData, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Deterministic constraint naming keeps Alembic autogenerate diffs stable.
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    type_annotation_map = {  # noqa: RUF012 - SQLAlchemy API
        dict[str, Any]: JSONB,
        list[Any]: JSONB,
        uuid.UUID: UUID(as_uuid=True),
        dt.datetime: DateTime(timezone=True),
    }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        pk = getattr(self, "id", None)
        return f"<{type(self).__name__} id={pk}>"


def uuid7() -> uuid.UUID:
    """Time-ordered UUID (RFC 9562 v7) for insert-heavy tables.

    Time ordering keeps B-tree inserts append-mostly, which matters for
    ``transcript_segments`` and ``usage_events`` (PRD §24.1).
    """
    unix_ms = int(dt.datetime.now(dt.UTC).timestamp() * 1000)
    rand = uuid.uuid4().bytes
    raw = bytearray(unix_ms.to_bytes(6, "big") + rand[6:])
    raw[6] = (raw[6] & 0x0F) | 0x70  # version 7
    raw[8] = (raw[8] & 0x3F) | 0x80  # RFC 4122 variant
    return uuid.UUID(bytes=bytes(raw))


class UUIDPrimaryKey:
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)


class Timestamped:
    created_at: Mapped[dt.datetime] = mapped_column(
        server_default=func.now(), nullable=False, index=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )


class SoftDeletable:
    """User content is soft-deleted; the deletion pipeline (PRD §29.4) hard-deletes."""

    deleted_at: Mapped[dt.datetime | None] = mapped_column(default=None)

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None


class UserOwned:
    """Tenant boundary. Every query against these tables is user-scoped in SQL."""

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    organization_id: Mapped[uuid.UUID | None] = mapped_column(default=None)


def jsonb_dict() -> Mapped[dict[str, Any]]:
    return mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))


def jsonb_list() -> Mapped[list[Any]]:
    return mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
