"""Hard budgets and usage accounting for a run.

Budgets are checked by the agent loop before each step; usage is an immutable
ledger so every charge produces a new value and can be checkpointed as-is.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from haven.domain.enums import StopReason


@dataclass(frozen=True, slots=True)
class Budget:
    #: Sized so a run survives exploration plus ~3 fix-verify rounds. The
    #: minimum successful trajectory is read/edit/create/diff/check/answer, and
    #: each failed check costs another fix/diff/check. See ADR 0006 for the
    #: derivation; these are engineering defaults, not eval-derived optima.
    max_steps: int = 24
    max_tool_calls: int = 48
    max_wall_time_seconds: float = 600.0
    max_input_tokens: int = 400_000
    max_output_tokens: int = 64_000
    max_cost_usd: float = 2.0

    def tightened(self, other: Budget) -> Budget:
        """Merge with another budget, keeping the stricter limit of each field.

        Used for project-level config: a repository may lower budgets but can
        never raise them above the user-level values.
        """
        return Budget(
            max_steps=min(self.max_steps, other.max_steps),
            max_tool_calls=min(self.max_tool_calls, other.max_tool_calls),
            max_wall_time_seconds=min(self.max_wall_time_seconds, other.max_wall_time_seconds),
            max_input_tokens=min(self.max_input_tokens, other.max_input_tokens),
            max_output_tokens=min(self.max_output_tokens, other.max_output_tokens),
            max_cost_usd=min(self.max_cost_usd, other.max_cost_usd),
        )


@dataclass(frozen=True, slots=True)
class BudgetUsage:
    steps: int = 0
    tool_calls: int = 0
    wall_time_seconds: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    usage_estimated: bool = False

    def charge_step(self) -> BudgetUsage:
        return replace(self, steps=self.steps + 1)

    def charge_tool_call(self) -> BudgetUsage:
        return replace(self, tool_calls=self.tool_calls + 1)

    def charge_tokens(
        self, input_tokens: int, output_tokens: int, cost_usd: float, *, estimated: bool
    ) -> BudgetUsage:
        return replace(
            self,
            input_tokens=self.input_tokens + input_tokens,
            output_tokens=self.output_tokens + output_tokens,
            cost_usd=self.cost_usd + cost_usd,
            usage_estimated=self.usage_estimated or estimated,
        )

    def with_wall_time(self, seconds: float) -> BudgetUsage:
        return replace(self, wall_time_seconds=seconds)


def check_budget(budget: Budget, usage: BudgetUsage) -> StopReason | None:
    """Return the stop reason if any budget is exhausted, else None.

    Checks are ordered so the report always names one deterministic reason.
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
