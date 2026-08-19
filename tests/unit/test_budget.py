import math

import pytest

from haven.domain import (
    Budget,
    BudgetUsage,
    StopReason,
    check_accumulated_budget,
    check_budget,
)
from haven.domain.budget import BUDGET_TIERS, DEFAULT_TIER


class TestTiers:
    def test_standard_is_the_default_and_unchanged(self) -> None:
        """增加档位不得悄悄改变现有行为。"""
        assert DEFAULT_TIER == "standard"
        assert BUDGET_TIERS["standard"] == Budget()

    def test_quick_is_tighter_and_deep_is_looser(self) -> None:
        quick, standard, deep = (BUDGET_TIERS[name] for name in ("quick", "standard", "deep"))
        assert quick.max_steps < standard.max_steps < deep.max_steps
        assert quick.max_cost_usd < standard.max_cost_usd < deep.max_cost_usd

    def test_every_tier_has_matching_tool_headroom(self) -> None:
        """工具预算无法支撑的步骤预算会让运行因错误原因提前结束。"""
        for name, budget in BUDGET_TIERS.items():
            assert budget.max_tool_calls >= budget.max_steps, name

    def test_a_project_can_still_only_tighten_a_tier(self) -> None:
        deep = BUDGET_TIERS["deep"]
        greedy = Budget(max_steps=10_000, max_cost_usd=999.0)
        assert deep.tightened(greedy).max_steps == deep.max_steps
        assert deep.tightened(greedy).max_cost_usd == deep.max_cost_usd


def test_fresh_usage_within_budget() -> None:
    assert check_budget(Budget(), BudgetUsage()) is None
    for invalid in (
        lambda: Budget(max_steps=1.5),  # type: ignore[arg-type]
        lambda: Budget(max_wall_time_seconds=True),
        lambda: Budget(max_cost_usd=math.nan),
        lambda: BudgetUsage(steps=1.5),  # type: ignore[arg-type]
        lambda: BudgetUsage(cost_usd=math.inf),
    ):
        with pytest.raises(ValueError):
            invalid()


def test_step_budget_exhausted() -> None:
    usage = BudgetUsage(steps=12)
    assert check_budget(Budget(max_steps=12), usage) is StopReason.STEP_BUDGET_EXHAUSTED


def test_tool_budget_exhausted() -> None:
    usage = BudgetUsage(tool_calls=Budget().max_tool_calls)
    assert check_budget(Budget(), usage) is StopReason.TOOL_BUDGET_EXHAUSTED


def test_defaults_leave_room_for_retries() -> None:
    """ADR 0006：最低路径是 read/edit/create/diff/check/answer，再加上重试空间。"""
    budget = Budget()
    minimum_trajectory = 6
    per_retry = 3
    assert budget.max_steps >= minimum_trajectory + 3 * per_retry
    assert budget.max_tool_calls >= budget.max_steps


def test_wall_time_exhausted() -> None:
    usage = BudgetUsage(wall_time_seconds=601)
    assert check_budget(Budget(), usage) is StopReason.WALL_TIME_EXHAUSTED
    assert check_accumulated_budget(Budget(), usage) is StopReason.WALL_TIME_EXHAUSTED


def test_cost_budget_exhausted() -> None:
    usage = BudgetUsage(cost_usd=2.5)
    assert check_budget(Budget(), usage) is StopReason.COST_BUDGET_EXHAUSTED


def test_charges_are_immutable() -> None:
    usage = BudgetUsage()
    charged = usage.charge_step().charge_tool_call()
    assert usage.steps == 0
    assert charged.steps == 1
    assert charged.tool_calls == 1


def test_token_charge_tracks_estimation_flag() -> None:
    usage = BudgetUsage().charge_tokens(100, 50, 0.001, estimated=True)
    assert usage.input_tokens == 100
    assert usage.usage_estimated is True
    # 一旦估算，就保持估算状态
    usage = usage.charge_tokens(10, 5, 0.0001, estimated=False)
    assert usage.usage_estimated is True


def test_project_budget_can_only_tighten() -> None:
    user = Budget(max_steps=12, max_cost_usd=2.0)
    project = Budget(max_steps=40, max_cost_usd=0.5)
    merged = user.tightened(project)
    assert merged.max_steps == 12  # 项目配置不能提高上限
    assert merged.max_cost_usd == 0.5  # 项目配置可以降低上限
