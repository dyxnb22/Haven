"""由应用层拥有的、每次运行可变的工作状态。

这就是 *State*：运行所知道的内容。它不是 Context（模型本轮看到的内容），不是
Trace（日志记录的内容），也不是 ModelResult（模型刚刚返回的内容）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from haven.contracts.model import ModelMessage
from haven.contracts.tools import PlanStep
from haven.domain.budget import Budget, BudgetUsage
from haven.domain.enums import PermissionMode, RunStatus
from haven.domain.evidence import EvidenceLedger
from haven.domain.ids import RunId
from haven.domain.transitions import transition


@dataclass(slots=True)
class RunContext:
    run_id: RunId
    goal: str
    mode: PermissionMode
    budget: Budget
    usage: BudgetUsage = field(default_factory=BudgetUsage)
    status: RunStatus = RunStatus.CREATED
    transcript: list[ModelMessage] = field(default_factory=list)
    ledger: EvidenceLedger = field(default_factory=EvidenceLedger)
    #: 规范化路径 -> 本次运行中最近一次读取时的内容摘要
    files_read: dict[str, str] = field(default_factory=dict)
    #: 代理当前的计划。它位于 State 而不是 transcript 中，因此每轮都会重新
    #: 渲染到 Context，不会被截断丢弃。
    plan: tuple[PlanStep, ...] = ()
    #: 本次运行中用户已经批准的 repo.check 调用的审批摘要（ADR 0025）。
    #: 对这类检查进行字节级相同的重跑时，会带着日志记录自动批准，而不会
    #: 再次询问。它仅在本次运行的内存范围内有效——特意不写入 checkpoint，
    #: 因此恢复后的运行在重新启用前必须再次询问。
    standing_check_grants: set[str] = field(default_factory=set)
    nudges: int = 0
    last_seq: int = 0

    def move_to(self, target: RunStatus) -> None:
        self.status = transition(self.status, target)
