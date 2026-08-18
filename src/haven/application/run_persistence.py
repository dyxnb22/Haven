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
from haven.contracts.events import RunFinished
from haven.domain.enums import RunStatus, StopReason
from haven.ports.session import SessionStorePort
from haven.ports.workspace import WorkspacePort


@dataclass(frozen=True, slots=True)
class RunOutcome:
    run_id: str
    status: RunStatus
    stop_reason: StopReason
    gate_reason: str
    steps: int
    tool_calls: int
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    cost_usd: float
    cost_known: bool
    usage_estimated: bool
    final_text: str


class CheckpointManager:
    def __init__(
        self, store: SessionStorePort, workspace: WorkspacePort, emitter: EventEmitter
    ) -> None:
        self._store = store
        self._workspace = workspace
        self._emitter = emitter

    async def save(self, ctx: RunContext) -> None:
        ctx.last_seq = self._emitter.last_seq(ctx.run_id)
        original_artifacts: dict[str, str] = {}
        for path, content in self._workspace.original_contents().items():
            original_artifacts[path] = await self._store.put_artifact(content.encode("utf-8"))
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
            original_artifacts=original_artifacts,
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

    async def finish(
        self,
        ctx: RunContext,
        status: RunStatus,
        stop_reason: StopReason,
        final_text: str,
        gate_reason: str = "",
    ) -> RunOutcome:
        if ctx.status is not status:
            ctx.status = status
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
                cost_known=self._cost_known,
                usage_estimated=ctx.usage.usage_estimated,
                duration_ms=int(ctx.usage.wall_time_seconds * 1000),
            ),
        )
        return RunOutcome(
            run_id=ctx.run_id,
            status=status,
            stop_reason=stop_reason,
            gate_reason=gate_reason,
            steps=ctx.usage.steps,
            tool_calls=ctx.usage.tool_calls,
            input_tokens=ctx.usage.input_tokens,
            output_tokens=ctx.usage.output_tokens,
            cached_input_tokens=ctx.usage.cached_input_tokens,
            cost_usd=cost_usd,
            cost_known=self._cost_known,
            usage_estimated=ctx.usage.usage_estimated,
            final_text=final_text,
        )
