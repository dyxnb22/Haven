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
    # crash ambiguity out of tool execution
    s = transition(RunStatus.EXECUTING_TOOL, RunStatus.EFFECT_UNKNOWN)
    # reconciled -> may continue; abandoned -> failed
    assert transition(s, RunStatus.RUNNING_MODEL) is RunStatus.RUNNING_MODEL
    assert transition(s, RunStatus.FAILED) is RunStatus.FAILED


def test_cannot_skip_validation() -> None:
    with pytest.raises(InvalidTransitionError):
        transition(RunStatus.RUNNING_MODEL, RunStatus.EXECUTING_TOOL)
