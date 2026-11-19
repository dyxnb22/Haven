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


def test_token_charge_rejects_negative_or_nan() -> None:
    usage = BudgetUsage()
    with pytest.raises(ValueError, match="non-negative and finite"):
        usage.charge_tokens(-1, 10, 0.01, estimated=False)
    with pytest.raises(ValueError, match="non-negative and finite"):
        usage.charge_tokens(10, -1, 0.01, estimated=False)
    with pytest.raises(ValueError, match="non-negative and finite"):
        usage.charge_tokens(10, 10, -0.01, estimated=False)
    with pytest.raises(ValueError, match="non-negative and finite"):
        usage.charge_tokens(10, 10, 0.01, estimated=False, cached_input_tokens=-5)
    with pytest.raises(ValueError, match="non-negative and finite"):
        usage.charge_tokens(10, 10, float("nan"), estimated=False)


class TestInvariantsThatHadNoTest:
    """三条此前无人把守的不变量。

    全部是用变异测试发现的：把每条改坏，14 条既有测试依然全绿。
    """

    def test_cached_tokens_cannot_exceed_total_input(self) -> None:
        """缓存命中是输入的一部分，不可能比总量还多。

        少了这条校验，`cache_hit_rate` 会算出大于 1 的比例——一个把"命中了一半"
        和"命中了三次"混为一谈的数字。
        """
        with pytest.raises(ValueError, match="cached input tokens cannot exceed"):
            BudgetUsage(input_tokens=10, cached_input_tokens=11)
        # 相等是合法的：全部命中。
        assert BudgetUsage(input_tokens=10, cached_input_tokens=10).cache_hit_rate == 1.0

    def test_a_charge_can_also_over_report_the_cache(self) -> None:
        """构造函数的校验拦得住直接构造，拦不住 charge_tokens 的增量。"""
        with pytest.raises(ValueError, match="cached input tokens cannot exceed"):
            BudgetUsage().charge_tokens(10, 5, 0.01, estimated=False, cached_input_tokens=11)

    def test_an_unknown_cost_survives_a_later_known_call(self) -> None:
        """一次没有费率卡的调用不能被后面有费率卡的调用洗成"已知"。

        否则运行里只要混过一次未定价模型，最终成本就会被报成已知——而那个数字
        其实漏掉了一部分，读者无从知道。
        """
        usage = BudgetUsage().charge_tokens(100, 50, 0.0, estimated=False, cost_known=False)
        assert usage.cost_known is False
        # 之后来一次完全已知的调用。
        usage = usage.charge_tokens(10, 5, 0.002, estimated=False, cost_known=True)
        assert usage.cost_known is False, "未知部分不能被后续已知调用抹掉"

    def test_budget_exhaustion_names_one_deterministic_reason(self) -> None:
        """顺序固定，因此多个上限同时耗尽时报告的原因是可预期的。

        文档承诺"报告始终只会指出一个确定性的原因"；没有这条测试，把顺序倒过来
        同样全绿——而同一个悬崖边上的运行会开始报不同的停止原因。
        """
        budget = Budget(max_steps=2, max_tool_calls=2, max_input_tokens=2, max_output_tokens=2)
        usage = BudgetUsage(steps=9, tool_calls=9, input_tokens=9, output_tokens=9, cost_usd=99.0)
        assert check_budget(budget, usage) is StopReason.STEP_BUDGET_EXHAUSTED

    def test_the_accumulated_check_does_not_re_judge_steps(self) -> None:
        """`check_accumulated_budget` 只判定会增长的累计资源。

        步骤与工具上限由循环顶部的 `check_budget` 负责；在这里重复判定会让同一个
        悬崖出现两条代码路径，而它们返回的理由必须一致才不会引人误解。
        """
        budget = Budget(max_steps=2)
        spent = BudgetUsage(steps=9)
        assert check_accumulated_budget(budget, spent) is None
        assert check_budget(budget, spent) is StopReason.STEP_BUDGET_EXHAUSTED
