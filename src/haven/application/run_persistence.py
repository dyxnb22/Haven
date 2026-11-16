"""运行检查点和唯一终态持久化出口。"""

from dataclasses import dataclass

from haven.application.emitter import EventEmitter
from haven.application.state import RunContext
from haven.contracts.checkpoint import (
    BudgetSnapshot,
    CheckpointV1,
    EvidenceSnapshot,
    UsageSnapshot,
)
from haven.contracts.events import PlanStepView, PlanUpdated, RunFinished
from haven.contracts.user_input import UserInputRequest
from haven.domain.enums import AgentMode, PlanStatus, RunStatus, StopReason
from haven.ports.session import SessionStorePort
from haven.ports.workspace import WorkspacePort


@dataclass(frozen=True, slots=True)
class RunOutcome:
    """运行完成后交给 CLI/TUI 的最终结果摘要。"""

    #: 已完成运行的稳定标识。
    run_id: str
    #: 最终生命周期状态。
    status: RunStatus
    #: 导致模型循环停止的最终原因。
    stop_reason: StopReason
    #: Evidence Gate 原因；未进入验证阶段时为空。
    gate_reason: str
    #: 已完成的模型循环轮数。
    steps: int
    #: 已执行的工具调用次数。
    tool_calls: int
    #: 输入 token 总数，包含缓存命中部分。
    input_tokens: int
    #: 生成的输出 token 总数。
    output_tokens: int
    #: 由提供商缓存提供的输入 token 数量。
    cached_input_tokens: int
    #: 累计估算费用，单位为美元。
    cost_usd: float
    #: 所选模型是否有已知费率卡。
    cost_known: bool
    #: 任意用量值来自估算而非提供商报告时为 True。
    usage_estimated: bool
    #: 最终可见答案文本。
    final_text: str
    #: 非终态暂停时返回的待答问题。
    pending_input: UserInputRequest | None = None


class CheckpointManager:
    """把内存运行状态转换为版本化检查点并保存。"""

    def __init__(
        self,
        store: SessionStorePort,
        workspace: WorkspacePort,
        emitter: EventEmitter,
        *,
        include_originals: bool = True,
    ) -> None:
        self._store = store
        self._workspace = workspace
        self._emitter = emitter
        self._include_originals = include_originals

    async def save(self, ctx: RunContext) -> None:
        """把当前运行状态、消息、证据和原始文件构件写成版本化检查点。"""
        # 新进程的 emitter 还可能没有发出事件，不能把检查点序号回退到 0。
        # 否则 SQLite 仍会优先加载旧的高序号快照，丢掉刚保存的用户答案。
        ctx.last_seq = max(ctx.last_seq, self._emitter.last_seq(ctx.run_id))
        original_artifacts: dict[str, str] = {}
        originals = self._workspace.original_contents() if self._include_originals else {}
        for path, content in originals.items():
            original_artifacts[path] = (
                "" if content is None else await self._store.put_artifact(content.encode("utf-8"))
            )
        checkpoint = CheckpointV1(
            run_id=ctx.run_id,
            workspace_digest=self._workspace.workspace_digest,
            goal=ctx.goal,
            mode=ctx.mode.value,
            status=ctx.status.value,
            last_seq=ctx.last_seq,
            budget=BudgetSnapshot.from_domain(ctx.budget),
            usage=UsageSnapshot.from_domain(ctx.usage),
            messages=tuple(ctx.transcript),
            evidence=EvidenceSnapshot.from_domain(ctx.ledger),
            files_read=dict(ctx.files_read),
            plan=ctx.plan,
            agent_mode=ctx.agent_mode,
            plan_status=ctx.plan_status,
            pending_input=ctx.pending_input,
            parent_run_id=ctx.parent_run_id,
            root_run_id=ctx.root_run_id,
            file_scope=ctx.file_scope,
            require_review=ctx.require_review,
            guidance_snapshot=ctx.guidance_snapshot,
            verification=ctx.verification,
            model_selection=ctx.model_selection,
            handoff=ctx.handoff,
            original_artifacts=original_artifacts,
            catalog_digest=ctx.catalog_digest,
        )
        await self._store.save_checkpoint(checkpoint)


