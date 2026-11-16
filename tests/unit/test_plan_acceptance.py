import pytest

from haven.application.planning import normalize_plan
from haven.application.state import RunContext
from haven.contracts.tools import PlanStep
from haven.domain.budget import Budget
from haven.domain.enums import PermissionMode
from haven.domain.evidence import CheckEvidence, EditEvidence


def context() -> RunContext:
    return RunContext(
        run_id="plan", goal="Fix the parser", mode=PermissionMode.INTERACTIVE, budget=Budget()
    )


def test_step_ids_survive_reordering_and_new_steps_get_new_ids() -> None:
    ctx = context()
    ctx.plan = normalize_plan(ctx, (PlanStep(title="Read"), PlanStep(title="Fix")))
    plan = normalize_plan(
        ctx, (PlanStep(title="Fix"), PlanStep(title="Read"), PlanStep(title="Test"))
    )
    assert [step.id for step in plan] == ["step-2", "step-1", "step-3"]


@pytest.mark.parametrize(
    "steps",
    [
        (PlanStep(id="a", title="A", depends_on=("missing",)),),
        (
            PlanStep(id="a", title="A", depends_on=("b",)),
            PlanStep(id="b", title="B", depends_on=("a",)),
        ),
        (
            PlanStep(id="a", title="A"),
            PlanStep(id="b", title="B", status="done", depends_on=("a",)),
        ),
        (PlanStep(title="Blocked", status="blocked"),),
        (PlanStep(title="Done", status="done", acceptance=("Test passes",)),),
    ],
)
def test_invalid_dependencies_or_unsupported_completion_are_rejected(
    steps: tuple[PlanStep, ...],
) -> None:
    with pytest.raises(ValueError):
        normalize_plan(context(), steps)


def test_acceptance_uses_real_fresh_evidence() -> None:
    ctx = context()
    ctx.ledger = ctx.ledger.with_edit(EditEvidence(2, "parser.py", "old", "new"))
    step = PlanStep(
        title="Fix", status="done", acceptance=("Parser tests pass",), evidence=("check:parser",)
    )
    with pytest.raises(ValueError, match="fresh recorded evidence"):
        normalize_plan(ctx, (step,))
    ctx.ledger = ctx.ledger.with_check(CheckEvidence(3, "parser", 0, 1, False))
    assert normalize_plan(ctx, (step,))[0].status == "done"
    ctx.ledger = ctx.ledger.with_check(CheckEvidence(4, "parser", 1, 1, False))
    with pytest.raises(ValueError, match="fresh recorded evidence"):
        normalize_plan(ctx, (step,))
    ctx.ledger = ctx.ledger.with_check(CheckEvidence(5, "parser", 0, 1, False))
    ctx.ledger = ctx.ledger.with_edit(EditEvidence(6, "parser.py", "new", "changed"))
    with pytest.raises(ValueError, match="fresh recorded evidence"):
        normalize_plan(ctx, (step,))
