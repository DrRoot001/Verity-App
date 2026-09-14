"""admin runtime settings, flags, and prompt registry

Revision ID: 2a9d71c4f6e8
Revises: 9606771e9510
Create Date: 2026-08-16 15:10:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "2a9d71c4f6e8"
down_revision: str | None = "9606771e9510"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "platform_settings",
        sa.Column("namespace", sa.String(length=32), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column(
            "value",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("updated_by", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_platform_settings")),
        sa.UniqueConstraint("namespace", "key", name="uq_platform_settings_namespace_key"),
    )
    op.create_index("ix_platform_settings_namespace", "platform_settings", ["namespace"])
    op.create_index(op.f("ix_platform_settings_created_at"), "platform_settings", ["created_at"])

    op.create_table(
        "feature_flags",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "rollout_percentage", sa.Integer(), server_default=sa.text("100"), nullable=False
        ),
        sa.Column("updated_by", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "rollout_percentage >= 0 AND rollout_percentage <= 100",
            name=op.f("ck_feature_flags_feature_flags_rollout_range"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_feature_flags")),
        sa.UniqueConstraint("key", name=op.f("uq_feature_flags_key")),
    )
    op.create_index(op.f("ix_feature_flags_created_at"), "feature_flags", ["created_at"])

    op.create_table(
        "prompt_versions",
        sa.Column("prompt_id", sa.String(length=120), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("task_class", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="development", nullable=False),
        sa.Column("system_template", sa.Text(), nullable=False),
        sa.Column("user_template", sa.Text(), nullable=False),
        sa.Column(
            "variables",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("output_schema", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("notes", sa.Text(), server_default="", nullable=False),
        sa.Column("eval_score", sa.Float(), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('development','staging','production','archived')",
            name=op.f("ck_prompt_versions_prompt_versions_status_valid"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_prompt_versions")),
        sa.UniqueConstraint("prompt_id", "version", name="uq_prompt_versions_id_version"),
    )
    op.create_index(op.f("ix_prompt_versions_created_at"), "prompt_versions", ["created_at"])
    op.create_index(
        "ix_prompt_versions_lookup", "prompt_versions", ["prompt_id", "status", "version"]
    )


def downgrade() -> None:
    op.drop_index("ix_prompt_versions_lookup", table_name="prompt_versions")
    op.drop_index(op.f("ix_prompt_versions_created_at"), table_name="prompt_versions")
    op.drop_table("prompt_versions")
    op.drop_index(op.f("ix_feature_flags_created_at"), table_name="feature_flags")
    op.drop_table("feature_flags")
    op.drop_index(op.f("ix_platform_settings_created_at"), table_name="platform_settings")
    op.drop_index("ix_platform_settings_namespace", table_name="platform_settings")
    op.drop_table("platform_settings")
