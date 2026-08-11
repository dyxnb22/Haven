"""Run status state machine.

Every transition goes through `transition()` so an illegal move is a bug that
fails loudly instead of silently corrupting a run.
"""

from __future__ import annotations

from haven.domain.enums import ACTIVE_STATUSES, RunStatus


class InvalidTransitionError(Exception):
    def __init__(self, current: RunStatus, target: RunStatus) -> None:
        super().__init__(f"illegal run transition: {current} -> {target}")
        self.current = current
        self.target = target


_ALLOWED: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.CREATED: frozenset({RunStatus.RUNNING_MODEL}),
    RunStatus.RUNNING_MODEL: frozenset(
        {RunStatus.VALIDATING_TOOL, RunStatus.VERIFYING, RunStatus.FAILED}
    ),
    RunStatus.VALIDATING_TOOL: frozenset(
        {
            RunStatus.RUNNING_MODEL,  # deny / validation error is fed back to the model
            RunStatus.EXECUTING_TOOL,
            RunStatus.WAITING_APPROVAL,
        }
    ),
    RunStatus.WAITING_APPROVAL: frozenset(
        {
            RunStatus.RUNNING_MODEL,  # rejected
            RunStatus.EXECUTING_TOOL,  # approved
        }
    ),
    RunStatus.EXECUTING_TOOL: frozenset(
        {
            RunStatus.RUNNING_MODEL,
            RunStatus.FAILED,
            RunStatus.EFFECT_UNKNOWN,
        }
    ),
    RunStatus.VERIFYING: frozenset(
        {RunStatus.SUCCEEDED, RunStatus.STOPPED, RunStatus.RUNNING_MODEL}
    ),
    RunStatus.SUCCEEDED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.STOPPED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
    RunStatus.EFFECT_UNKNOWN: frozenset(
        {
            RunStatus.RUNNING_MODEL,  # reconciled, run may continue
            RunStatus.FAILED,  # abandoned
        }
    ),
}


def transition(current: RunStatus, target: RunStatus) -> RunStatus:
    """Validate and return the target status.

    Any active status may always move to CANCELLED or STOPPED (budget or
    other program-side halt); everything else must be explicitly allowed.
    """
    if current in ACTIVE_STATUSES and target in (RunStatus.CANCELLED, RunStatus.STOPPED):
        return target
    if target in _ALLOWED.get(current, frozenset()):
        return target
    raise InvalidTransitionError(current, target)
