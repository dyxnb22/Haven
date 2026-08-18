"""运行的硬性预算和用量统计。

代理循环会在每一步之前检查预算；用量是不可变账本，因此每次计费都会产生新值，
并且可以原样写入检查点。
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from haven.domain.enums import StopReason


@dataclass(frozen=True, slots=True)
class Budget:
    #: 该上限应能支撑探索以及约 3 轮“修复-验证”。最短的成功路径是
    #: read/edit/create/diff/check/answer；每次检查失败还会额外消耗一轮
    #: fix/diff/check。推导过程见 ADR 0006；这些是工程默认值，并非由评估
    #: 数据求出的最优值。
    max_steps: int = 24
    max_tool_calls: int = 48
    max_wall_time_seconds: float = 600.0
    max_input_tokens: int = 400_000
    max_output_tokens: int = 64_000
    max_cost_usd: float = 2.0

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
    steps: int = 0
    tool_calls: int = 0
    wall_time_seconds: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cost_usd: float = 0.0
    usage_estimated: bool = False

    def charge_step(self) -> BudgetUsage:
        return replace(self, steps=self.steps + 1)

    def charge_tool_call(self) -> BudgetUsage:
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
