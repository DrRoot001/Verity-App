"""Runtime bridge for database-backed admin configuration.

Postgres is the source of truth. Redis is the hot path used by model routing
and feature gates so an admin change takes effect across API workers without a
restart or a database query on every generation.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from verity.ai.prompts import Prompt, PromptStatus, registry
from verity.ai.providers.base import TaskClass
from verity.modules.admin.models import FeatureFlag, PlatformSetting, PromptVersion
from verity.platform.db.session import transaction
from verity.platform.runtime_config import (
    default_ai_settings,
    publish_ai_settings,
    publish_feature_flag,
)


async def publish_flag(flag: FeatureFlag) -> None:
    await publish_feature_flag(flag.key, flag.enabled, flag.rollout_percentage)


def install_prompt(row: PromptVersion) -> None:
    registry.upsert(
        Prompt(
            id=row.prompt_id,
            version=row.version,
            task_class=TaskClass(row.task_class),
            status=PromptStatus(row.status),
            system=row.system_template,
            user_template=row.user_template,
            variables=tuple(str(item) for item in row.variables),
            output_schema=row.output_schema,
            notes=row.notes,
            eval_score=row.eval_score,
        )
    )


async def synchronize_runtime() -> None:
    """Load durable admin state into each process and the shared Redis cache."""
    async with transaction() as db:
        setting = (
            await db.execute(
                select(PlatformSetting).where(
                    PlatformSetting.namespace == "ai", PlatformSetting.key == "gateway"
                )
            )
        ).scalar_one_or_none()
        await publish_ai_settings(setting.value if setting else default_ai_settings())

        flags = list((await db.execute(select(FeatureFlag))).scalars())
        for flag in flags:
            await publish_flag(flag)

        prompts = list((await db.execute(select(PromptVersion))).scalars())
        for prompt in prompts:
            install_prompt(prompt)


async def upsert_default_flags(db: AsyncSession) -> None:
    defaults = {
        "mock_interviews": "Allow candidates to create mock interview sessions.",
        "live_copilot": "Allow candidates to create live copilot sessions.",
        "story_generation": "Allow AI-assisted Story Bank generation.",
        "resume_extraction": "Allow AI-assisted resume extraction.",
    }
    existing = set((await db.execute(select(FeatureFlag.key))).scalars())
    for key, description in defaults.items():
        if key not in existing:
            db.add(FeatureFlag(key=key, description=description, enabled=True))
