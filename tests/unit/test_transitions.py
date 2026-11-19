import pytest

from haven.domain import InvalidTransitionError, RunStatus, transition


def test_normal_flow() -> None:
    s = RunStatus.CREATED
    for target in (
        RunStatus.RUNNING_MODEL,
        RunStatus.VALIDATING_TOOL,
        RunStatus.WAITING_APPROVAL,
        RunStatus.EXECUTING_TOOL,
        RunStatus.RUNNING_MODEL,
        RunStatus.VERIFYING,
        RunStatus.SUCCEEDED,
    ):
        s = transition(s, target)
    assert s is RunStatus.SUCCEEDED


def test_any_active_state_can_cancel() -> None:
    for status in (
        RunStatus.RUNNING_MODEL,
        RunStatus.WAITING_APPROVAL,
        RunStatus.EXECUTING_TOOL,
        RunStatus.VERIFYING,
    ):
        assert transition(status, RunStatus.CANCELLED) is RunStatus.CANCELLED


def test_terminal_states_are_final() -> None:
    for terminal in (
        RunStatus.SUCCEEDED,
        RunStatus.FAILED,
        RunStatus.STOPPED,
        RunStatus.CANCELLED,
    ):
        with pytest.raises(InvalidTransitionError):
            transition(terminal, RunStatus.RUNNING_MODEL)


def test_effect_unknown_requires_reconciliation() -> None:
    # 工具执行过程中发生崩溃，结果存在歧义
    s = transition(RunStatus.EXECUTING_TOOL, RunStatus.EFFECT_UNKNOWN)
    # reconciled -> 可以继续；abandoned -> 失败
    assert transition(s, RunStatus.RUNNING_MODEL) is RunStatus.RUNNING_MODEL
    assert transition(s, RunStatus.FAILED) is RunStatus.FAILED


def test_cannot_skip_validation() -> None:
    with pytest.raises(InvalidTransitionError):
        transition(RunStatus.RUNNING_MODEL, RunStatus.EXECUTING_TOOL)


#: 完整的状态转换表。任何一条被加宽，测试就会红——这是故意的：这张表就是
#: "什么情况下一次运行算成功"的契约，改它必须是显式的动作。
_EXPECTED: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.CREATED: frozenset({RunStatus.RUNNING_MODEL}),
    RunStatus.RUNNING_MODEL: frozenset(
        {RunStatus.VALIDATING_TOOL, RunStatus.VERIFYING, RunStatus.FAILED}
    ),
    RunStatus.VALIDATING_TOOL: frozenset(
        {RunStatus.RUNNING_MODEL, RunStatus.EXECUTING_TOOL, RunStatus.WAITING_APPROVAL}
    ),
    RunStatus.WAITING_APPROVAL: frozenset({RunStatus.RUNNING_MODEL, RunStatus.EXECUTING_TOOL}),
    RunStatus.EXECUTING_TOOL: frozenset(
        {
            RunStatus.RUNNING_MODEL,
            RunStatus.WAITING_USER,
            RunStatus.FAILED,
            RunStatus.EFFECT_UNKNOWN,
        }
    ),
    RunStatus.WAITING_USER: frozenset({RunStatus.RUNNING_MODEL}),
    RunStatus.VERIFYING: frozenset(
        {RunStatus.SUCCEEDED, RunStatus.STOPPED, RunStatus.RUNNING_MODEL}
    ),
    RunStatus.SUCCEEDED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.STOPPED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
    RunStatus.EFFECT_UNKNOWN: frozenset({RunStatus.RUNNING_MODEL, RunStatus.FAILED}),
}

_TERMINALS = (RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.STOPPED, RunStatus.CANCELLED)


def test_the_whole_table_is_pinned() -> None:
    """整张转换表逐格钉住。

    此前只测了"允许的走得通、几个典型的不许走"。变异测试暴露出三条被漏掉的加宽：
    EFFECT_UNKNOWN 可直进终态、VALIDATING_TOOL 可跳过 VERIFYING、CREATED 可直接
    成功——三条都让 5 条既有测试全绿。
    """
    from haven.domain.transitions import _ALLOWED

    assert dict(_ALLOWED) == {status: targets for status, targets in _EXPECTED.items()}


def test_an_undetermined_side_effect_cannot_be_marked_successful() -> None:
    """无法证明的副作用必须经人工调和，不能被判成成功。

    这是整个 EFFECT_UNKNOWN 机制存在的理由：直接放行 SUCCEEDED 等于让一次
    "我们不知道磁盘发生了什么"的运行盖上成功的章。
    """
    # `FAILED` 是唯一例外——那是"放弃"的正当出口，人决定不再追究。
    for terminal in (RunStatus.SUCCEEDED, RunStatus.STOPPED, RunStatus.CANCELLED):
        with pytest.raises(InvalidTransitionError):
            transition(RunStatus.EFFECT_UNKNOWN, terminal)


def test_verification_cannot_be_skipped() -> None:
    """VALIDATING_TOOL 到不了 SUCCEEDED：证据门禁的前置条件。

    工具刚通过校验、还没执行，此时没有任何"写入之后有通过的检查"可谈。
    """
    with pytest.raises(InvalidTransitionError):
        transition(RunStatus.VALIDATING_TOOL, RunStatus.SUCCEEDED)


def test_a_run_that_never_started_cannot_succeed() -> None:
    with pytest.raises(InvalidTransitionError):
        transition(RunStatus.CREATED, RunStatus.SUCCEEDED)
    with pytest.raises(InvalidTransitionError):
        transition(RunStatus.CREATED, RunStatus.FAILED)
