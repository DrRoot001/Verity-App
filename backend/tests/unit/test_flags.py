"""Feature flag evaluation (PRD §58)."""

from __future__ import annotations

from verity.platform.flags import (
    EvaluationContext,
    FlagDefinition,
    FlagRegistry,
    FlagRollout,
    evaluate,
)


def test_disabled_flag_is_off() -> None:
    flag = FlagDefinition(key="coding_engine", enabled=False)
    assert evaluate(flag, EvaluationContext(user_id="u1")) is False


def test_enabled_flag_with_no_rollout_is_on() -> None:
    flag = FlagDefinition(key="coding_engine", enabled=True)
    assert evaluate(flag, EvaluationContext(user_id="u1")) is True


def test_explicit_user_targeting_overrides_disabled_flag() -> None:
    """Support must be able to enable a feature for one account."""
    flag = FlagDefinition(
        key="screen_context", enabled=False, rollout=FlagRollout(user_ids=frozenset({"u1"}))
    )
    assert evaluate(flag, EvaluationContext(user_id="u1")) is True
    assert evaluate(flag, EvaluationContext(user_id="u2")) is False


def test_percentage_rollout_is_deterministic_per_user() -> None:
    flag = FlagDefinition(key="detector_v2", enabled=True, rollout=FlagRollout(percentage=50))
    ctx = EvaluationContext(user_id="stable-user")
    first = evaluate(flag, ctx)
    assert all(evaluate(flag, ctx) is first for _ in range(50))


def test_percentage_rollout_splits_population() -> None:
    flag = FlagDefinition(key="detector_v2", enabled=True, rollout=FlagRollout(percentage=50))
    enabled = sum(evaluate(flag, EvaluationContext(user_id=f"user-{i}")) for i in range(1000))
    assert 400 < enabled < 600  # ~50% with sampling tolerance


def test_percentage_rollout_is_independent_across_flags() -> None:
    """A user in bucket 5 for one flag must not be in bucket 5 for every flag."""
    ctx = EvaluationContext(user_id="user-42")
    a = FlagDefinition(key="flag_a", enabled=True, rollout=FlagRollout(percentage=50))
    b = FlagDefinition(key="flag_b", enabled=True, rollout=FlagRollout(percentage=50))
    results = {
        evaluate(
            FlagDefinition(key=f"flag_{i}", enabled=True, rollout=FlagRollout(percentage=50)),
            ctx,
        )
        for i in range(20)
    }
    assert results == {True, False}
    assert isinstance(evaluate(a, ctx), bool)
    assert isinstance(evaluate(b, ctx), bool)


def test_plan_targeting() -> None:
    flag = FlagDefinition(
        key="advanced_reports", enabled=True, rollout=FlagRollout(plans=frozenset({"pro", "max"}))
    )
    assert evaluate(flag, EvaluationContext(user_id="u", plan_id="pro")) is True
    assert evaluate(flag, EvaluationContext(user_id="u", plan_id="free")) is False


def test_platform_targeting() -> None:
    flag = FlagDefinition(
        key="screen_context", enabled=True, rollout=FlagRollout(platforms=frozenset({"macos"}))
    )
    assert evaluate(flag, EvaluationContext(user_id="u", platform="macos")) is True
    assert evaluate(flag, EvaluationContext(user_id="u", platform="web")) is False


def test_min_app_version_gate() -> None:
    flag = FlagDefinition(key="new_stt", enabled=True, rollout=FlagRollout(min_app_version="1.4.0"))
    assert evaluate(flag, EvaluationContext(user_id="u", app_version="1.4.0")) is True
    assert evaluate(flag, EvaluationContext(user_id="u", app_version="1.10.0")) is True
    assert evaluate(flag, EvaluationContext(user_id="u", app_version="1.3.9")) is False
    assert evaluate(flag, EvaluationContext(user_id="u", app_version=None)) is False


def test_anonymous_context_excluded_from_percentage_rollout() -> None:
    flag = FlagDefinition(key="x", enabled=True, rollout=FlagRollout(percentage=50))
    assert evaluate(flag, EvaluationContext()) is False


def test_unknown_flag_fails_closed() -> None:
    registry = FlagRegistry({})
    assert registry.is_enabled("never_defined", EvaluationContext(user_id="u")) is False