class RunFinalizer:
    """持久化终态、最终检查点和 run.finished 事件。"""

    def __init__(
        self,
        store: SessionStorePort,
        emitter: EventEmitter,
        checkpoints: CheckpointManager,
        *,
        cost_known: bool,
    ) -> None:
        self._store = store
        self._emitter = emitter
        self._checkpoints = checkpoints
        self._cost_known = cost_known

    async def pause_for_input(self, ctx: RunContext) -> RunOutcome:
        """持久化非终态暂停，不发出 run.finished，也不调用模型。"""
        if ctx.status is not RunStatus.WAITING_USER:
            ctx.move_to(RunStatus.WAITING_USER)
        await self._store.update_run_status(ctx.run_id, ctx.status, StopReason.NEEDS_INPUT.value)
        await self._checkpoints.save(ctx)
        return self._outcome(ctx, StopReason.NEEDS_INPUT, "")

    async def finish(
        self,
        ctx: RunContext,
        status: RunStatus,
        stop_reason: StopReason,
        final_text: str,
        gate_reason: str = "",
    ) -> RunOutcome:
        """以唯一终态顺序持久化状态、检查点和 ``run.finished`` 事件。"""
        ctx.pending_input = None
        if status is RunStatus.SUCCEEDED and ctx.plan:
            plan_status = ctx.plan_status
            if ctx.agent_mode is AgentMode.PLAN:
                ctx.plan_status = PlanStatus.READY
            elif all(step.status == "done" for step in ctx.plan):
                ctx.plan_status = PlanStatus.COMPLETED
            if ctx.plan_status is not plan_status:
                await self._emitter.emit(
                    ctx.run_id,
                    PlanUpdated(
                        run_id=ctx.run_id,
                        steps=tuple(
                            PlanStepView.model_validate(step.model_dump()) for step in ctx.plan
                        ),
                        plan_status=ctx.plan_status.value if ctx.plan_status else "",
                    ),
                )
        if ctx.status is not status:
            ctx.move_to(status)
        await self._store.update_run_status(ctx.run_id, status, stop_reason.value)
        await self._checkpoints.save(ctx)
        cost_usd = round(ctx.usage.cost_usd, 6)
        await self._emitter.emit(
            ctx.run_id,
            RunFinished(
                run_id=ctx.run_id,
                status=status.value,
                stop_reason=stop_reason.value,
                gate_reason=gate_reason,
                steps=ctx.usage.steps,
                tool_calls=ctx.usage.tool_calls,
                input_tokens=ctx.usage.input_tokens,
                output_tokens=ctx.usage.output_tokens,
                cached_input_tokens=ctx.usage.cached_input_tokens,
                cost_usd=cost_usd,
                cost_known=self._usage_cost_known(ctx),
                usage_estimated=ctx.usage.usage_estimated,
                duration_ms=int(ctx.usage.wall_time_seconds * 1000),
            ),
        )
        return self._outcome(ctx, stop_reason, final_text, gate_reason)

    def _outcome(
        self, ctx: RunContext, stop_reason: StopReason, final_text: str, gate_reason: str = ""
    ) -> RunOutcome:
        return RunOutcome(
            run_id=ctx.run_id,
            status=ctx.status,
            stop_reason=stop_reason,
            gate_reason=gate_reason,
            steps=ctx.usage.steps,
            tool_calls=ctx.usage.tool_calls,
            input_tokens=ctx.usage.input_tokens,
            output_tokens=ctx.usage.output_tokens,
            cached_input_tokens=ctx.usage.cached_input_tokens,
            cost_usd=round(ctx.usage.cost_usd, 6),
            cost_known=self._usage_cost_known(ctx),
            usage_estimated=ctx.usage.usage_estimated,
            final_text=final_text,
            pending_input=ctx.pending_input,
        )

    def _usage_cost_known(self, ctx: RunContext) -> bool:
        usage = ctx.usage
        if usage.input_tokens or usage.output_tokens:
            return usage.cost_known
        return usage.cost_known and self._cost_known
