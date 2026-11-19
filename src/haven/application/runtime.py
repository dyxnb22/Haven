"""HavenRuntime: 面向 CLI、TUI、SDK 和 MCP Server 的统一应用门面。

统一调度主运行循环、会话恢复、事件订阅、审批仲裁与资源生命周期，
对外隐藏 Workspace、Executor 以及底层数据库对象的直接依赖。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from haven.application.approvals import ApprovalResponder
from haven.application.emitter import EventEmitter
from haven.application.recovery_service import RecoveryService
from haven.application.run_persistence import RunOutcome
from haven.application.run_service import RunService, build_run_context_from_checkpoint
from haven.contracts.checkpoint import CheckpointV1
from haven.contracts.user_input import UserInputResponse
from haven.domain.enums import AgentMode, ApprovalDecision, ApprovalMode
from haven.ports.session import RunRecord, SessionStorePort


class HavenRuntime:
    """面向外界调用的统一 Haven 运行时门面。"""

    def __init__(
        self,
        *,
        run_service: RunService,
        store: SessionStorePort,
        emitter: EventEmitter,
        approvals: ApprovalResponder,
        recovery: RecoveryService | None = None,
    ) -> None:
        self._run_service = run_service
        self._store = store
        self._emitter = emitter
        self._approvals = approvals
        self._recovery = recovery

    @classmethod
    def from_services(cls, services: Any) -> HavenRuntime:
        """从已完成组装的应用组合根构造 Runtime 门面。"""
        return cls(
            run_service=services.run_service,
            store=services.store,
            emitter=services.emitter,
            approvals=services.run_service._approvals,
            recovery=services.recovery,
        )

    # -- 服务与端口访问器 -----------------------------------------------------

    @property
    def run_service(self) -> RunService:
        """底层 RunService 实例。"""
        return self._run_service

    @property
    def store(self) -> SessionStorePort:
        """底层会话持久化端口。"""
        return self._store

    @property
    def workspace_digest(self) -> str:
        """本运行时绑定工作区的摘要；存储是全局的，靠它把范围收回来。"""
        return self._run_service.workspace_digest

    @property
    def emitter(self) -> EventEmitter:
        """底层事件发射器。"""
        return self._emitter

    @property
    def approvals(self) -> ApprovalResponder:
        """底层审批仲裁器。"""
        return self._approvals

    @property
    def recovery(self) -> RecoveryService | None:
        """底层崩溃恢复服务（未启用时为 None）。"""
        return self._recovery

    @property
    def active_run_id(self) -> str | None:
        """当前正在驱动的活跃运行 ID。"""
        return self._run_service.active_run_id

    # -- 核心执行接口 ---------------------------------------------------------

    async def start_run(
        self,
        goal: str,
        *,
        skill_names: Sequence[str] = (),
        approval_mode: ApprovalMode | None = None,
        agent_mode: AgentMode = AgentMode.AGENT,
        require_review: bool = False,
        check_group: str | None = None,
        model_preset: str | None = None,
    ) -> RunOutcome:
        """启动新目标运行并等待执行收口。"""
        return await self._run_service.run(
            goal,
            skill_names=skill_names,
            approval_mode=approval_mode,
            agent_mode=agent_mode,
            require_review=require_review,
            check_group=check_group,
            model_preset=model_preset,
        )

    async def continue_run(
        self,
        previous_run_id: str,
        follow_up: str,
        *,
        skill_names: Sequence[str] = (),
        approval_mode: ApprovalMode | None = None,
        check_group: str | None = None,
        model_preset: str | None = None,
    ) -> RunOutcome:
        """在历史运行检查点基础上启动后续轮次。"""
        return await self._run_service.continue_run(
            previous_run_id,
            follow_up,
            skill_names=skill_names,
            approval_mode=approval_mode,
            check_group=check_group,
            model_preset=model_preset,
        )

    async def implement_plan(
        self,
        run_id: str,
        *,
        skill_names: Sequence[str] = (),
        approval_mode: ApprovalMode | None = None,
        check_group: str | None = None,
        model_preset: str | None = None,
    ) -> RunOutcome:
        """确认并执行已经准备好的计划；只读 Runtime 无法提升写权限。"""
        return await self._run_service.implement_plan(
            run_id,
            skill_names=skill_names,
            approval_mode=approval_mode,
            check_group=check_group,
            model_preset=model_preset,
        )

    async def resume_run(self, run_id: str) -> RunOutcome:
        """从指定检查点恢复并继续执行中断的运行。"""
        checkpoint = await self._store.load_checkpoint(run_id)
        if checkpoint is None:
            raise ValueError(f"no checkpoint for run {run_id!r}; cannot resume it")
        ctx = build_run_context_from_checkpoint(checkpoint)
        return await self._run_service.resume(ctx)

    async def answer_run(self, run_id: str, response: UserInputResponse) -> RunOutcome:
        """提交结构化回答，或取消正在等待用户的运行。"""
        return await self._run_service.answer_run(run_id, response)

    async def steer(self, text: str, *, run_id: str | None = None) -> bool:
        """为活跃运行注入用户转向指令。"""
        return await self._run_service.steer(text, run_id=run_id)

    async def approve(
        self,
        approval_id: str,
        decision: ApprovalDecision,
        *,
        run_id: str | None = None,
    ) -> bool:
        """响应工具调用的审批请求（支持 QueueApprovalBroker 等具有 resolve 能力的 responder）。"""
        resolver = getattr(self._approvals, "resolve", None)
        if callable(resolver):
            return bool(resolver(approval_id, decision))
        return False

    async def cancel(self, run_id: str) -> bool:
        """尝试取消指定的运行。"""
        return await self._run_service.cancel(run_id)

    # -- 查询接口 -------------------------------------------------------------

    async def get_checkpoint(self, run_id: str) -> CheckpointV1 | None:
        """读取指定运行的最新检查点。"""
        return await self._store.load_checkpoint(run_id)

    async def get_run(self, run_id: str) -> RunRecord | None:
        """读取指定运行的持久化状态元数据。"""
        return await self._store.get_run(run_id)

    # -- 生命周期管理 ---------------------------------------------------------

    async def close(self) -> None:
        """安全释放运行时占用的所有资源与子服务。"""
        await self._run_service.close()
