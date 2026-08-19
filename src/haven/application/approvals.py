"""流水线与人工（或测试策略）之间的审批 broker。"""

from __future__ import annotations

import asyncio
from typing import Literal, Protocol

from haven.domain.approval import ApprovalRequest
from haven.domain.enums import ApprovalDecision


class ApprovalResponder(Protocol):
    """交互层审批响应的最小协议。

    回答一次审批请求。由 TUI 桥接、无头 CLI（始终拒绝）和脚本化评估策略实现。
    """

    async def respond(self, request: ApprovalRequest) -> ApprovalDecision:
        """对一个审批请求返回批准或拒绝决定。"""
        ...


class AutoApprover:
    """用于测试和离线评估的确定性审批策略。"""

    def __init__(self, policy: Literal["approve_all", "reject_all"] = "approve_all") -> None:
        self._policy = policy
        self.seen: list[ApprovalRequest] = []

    async def respond(self, request: ApprovalRequest) -> ApprovalDecision:
        """记录请求并按固定策略返回批准或拒绝。"""
        self.seen.append(request)
        if self._policy == "approve_all":
            return ApprovalDecision.APPROVED
        return ApprovalDecision.REJECTED


class HeadlessApprover:
    """用于无头（非交互）运行的自动审批策略。

    无头运行没有可以询问的人工，因此必须在命令行中明确设置策略；每次写入仍会经过
    相同的 Registry -> Policy -> Approval -> Evidence 通道，此处只提供原本应由人工
    给出的 yes/no 决定。

    - ``reject``：拒绝所有操作（运行可以读取和提出方案，但绝不修改）。
    - ``trusted_recipe``：只批准已注册的 ``repo.check`` 配方，使流水线可以无人值守
      验证，但拒绝所有工作区修改。
    - ``all``：批准所有操作（完整的无人值守自动修复）；必须配合明确的 ``--write``，
      以免意外成为默认行为。
    """

    def __init__(self, policy: Literal["reject", "trusted_recipe", "all"]) -> None:
        self._policy = policy
        self.seen: list[ApprovalRequest] = []

    async def respond(self, request: ApprovalRequest) -> ApprovalDecision:
        """记录请求并按无头运行策略决定是否批准。"""
        self.seen.append(request)
        if self._policy == "all":
            return ApprovalDecision.APPROVED
        if self._policy == "trusted_recipe" and request.tool_name == "repo.check":
            return ApprovalDecision.APPROVED
        return ApprovalDecision.REJECTED


class QueueApprovalBroker:
    """将审批桥接到交互式 UI。

    流水线等待 `respond()`；人工做出决定后，UI 通过 `resolve()` 完成待处理的 future。
    取消操作会自然地传播。
    """

    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future[ApprovalDecision]] = {}

    async def respond(self, request: ApprovalRequest) -> ApprovalDecision:
        """挂起当前协程，直到 UI 通过相同审批 ID 调用 ``resolve``。"""
        loop = asyncio.get_running_loop()
        future: asyncio.Future[ApprovalDecision] = loop.create_future()
        self._pending[request.approval_id] = future
        try:
            return await future
        finally:
            self._pending.pop(request.approval_id, None)

    def resolve(self, approval_id: str, decision: ApprovalDecision) -> bool:
        """完成一个待处理审批；找不到或已完成时返回 ``False``。"""
        future = self._pending.get(approval_id)
        if future is None or future.done():
            return False
        future.set_result(decision)
        return True

    @property
    def pending_ids(self) -> tuple[str, ...]:
        """返回当前等待 UI 决定的审批 ID。"""
        return tuple(self._pending)
