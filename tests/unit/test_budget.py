from haven.domain import Budget, BudgetUsage, StopReason, check_budget
from haven.domain.budget import BUDGET_TIERS, DEFAULT_TIER


class TestTiers:
    def test_standard_is_the_default_and_unchanged(self) -> None:
        """Adding tiers must not silently move the existing behavior."""
        assert DEFAULT_TIER == "standard"
        assert BUDGET_TIERS["standard"] == Budget()

    def test_quick_is_tighter_and_deep_is_looser(self) -> None:
        quick, standard, deep = (BUDGET_TIERS[name] for name in ("quick", "standard", "deep"))
        assert quick.max_steps < standard.max_steps < deep.max_steps
        assert quick.max_cost_usd < standard.max_cost_usd < deep.max_cost_usd

    def test_every_tier_has_matching_tool_headroom(self) -> None:
        """A step budget the tool budget cannot serve would end runs early for
        the wrong reason."""
        for name, budget in BUDGET_TIERS.items():
            assert budget.max_tool_calls >= budget.max_steps, name

    def test_a_project_can_still_only_tighten_a_tier(self) -> None:
        deep = BUDGET_TIERS["deep"]
        greedy = Budget(max_steps=10_000, max_cost_usd=999.0)
        assert deep.tightened(greedy).max_steps == deep.max_steps
        assert deep.tightened(greedy).max_cost_usd == deep.max_cost_usd


def test_fresh_usage_within_budget() -> None:
    assert check_budget(Budget(), BudgetUsage()) is None


def test_step_budget_exhausted() -> None:
    usage = BudgetUsage(steps=12)
    assert check_budget(Budget(max_steps=12), usage) is StopReason.STEP_BUDGET_EXHAUSTED


def test_tool_budget_exhausted() -> None:
    usage = BudgetUsage(tool_calls=Budget().max_tool_calls)
    assert check_budget(Budget(), usage) is StopReason.TOOL_BUDGET_EXHAUSTED


def test_defaults_leave_room_for_retries() -> None:
    """ADR 0006: the floor is read/edit/create/diff/check/answer plus retries."""
    budget = Budget()
    minimum_trajectory = 6
    per_retry = 3
    assert budget.max_steps >= minimum_trajectory + 3 * per_retry
    assert budget.max_tool_calls >= budget.max_steps


def test_wall_time_exhausted() -> None:
    usage = BudgetUsage(wall_time_seconds=601)
    assert check_budget(Budget(), usage) is StopReason.WALL_TIME_EXHAUSTED


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
    # once estimated, stays estimated
    usage = usage.charge_tokens(10, 5, 0.0001, estimated=False)
    assert usage.usage_estimated is True


def test_project_budget_can_only_tighten() -> None:
    user = Budget(max_steps=12, max_cost_usd=2.0)
    project = Budget(max_steps=40, max_cost_usd=0.5)
    merged = user.tightened(project)
    assert merged.max_steps == 12  # project cannot raise
    assert merged.max_cost_usd == 0.5  # project may lower
