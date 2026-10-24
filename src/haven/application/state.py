"""由应用层拥有的、每次运行可变的工作状态。

这就是 *State*：运行所知道的内容。它不是 Context（模型本轮看到的内容），不是
Trace（日志记录的内容），也不是 ModelResult（模型刚刚返回的内容）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from haven.contracts.capability import CapabilitySnapshot
from haven.contracts.model import ModelMessage
from haven.contracts.tools import PlanStep
from haven.domain.budget import Budget, BudgetUsage
from haven.domain.enums import PermissionMode, RunStatus
from haven.domain.evidence import EvidenceLedger
from haven.domain.ids import RunId
from haven.domain.transitions import transition


@dataclass(slots=True)
class RunContext:
    """一次运行的内存状态：循环推进它，检查点负责保存它的可恢复部分。"""

    #: 本次运行的稳定标识，用于事件、数据库记录和检查点关联。
    run_id: RunId
    #: 用户要求代理完成的自然语言目标。
    goal: str
    #: 本次运行的权限模式，决定副作用工具是允许、询问还是拒绝。
    mode: PermissionMode
    #: 不可动态提高的硬资源上限。
    budget: Budget
    #: 已消耗的资源账本快照。
    usage: BudgetUsage = field(default_factory=BudgetUsage)
    #: 当前运行状态，由领域状态机验证状态转换。
    status: RunStatus = RunStatus.CREATED
    #: 已发送给模型的对话消息，包含用户、助手和工具消息。
    transcript: list[ModelMessage] = field(default_factory=list)
    #: 本次运行已收集的文件和检查证据。
    ledger: EvidenceLedger = field(default_factory=EvidenceLedger)
    #: 规范化路径 -> 本次运行中最近一次读取时的内容摘要
    files_read: dict[str, str] = field(default_factory=dict)
    #: 代理当前的计划。它位于 State 而不是 transcript 中，因此每轮都会重新
    #: 渲染到 Context，不会被截断丢弃。
    plan: tuple[PlanStep, ...] = ()
    #: 本次运行已授权的常驻审批：授权键 -> 请求它的技能名，或 ADR 0025 的哨兵值
    #: （ADR 0025 推广到 ADR 0032）。命中时带着日志记录自动批准，而不会再次询问。
    #: 它仅在本次运行的内存范围内有效——特意不写入 checkpoint，因此恢复后的运行
    #: 在重新启用前必须再次询问；一次恶意仓库也无法借此获得持久授权。
    standing_grants: dict[str, str] = field(default_factory=dict)
    #: 被选中技能请求常驻授权的工具 -> 请求它的技能名（ADR 0032）。出现在这里
    #: 的工具在本次运行内免审；不在这里的工具永远免审不了，因此技能文本无法
    #: 自行扩大权限。它同样只在本次运行的内存里有效——不进 checkpoint、不跨
    #: 进程，所以一次恶意仓库无法借此获得持久授权。
    granted_tools: dict[str, str] = field(default_factory=dict)
    #: 尚未消费的用户引导/转向提示数量。
    nudges: int = 0
    #: 最后写入事件的序号，用于恢复时继续单调递增的事件流。
    last_seq: int = 0
    #: 本次运行冻结的能力快照与摘要（ADR 规划书 P2）。
    catalog_digest: str = ""
    capabilities: CapabilitySnapshot | None = None
    #: 父运行与根运行关系（ADR 规划书 Phase P5 子 Agent）。
    parent_run_id: str | None = None
    root_run_id: str | None = None
    #: 以下两项仅用于当前进程的单调时钟，不写入检查点；恢复时会从 usage 中
    #: 已保存的累计墙钟重新起算。
    _timing_started_at: float | None = field(default=None, repr=False)
    _timing_base: float = field(default=0.0, repr=False)

    def move_to(self, target: RunStatus) -> None:
        """通过领域状态机移动状态，拒绝未声明的转换。"""
        self.status = transition(self.status, target)

    def start_timing(self, now: float) -> None:
        """从已有累计用量继续启动当前进程的单调墙钟。"""
        self._timing_base = self.usage.wall_time_seconds
        self._timing_started_at = now

    def refresh_wall_time(self, now: float) -> None:
        """把当前单调时钟差写回不可变用量快照。"""
        if self._timing_started_at is None:
            return
        elapsed = max(0.0, now - self._timing_started_at)
        self.usage = self.usage.with_wall_time(self._timing_base + elapsed)
