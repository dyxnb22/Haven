"""运行状态状态机。

每次状态转换都经过 `transition()`，因此非法移动会明确失败，而不是悄悄破坏运行。
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
            RunStatus.RUNNING_MODEL,  # deny / 校验错误会反馈给模型
            RunStatus.EXECUTING_TOOL,
            RunStatus.WAITING_APPROVAL,
        }
    ),
    RunStatus.WAITING_APPROVAL: frozenset(
        {
            RunStatus.RUNNING_MODEL,  # 已拒绝
            RunStatus.EXECUTING_TOOL,  # 已批准
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
            RunStatus.RUNNING_MODEL,  # 已调和，运行可以继续
            RunStatus.FAILED,  # 已放弃
        }
    ),
}


def transition(current: RunStatus, target: RunStatus) -> RunStatus:
    """校验并返回目标状态。

    任何活动状态都可以移动到 CANCELLED 或 STOPPED（预算耗尽或其他程序侧停止）；
    其他转换则必须被明确允许。
    """
    if current in ACTIVE_STATUSES and target in (RunStatus.CANCELLED, RunStatus.STOPPED):
        return target
    if target in _ALLOWED.get(current, frozenset()):
        return target
    raise InvalidTransitionError(current, target)
