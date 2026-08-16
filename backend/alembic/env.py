"""Alembic environment (PRD §22.1: forward-only, expand/contract migrations)."""

from __future__ import annotations

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from verity.platform.config import settings
from verity.platform.db.base import Base
from verity.platform.db.registry import import_all_models

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Credentials come from the environment, never from alembic.ini.
config.set_main_option("sqlalchemy.url", settings.database_url_sync)

# Importing every model module populates Base.metadata for autogenerate.
import_all_models()
target_metadata = Base.metadata


def _include_object(
    obj: object, name: str | None, type_: str, reflected: bool, compare_to: object
) -> bool:
    """Skip objects Alembic must not manage.

    Declarative partitions of ``usage_events``/``transcript_segments`` are
    created and dropped by the partition-management job (PRD §27.2), not by
    migrations.
    """
    is_managed_partition = (
        type_ == "table"
        and bool(name)
        and ("_y20" in (name or "") or (name or "").endswith("_default"))
    )
    return not is_managed_partition


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url_sync,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            include_object=_include_object,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
