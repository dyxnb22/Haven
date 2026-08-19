"""运行的硬性预算和用量统计。

代理循环会在每一步之前检查预算；用量是不可变账本，因此每次计费都会产生新值，
并且可以原样写入检查点。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

from haven.domain.enums import StopReason


@dataclass(frozen=True, slots=True)
class Budget:
    """一次运行允许消耗的资源上限；所有字段都是硬限制。"""

    #: 该上限应能支撑探索以及约 3 轮“修复-验证”。最短的成功路径是
    #: read/edit/create/diff/check/answer；每次检查失败还会额外消耗一轮
    #: fix/diff/check。推导过程见 ADR 0006；这些是工程默认值，并非由评估
    #: 数据求出的最优值。
    #: 代理循环已完成的模型交互轮数上限。
    max_steps: int = 24
    #: 所有工具调用的总次数上限（包括同一轮中的多个调用）。
    max_tool_calls: int = 48
    #: 从运行开始计算的墙上时钟秒数上限。
    max_wall_time_seconds: float = 600.0
    #: 未命中缓存的输入与缓存输入合计 token 上限。
    max_input_tokens: int = 400_000
    #: 模型生成的输出 token 上限。
    max_output_tokens: int = 64_000
    #: 本次运行允许产生的模型费用上限，单位为美元。
    max_cost_usd: float = 2.0

    def __post_init__(self) -> None:
        """硬上限必须为有限正数；零、负数和 NaN 都会破坏停止语义。"""
        integer_limits = (
            self.max_steps,
            self.max_tool_calls,
            self.max_input_tokens,
            self.max_output_tokens,
        )
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value <= 0
            for value in integer_limits
        ):
            raise ValueError("integer budget limits must be greater than zero")
        numeric_limits = (self.max_wall_time_seconds, self.max_cost_usd)
        if any(
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(value)
            or value <= 0
            for value in numeric_limits
        ):
            raise ValueError("numeric budget limits must be finite and greater than zero")

    def tightened(self, other: Budget) -> Budget:
        """与另一个预算合并，并为每个字段保留更严格的限制。

        用于项目级配置：仓库可以降低预算，但绝不能将其提高到用户级值之上。
        """
        return Budget(
            max_steps=min(self.max_steps, other.max_steps),
            max_tool_calls=min(self.max_tool_calls, other.max_tool_calls),
            max_wall_time_seconds=min(self.max_wall_time_seconds, other.max_wall_time_seconds),
            max_input_tokens=min(self.max_input_tokens, other.max_input_tokens),
            max_output_tokens=min(self.max_output_tokens, other.max_output_tokens),
            max_cost_usd=min(self.max_cost_usd, other.max_cost_usd),
        )

    #: 用户在 CLI 中选择的命名预设。它们以数据形式定义在这里，使上限在
    #: 程序中保持为常量：运行只能选择一个档位，不能自行发明档位。
    #: `standard` 是历史默认值，并且特意与原默认值完全一致。


BUDGET_TIERS: dict[str, Budget] = {
    "quick": Budget(
        max_steps=8,
        max_tool_calls=16,
        max_wall_time_seconds=180.0,
        max_cost_usd=0.5,
    ),
    "standard": Budget(),
    "deep": Budget(
        max_steps=80,
        max_tool_calls=160,
        max_wall_time_seconds=1800.0,
        max_cost_usd=5.0,
    ),
}

DEFAULT_TIER = "standard"


@dataclass(frozen=True, slots=True)
class BudgetUsage:
    """一次运行已经消耗的资源快照，以不可变值逐次更新。"""

    #: 已完成的模型交互轮数。
    steps: int = 0
    #: 已执行的工具调用次数。
    tool_calls: int = 0
    #: 从运行开始累计的墙上时钟秒数。
    wall_time_seconds: float = 0.0
    #: 输入 token 总数，包含缓存命中部分。
    input_tokens: int = 0
    #: 输出 token 总数。
    output_tokens: int = 0
    #: 输入 token 中由提供商缓存命中的数量。
    cached_input_tokens: int = 0
    #: 按当前 pricing 估算或累计的费用，单位为美元。
    cost_usd: float = 0.0
    #: 任一用量或费用是否来自估算，而非提供商的精确计数。
    usage_estimated: bool = False

    def __post_init__(self) -> None:
        counters = (
            self.steps,
            self.tool_calls,
            self.input_tokens,
            self.output_tokens,
            self.cached_input_tokens,
        )
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in counters
        ):
            raise ValueError("usage counters must be non-negative integers")
        if self.cached_input_tokens > self.input_tokens:
            raise ValueError("cached input tokens cannot exceed total input tokens")
        if (
            isinstance(self.wall_time_seconds, bool)
            or not isinstance(self.wall_time_seconds, int | float)
            or not math.isfinite(self.wall_time_seconds)
            or self.wall_time_seconds < 0
            or isinstance(self.cost_usd, bool)
            or not isinstance(self.cost_usd, int | float)
            or not math.isfinite(self.cost_usd)
            or self.cost_usd < 0
        ):
            raise ValueError("usage time and cost must be finite and non-negative")

    def charge_step(self) -> BudgetUsage:
        """记录完成一轮模型交互。"""
        return replace(self, steps=self.steps + 1)

    def charge_tool_call(self) -> BudgetUsage:
        """记录一次工具调用。"""
        return replace(self, tool_calls=self.tool_calls + 1)

    def charge_tokens(
        self,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        *,
        estimated: bool,
        cached_input_tokens: int = 0,
    ) -> BudgetUsage:
        """累计 token、缓存命中量和费用，并传播估算标记。"""
        return replace(
            self,
            input_tokens=self.input_tokens + input_tokens,
            output_tokens=self.output_tokens + output_tokens,
            cached_input_tokens=self.cached_input_tokens + cached_input_tokens,
            cost_usd=self.cost_usd + cost_usd,
            usage_estimated=self.usage_estimated or estimated,
        )

    @property
    def cache_hit_rate(self) -> float:
        """整个运行期间由缓存提供的输入 token 占比。"""
        return self.cached_input_tokens / self.input_tokens if self.input_tokens else 0.0

    def with_wall_time(self, seconds: float) -> BudgetUsage:
        """用当前运行墙上时钟读数生成新的用量快照。"""
        return replace(self, wall_time_seconds=seconds)


def check_budget(budget: Budget, usage: BudgetUsage) -> StopReason | None:
    """如果任一预算耗尽则返回停止原因，否则返回 None。

    检查顺序固定，因此报告始终只会指出一个确定性的原因。
    """
    if usage.steps >= budget.max_steps:
        return StopReason.STEP_BUDGET_EXHAUSTED
    if usage.tool_calls >= budget.max_tool_calls:
        return StopReason.TOOL_BUDGET_EXHAUSTED
    if usage.wall_time_seconds >= budget.max_wall_time_seconds:
        return StopReason.WALL_TIME_EXHAUSTED
    if usage.input_tokens >= budget.max_input_tokens:
        return StopReason.TOKEN_BUDGET_EXHAUSTED
    if usage.output_tokens >= budget.max_output_tokens:
        return StopReason.TOKEN_BUDGET_EXHAUSTED
    if usage.cost_usd >= budget.max_cost_usd:
        return StopReason.COST_BUDGET_EXHAUSTED
    return None


def check_accumulated_budget(budget: Budget, usage: BudgetUsage) -> StopReason | None:
    """在一次模型调用完成后检查会增长的累计资源，不重复判定步骤/工具上限。"""
    if usage.wall_time_seconds >= budget.max_wall_time_seconds:
        return StopReason.WALL_TIME_EXHAUSTED
    if (
        usage.input_tokens >= budget.max_input_tokens
        or usage.output_tokens >= budget.max_output_tokens
    ):
        return StopReason.TOKEN_BUDGET_EXHAUSTED
    if usage.cost_usd >= budget.max_cost_usd:
        return StopReason.COST_BUDGET_EXHAUSTED
    return None
