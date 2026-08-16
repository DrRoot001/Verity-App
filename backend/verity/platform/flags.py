"""Feature flags (PRD §58, §22.3).

Evaluated server-side and exposed to clients in a bootstrap payload. Targeting
supports global toggle, percentage rollout, plan, user, platform and minimum app
version. Percentage bucketing is deterministic per (flag, user) so a user's
experience does not flicker between requests.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class FlagRollout:
    percentage: int | None = None
    plans: frozenset[str] = field(default_factory=frozenset)
    user_ids: frozenset[str] = field(default_factory=frozenset)
    platforms: frozenset[str] = field(default_factory=frozenset)
    min_app_version: str | None = None

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> FlagRollout:
        return cls(
            percentage=raw.get("percentage"),
            plans=frozenset(raw.get("plans") or ()),
            user_ids=frozenset(raw.get("user_ids") or ()),
            platforms=frozenset(raw.get("platforms") or ()),
            min_app_version=raw.get("min_app_version"),
        )


@dataclass(frozen=True, slots=True)
class FlagDefinition:
    key: str
    enabled: bool
    rollout: FlagRollout = field(default_factory=FlagRollout)


@dataclass(frozen=True, slots=True)
class EvaluationContext:
    user_id: str | None = None
    plan_id: str | None = None
    platform: str | None = None
    app_version: str | None = None


def _parse_version(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in version.split(".")[:4]:
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def _bucket(flag_key: str, user_id: str) -> int:
    """Stable 0-99 bucket. Salted by flag key so rollouts are independent."""
    digest = hashlib.blake2b(f"{flag_key}:{user_id}".encode(), digest_size=4).digest()
    return int.from_bytes(digest, "big") % 100


def evaluate(flag: FlagDefinition, ctx: EvaluationContext) -> bool:
    """Resolve a flag for one context.

    Explicit user targeting wins over every other rule so support can enable a
    feature for one account without touching the rollout percentage.
    """
    if ctx.user_id and ctx.user_id in flag.rollout.user_ids:
        return True

    if not flag.enabled:
        return False

    rollout = flag.rollout

    if rollout.platforms and (ctx.platform or "") not in rollout.platforms:
        return False

    if rollout.plans and (ctx.plan_id or "") not in rollout.plans:
        return False

    if rollout.min_app_version:
        if not ctx.app_version:
            return False
        if _parse_version(ctx.app_version) < _parse_version(rollout.min_app_version):
            return False

    if rollout.percentage is not None:
        if rollout.percentage >= 100:
            return True
        if rollout.percentage <= 0:
            return False
        if not ctx.user_id:
            return False
        return _bucket(flag.key, ctx.user_id) < rollout.percentage

    return True


class FlagRegistry:
    """In-memory view of the ``feature_flags`` table, refreshed by the API layer."""

    def __init__(self, definitions: dict[str, FlagDefinition] | None = None) -> None:
        self._definitions: dict[str, FlagDefinition] = definitions or {}

    def replace_all(self, definitions: dict[str, FlagDefinition]) -> None:
        self._definitions = definitions

    def is_enabled(self, key: str, ctx: EvaluationContext) -> bool:
        definition = self._definitions.get(key)
        if definition is None:
            return False  # Unknown flags are off — fail closed.
        return evaluate(definition, ctx)

    def evaluate_all(self, ctx: EvaluationContext) -> dict[str, bool]:
        return {key: evaluate(d, ctx) for key, d in self._definitions.items()}


registry = FlagRegistry()
