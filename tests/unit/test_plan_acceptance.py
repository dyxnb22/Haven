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


def test_read_evidence_must_postdate_the_edit_it_attests_to() -> None:
    """`read:` 是唯一此前不做新鲜度校验的证据种类。

    只查"路径在不在 files_read 里"意味着：先读文件、再改文件、然后用那次读取作为
    "改完且验收通过"的证据——它证明的其实是改动之前的状态。`diff` 与 `check:` 一向
    要求证据晚于最后一次编辑，`read:` 没有理由例外。
    """
    ctx = context()
    ctx.files_read["parser.py"] = "old"
    ctx.ledger = ctx.ledger.with_edit(EditEvidence(2, "parser.py", "old", "new"))
    step = PlanStep(
        title="Fix", status="done", acceptance=("Parser tests pass",), evidence=("read:parser.py",)
    )
    with pytest.raises(ValueError, match="fresh recorded evidence"):
        normalize_plan(ctx, (step,))

    # 改动之后重新读过：摘要变成编辑的 postimage，证据重新成立。
    ctx.files_read["parser.py"] = "new"
    assert normalize_plan(ctx, (step,))[0].status == "done"

    # 又改了一次，读取再次过期。
    ctx.ledger = ctx.ledger.with_edit(EditEvidence(6, "parser.py", "new", "changed"))
    with pytest.raises(ValueError, match="fresh recorded evidence"):
        normalize_plan(ctx, (step,))


def test_read_evidence_for_an_untouched_file_is_accepted() -> None:
    """没有编辑过的文件，读取就是现状——不该被误判成过期。"""
    ctx = context()
    ctx.files_read["parser.py"] = "current"
    step = PlanStep(
        title="Read", status="done", acceptance=("Understood",), evidence=("read:parser.py",)
    )
    assert normalize_plan(ctx, (step,))[0].status == "done"


def test_read_evidence_for_a_never_read_file_is_rejected() -> None:
    ctx = context()
    step = PlanStep(
        title="Read", status="done", acceptance=("Understood",), evidence=("read:parser.py",)
    )
    with pytest.raises(ValueError, match="fresh recorded evidence"):
        normalize_plan(ctx, (step,))


def test_read_evidence_for_a_file_deleted_after_the_read_is_rejected() -> None:
    """读完之后文件被删掉：那次读取证明的是一个已经不存在的文件。"""
    ctx = context()
    ctx.files_read["parser.py"] = "old"
    ctx.ledger = ctx.ledger.with_edit(EditEvidence(2, "parser.py", "old", ""))
    step = PlanStep(
        title="Remove", status="done", acceptance=("Gone",), evidence=("read:parser.py",)
    )
    with pytest.raises(ValueError, match="fresh recorded evidence"):
        normalize_plan(ctx, (step,))


def test_a_process_write_also_expires_the_read() -> None:
    """进程写入同样进 ledger，因此这条边界覆盖外部改动，不只是 Haven 自己的编辑。"""
    ctx = context()
    ctx.files_read["calc.py"] = "before"
    ctx.ledger = ctx.ledger.with_edit(EditEvidence(3, "calc.py", "before", "after-process"))
    step = PlanStep(title="Run", status="done", acceptance=("Passes",), evidence=("read:calc.py",))
    with pytest.raises(ValueError, match="fresh recorded evidence"):
        normalize_plan(ctx, (step,))
