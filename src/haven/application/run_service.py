"""RunService：负责一次运行的有界代理循环。

循环形态为 Model -> Tool(s) -> Observation -> Model……直到程序决定停止。每次停止都
只有一个原因；要判定成功，还必须通过证据门禁。

循环的一轮（见 `_drive`）按以下顺序执行：

    1. 预算检查              - 步数/工具调用/墙上时间/token/成本上限
                              （domain/budget.py）；超出任一上限就停止运行。
    2. 投递 steering         - 通过 `steer()` 排队的用户输入在此边界变成普通 user 消息，
                              绝不会插入流式输出中途（ADR 0020）。
    3. 构建上下文            - ContextBuilder 选择头部/历史/尾部，超预算时执行压缩，
                              并将分段报告到轨迹（`context.built`）。
    4. 模型流                - 提供商事件实时重新发送给 UI；流中途断开时由
                              `_stream_model` 以有界方式重试。
    5a. 工具调用             - 逐个交给 ToolPipeline；结果追加到 transcript；三次相同
                              调用加结果构成卡循环时停止运行。
    5b. 没有工具调用         - 文本成为候选最终答案；`finish_reason == "length"` 时执行
                              有界续写，而不是直接接受答案。
    6. 检查点                - 为崩溃恢复保存持久化快照。
    7. 完成                  - `_finish` 应用证据门禁：编辑过文件的运行只有在最后一次
                              写入之后记录了 diff 和通过的 check 时才成功，否则以停止
                              原因失败。

多轮会话：`continue_run` 恢复带检查点的 transcript 并追加后续请求（fork 表示从任意
较早的运行 ID 继续，见 ADR 0015/0020）；`rewind`（RecoveryService）是对已完成运行
文件的用户级撤销。
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
from collections import deque
from pathlib import Path

from haven.application.answer_resolution import (
    MAX_EMPTY_REPLIES as MAX_EMPTY_REPLIES,
)
from haven.application.answer_resolution import (
    MAX_EVIDENCE_NUDGES as MAX_EVIDENCE_NUDGES,
)
from haven.application.answer_resolution import (
    MAX_OUTPUT_CONTINUATIONS as MAX_OUTPUT_CONTINUATIONS,
)
from haven.application.answer_resolution import (
    AnswerAssembly,
    AnswerResolver,
)
from haven.application.approvals import ApprovalResponder
from haven.application.context_builder import ContextBuilder
from haven.application.emitter import EventEmitter
from haven.application.model_stream import (
    MODEL_RETRY_MAX_DELAY,
    ModelStreamer,
)
from haven.application.model_stream import retry_delay as _retry_delay
from haven.application.profiles import profile_for
from haven.application.registry import ToolRegistry
from haven.application.run_persistence import (
    CheckpointManager,
    RunFinalizer,
    RunOutcome,
)
from haven.application.run_telemetry import RunTelemetry
from haven.application.state import RunContext
from haven.application.tool_pipeline import ToolPipeline
from haven.contracts.checkpoint import CheckpointV1
from haven.contracts.events import (
    ContextBuilt,
    ModelCompleted,
    Notice,
    RunCreated,
    SteerQueued,
    StepStarted,
)
from haven.contracts.model import (
    ModelMessage,
    ModelRequest,
    ModelResult,
    ToolCallProposal,
)
from haven.contracts.tools import RecipeSpec, tool_schemas
from haven.domain.budget import Budget, check_accumulated_budget, check_budget
from haven.domain.enums import EffectState, PermissionMode, RunStatus, StopReason
from haven.domain.ids import RunId, new_run_id
from haven.domain.pricing import Pricing
from haven.domain.stuck import StuckLoopDetector, call_fingerprint
from haven.ports.executor import ExecutorPort
from haven.ports.model import ModelPort, ProviderError
from haven.ports.sandbox import SandboxLauncher
from haven.ports.session import SessionStorePort
from haven.ports.workspace import WorkspacePort

#: 提供商返回的 `context_overflow`（字符预算超过实际 token 窗口）会通过缩小
#: 预算并重建上下文来恢复，最多进行这么多次；之后运行失败，因此确实无法
#: 放入的 transcript 仍会导致停止。
MAX_CONTEXT_OVERFLOW_RETRIES = 2
#: 每次溢出重试后保留多少预算。缩减幅度足以让几次缩小消除真实的超出；
#: builder 会设置下限，以保证固定头部始终放得下。
CONTEXT_OVERFLOW_SHRINK = 0.6

#: 在真实网络中，提供商的临时故障很常见，因此因一次故障丢掉整个运行不应是
#: 默认行为。实测：8 次在线运行中有 3 次在收到任何 token 前遇到 ConnectError，
#: 31 个真实仓库案例中另有 2 个后来因一次适配器仍判定为不可重试的错误而失败——
#: 这个循环的效果取决于该分类是否准确。
__all__ = ["MODEL_RETRY_MAX_DELAY", "RunOutcome", "RunService", "_retry_delay"]


class RunService:
    """驱动一次有预算、有审计、可恢复的 Model → Tool → Observation 循环。"""

    def __init__(
        self,
        *,
        model: ModelPort,
        workspace: WorkspacePort,
        executor: ExecutorPort,
        store: SessionStorePort,
        emitter: EventEmitter,
        approvals: ApprovalResponder,
        recipes: dict[str, RecipeSpec],
        mode: PermissionMode,
        budget: Budget,
        pricing: Pricing | None = None,
        git_branch: str = "",
        git_commit: str = "",
        project_guidance: str = "",
        launcher: SandboxLauncher | None = None,
        context_chars_override: int = 0,
        supports_prefix_continuation: bool | None = None,
    ) -> None:
        self._model = model
        self._streamer = ModelStreamer(emitter)
        self._workspace = workspace
        self._store = store
        self._emitter = emitter
        self._mode = mode
        self._budget = budget
        # 每个模型的默认值；未知模型继承 Haven 的历史行为，而不是使用根据
        # 相似名称猜出的数字。
        self._profile = profile_for(model.model_name)
        # 评估/A-B 对 profile 上下文预算的覆盖值（0 = 使用 profile）。这样压缩
        # A-B 可以通过缩小预算来强制提前压缩，而无需虚构一个模型。
        self._context_chars = context_chars_override or self._profile.max_context_chars
        # 原生前缀续写既是模型能力，也是 endpoint 事实：DeepSeek 只在 beta base
        # URL 接受 `prefix: true`。组合根解析两者后将结论传到这里；None 表示
        # “没有部署层面的判断”，因此由 profile 自己的标志决定。
        self._supports_prefix = (
            self._profile.supports_assistant_prefix
            if supports_prefix_continuation is None
            else supports_prefix_continuation
        )
        # 已配置的费率优先，否则回退到模型公布的费率卡。报告实际使用模型的
        # 已记录价格优于报告 $0.00，但它是公布值而不是发票金额——见 profile
        # 中带日期的注释。
        self._pricing = pricing if pricing is not None else self._profile.pricing
        self._telemetry = RunTelemetry(emitter, self._pricing)
        self._checkpoints = CheckpointManager(store, workspace, emitter)
        self._finalizer = RunFinalizer(
            store, emitter, self._checkpoints, cost_known=self._pricing.is_known
        )
        self._git_branch = git_branch
        self._git_commit = git_commit
        self._project_guidance = project_guidance
        self._recipes = recipes
        self._registry = ToolRegistry()
        self._launcher = launcher
        # Steering：运行活跃时接受用户输入，但只在轮次边界投递，避免工具通道
        # 在副作用进行中被打断。到达时写入日志（持久化），由循环取出处理。
        self._steer_queue: deque[str] = deque()
        self._active_run_id: str | None = None
        # 每个运行一个独占临时目录，运行结束时删除。它使必须写入某处的沙箱工具
        # 不需要获得工作区之外的写权限。
        self._scratch_dir = Path(tempfile.mkdtemp(prefix="haven-scratch-"))
        self._scratch_ready = True
        self._pipeline = ToolPipeline(
            workspace=workspace,
            executor=executor,
            store=store,
            emitter=emitter,
            approvals=approvals,
            registry=self._registry,
            recipes=recipes,
            mode=mode,
            launcher=launcher,
            scratch_dir=self._scratch_dir,
        )
        self._answers = AnswerResolver(
            emitter=emitter,
            workspace=workspace,
            verification_available=lambda: bool(self._recipes),
            supports_prefix=self._supports_prefix,
            finish=self._finish,
            checkpoint=self._checkpoint,
        )

    # -- 入口 -----------------------------------------------------------------

    async def run(self, goal: str) -> RunOutcome:
        """创建运行并执行主循环，最终结果由证据门禁裁定。"""
        goal = goal.strip()
        if not (3 <= len(goal) <= 4000):
            raise ValueError("goal must be between 3 and 4000 characters")

        ctx = RunContext(
            run_id=new_run_id(),
            goal=goal,
            mode=self._mode,
            budget=self._budget,
        )
        await self._announce_run(ctx, goal)
        return await self._drive(ctx)

    async def continue_run(self, previous_run_id: str, follow_up: str) -> RunOutcome:
        """启动继承此前运行 transcript 的后续轮次。

        之前的对话会被带入，使模型拥有上下文，但这是一次全新的 Run：新的 ID、新的
        预算和新的证据账本，因此后续轮次的成功只根据其自身编辑来判断。会话的头部目标
        保持稳定（有利于提示缓存），后续内容作为 user 消息接入。
        """
        follow_up = follow_up.strip()
        if not (3 <= len(follow_up) <= 4000):
            raise ValueError("follow-up must be between 3 and 4000 characters")

        checkpoint = await self._store.load_checkpoint(previous_run_id)
        if checkpoint is None:
            raise ValueError(f"no checkpoint for run {previous_run_id!r}; cannot continue it")

        # 拒绝将某次运行的 transcript 和文件状态嫁接到另一个仓库：恢复流程
        # 会执行此检查，后续轮次也必须执行。
        if checkpoint.workspace_digest != self._workspace.workspace_digest:
            raise ValueError(
                "workspace identity changed since the run being continued; "
                "continue it from the same workspace"
            )

        # 后续轮次是新的 turn：它的运行 diff 必须只显示自身的改动，因此要重置
        # 上一轮的运行范围原始内容（在新进程中无害，但在会话复用的长期 TUI
        # 工作区中是必需的）。
        self._workspace.restore_originals({})

        transcript = list(checkpoint.messages)
        transcript.append(ModelMessage(role="user", content=f"Follow-up request: {follow_up}"))
        ctx = RunContext(
            run_id=new_run_id(),
            goal=checkpoint.goal,
            mode=self._mode,
            budget=self._budget,
            transcript=transcript,
            plan=checkpoint.plan,
            files_read=dict(checkpoint.files_read),
        )
        await self._announce_run(ctx, checkpoint.goal, parent_run_id=previous_run_id)
        await self._emitter.emit(
            ctx.run_id,
            Notice(run_id=ctx.run_id, level="info", message=f"continuing session: {follow_up}"),
        )
        return await self._drive(ctx)

    async def _announce_run(self, ctx: RunContext, goal: str, *, parent_run_id: str = "") -> None:
        await self._store.create_run(
            ctx.run_id,
            str(self._workspace.root),
            self._workspace.workspace_digest,
            goal,
            self._mode.value,
        )
        await self._emitter.emit(
            ctx.run_id,
            RunCreated(
                run_id=ctx.run_id,
                workspace=str(self._workspace.root),
                workspace_digest=self._workspace.workspace_digest,
                goal=goal,
                mode=self._mode.value,
                model_name=self._model.model_name,
                git_branch=self._git_branch,
                git_commit=self._git_commit,
                max_steps=self._budget.max_steps,
                sandbox_backend=(self._launcher.backend if self._launcher is not None else "none"),
                parent_run_id=parent_run_id,
            ),
        )

    async def resume(self, ctx: RunContext) -> RunOutcome:
        """继续由 RecoveryService 构建的已恢复运行上下文。"""
        await self._emitter.emit(
            ctx.run_id,
            Notice(run_id=ctx.run_id, level="info", message="run resumed from checkpoint"),
        )
        return await self._drive(ctx)

    @property
    def active_run_id(self) -> str | None:
        """返回当前正在驱动的运行 ID；没有运行时返回 None。"""
        return self._active_run_id

    async def close(self) -> None:
        """释放尚未被运行生命周期删除的临时目录；重复调用保持安全。"""
        if self._active_run_id is None and self._scratch_ready:
            shutil.rmtree(self._scratch_dir, ignore_errors=True)
            self._scratch_ready = False

    async def steer(self, text: str) -> bool:
        """为活跃运行排队用户输入，并在下一轮边界投递。

        当前模型调用和正在执行的工具都不会被打断；文本会在下一次模型请求前变成 user
        消息。没有活跃运行时返回 False（调用方应改为启动运行或后续轮次）。排队文本
        会立即写入日志，因此可以跨越崩溃保留。
        """
        text = text.strip()
        if not text or self._active_run_id is None:
            return False
        if len(text) > 4000:
            raise ValueError("steering text must be at most 4000 characters")
        self._steer_queue.append(text)
        await self._emitter.emit(
            self._active_run_id,
            SteerQueued(run_id=self._active_run_id, text=text),
        )
        return True

    def _drain_steering(self) -> list[str]:
        drained: list[str] = []
        while self._steer_queue:
            drained.append(self._steer_queue.popleft())
        return drained

    # -- 循环 ------------------------------------------------------------------

    async def _drive(self, ctx: RunContext) -> RunOutcome:
        """轮次循环。编号阶段记录在模块文档字符串中。每个 RunOutcome 都由 `_finish` 生成
        （直接生成，或在 `_handle_tool_calls` 内生成），因此停止原因和证据门禁只有
        一个应用位置。"""
        if not self._scratch_ready:
            # 绝不复用已删除目录的名字：本机另一个进程可能在两轮之间抢占旧路径
            # 或把它换成符号链接。mkdtemp 原子创建一个仅属于本轮的新目录。
            self._scratch_dir = Path(tempfile.mkdtemp(prefix="haven-scratch-"))
            self._pipeline.replace_scratch_dir(self._scratch_dir)
            self._scratch_ready = True
        builder = ContextBuilder(
            goal=ctx.goal,
            tools=tool_schemas(),
            budget=ctx.budget,
            recipe_ids=tuple(self._recipes),
            project_guidance=self._project_guidance,
            sandbox_backend=self._launcher.backend if self._launcher is not None else "",
            max_context_chars=self._context_chars,
            reasoning_effort=self._profile.reasoning_effort,
        )
        stuck = StuckLoopDetector()
        # 最近一次记录的 request envelope 摘要，因此未变化的 envelope 不会
        # 每一步都重复写日志。
        envelope_digest = ""
        ctx.start_timing(time.monotonic())
        # 输出截断和空回复的恢复状态（截断答案绝不能被静默接受为完整答案）。
        answer = AnswerAssembly()

        self._active_run_id = ctx.run_id
        self._steer_queue.clear()
        try:
            while True:
                ctx.refresh_wall_time(time.monotonic())
                if stop := check_budget(ctx.budget, ctx.usage):
                    return await self._finish(ctx, RunStatus.STOPPED, stop, answer.final_text)

                ctx.usage = ctx.usage.charge_step()
                step = ctx.usage.steps
                if ctx.status is RunStatus.CREATED:
                    ctx.move_to(RunStatus.RUNNING_MODEL)
                await self._emitter.emit(ctx.run_id, StepStarted(run_id=ctx.run_id, step=step))

                # 轮次边界：排队中的 steering 会在下一次模型请求前变成普通 user
                # 消息——绝不在流式输出或工具调用中途插入。
                for steered in self._drain_steering():
                    ctx.transcript.append(
                        ModelMessage(role="user", content=f"User update: {steered}")
                    )
                    await self._emitter.emit(
                        ctx.run_id,
                        Notice(
                            run_id=ctx.run_id,
                            level="info",
                            message=f"steering delivered: {steered[:160]}",
                        ),
                    )

                # 构建并流式请求，且对提供商上下文溢出进行有界恢复：这里的 400 表示
                # 字符预算超过了实际 token 窗口，因此要缩小预算（强制进行更多压缩）
                # 并重建请求，而不是让运行失败。
                overflow_retries = 0
                while True:
                    request, segments = builder.build(ctx.transcript, ctx.usage, ctx.plan)
                    envelope_digest = await self._telemetry.record_envelope(
                        ctx, step, request, envelope_digest
                    )
                    await self._emitter.emit(
                        ctx.run_id,
                        ContextBuilt(
                            run_id=ctx.run_id,
                            step=step,
                            segments=segments,
                            total_bytes=sum(s.size_bytes for s in segments),
                        ),
                    )

                    try:
                        ctx.refresh_wall_time(time.monotonic())
                        remaining = max(
                            0.0,
                            ctx.budget.max_wall_time_seconds - ctx.usage.wall_time_seconds,
                        )
                        async with asyncio.timeout(remaining):
                            result = await self._stream_model(ctx, step, request)
                        break
                    except TimeoutError:
                        ctx.refresh_wall_time(time.monotonic())
                        return await self._finish(
                            ctx,
                            RunStatus.STOPPED,
                            StopReason.WALL_TIME_EXHAUSTED,
                            answer.final_text,
                        )
                    except ProviderError as exc:
                        if (
                            exc.code == "context_overflow"
                            and overflow_retries < MAX_CONTEXT_OVERFLOW_RETRIES
                        ):
                            overflow_retries += 1
                            new_budget = builder.reduce_budget(CONTEXT_OVERFLOW_SHRINK)
                            await self._emitter.emit(
                                ctx.run_id,
                                Notice(
                                    run_id=ctx.run_id,
                                    level="warning",
                                    message=(
                                        "context overflow; forcing compaction "
                                        f"(budget -> {new_budget} chars), retry "
                                        f"{overflow_retries}/{MAX_CONTEXT_OVERFLOW_RETRIES}"
                                    ),
                                ),
                            )
                            continue
                        await self._emitter.emit(
                            ctx.run_id,
                            Notice(
                                run_id=ctx.run_id,
                                level="error",
                                message=f"provider error ({exc.code}): {exc}",
                            ),
                        )
                        return await self._finish(
                            ctx, RunStatus.FAILED, StopReason.PROVIDER_ERROR, answer.final_text
                        )

                self._telemetry.charge_usage(ctx, request, result)
                ctx.refresh_wall_time(time.monotonic())
                await self._emitter.emit(
                    ctx.run_id,
                    ModelCompleted(
                        run_id=ctx.run_id,
                        step=step,
                        text=result.text,
                        tool_call_count=len(result.tool_calls),
                        input_tokens=result.usage.input_tokens,
                        output_tokens=result.usage.output_tokens,
                        usage_estimated=result.usage.estimated,
                        ttft_ms=result.ttft_ms,
                        duration_ms=result.duration_ms,
                        finish_reason=result.finish_reason,
                        reasoning_tokens=result.usage.reasoning_tokens,
                        cached_input_tokens=result.usage.cached_input_tokens,
                    ),
                )
                ctx.transcript.append(
                    ModelMessage(
                        role="assistant",
                        content=result.text,
                        tool_calls=result.tool_calls,
                        provider_reasoning=result.provider_reasoning,
                    )
                )

                if stop := check_accumulated_budget(ctx.budget, ctx.usage):
                    return await self._finish(ctx, RunStatus.STOPPED, stop, answer.final_text)

                if result.tool_calls:
                    answer.reset_partial()
                    stopped = await self._handle_tool_calls(ctx, step, result.tool_calls, stuck)
                    await self._checkpoint(ctx)
                    if stopped is not None:
                        return stopped
                    continue

                # 没有工具调用：该回复是候选最终答案。
                # 先有界地恢复截断或空回复，再由 Evidence Gate 决定——模型的声明
                # 永远不能代替门禁判断。
                recovered = await self._answers.recover_incomplete_reply(ctx, result, answer)
                if isinstance(recovered, RunOutcome):
                    return recovered
                if recovered:
                    continue
                outcome = await self._answers.finish_with_gate(ctx, result, answer)
                if outcome is not None:
                    return outcome
        except asyncio.CancelledError:
            executions = await self._store.load_executions(ctx.run_id)
            started = [
                record for record in executions if record.effect_state is EffectState.STARTED
            ]
            # 取消可以落在 STARTED 持久化之后、具体执行器的异常处理之前（例如
            # 进程前快照或异步数据库边界）。终态收口负责把所有这类悬空记录显式
            # 归一为未知影响，不能只让 run 行说 unknown 而明细仍声称“正在执行”。
            for record in started:
                await self._store.update_execution_state(
                    ctx.run_id, record.call_id, EffectState.EFFECT_UNKNOWN
                )
            uncertain = bool(started) or any(
                record.effect_state is EffectState.EFFECT_UNKNOWN for record in executions
            )
            if uncertain:
                await self._finish(
                    ctx,
                    RunStatus.EFFECT_UNKNOWN,
                    StopReason.EFFECT_UNKNOWN,
                    answer.final_text,
                )
            else:
                await self._finish(
                    ctx, RunStatus.CANCELLED, StopReason.CANCELLED, answer.final_text
                )
            raise
        finally:
            self._active_run_id = None
            if self._steer_queue:
                # 未投递的 steering 不能泄漏到后续运行；排队事件会留在日志中
                # 作为记录。
                self._steer_queue.clear()
            if self._scratch_ready:
                shutil.rmtree(self._scratch_dir, ignore_errors=True)
                self._scratch_ready = False

    async def _handle_tool_calls(
        self,
        ctx: RunContext,
        step: int,
        calls: tuple[ToolCallProposal, ...],
        stuck: StuckLoopDetector,
    ) -> RunOutcome | None:
        """严格按顺序通过流水线执行一轮中的工具调用。

        返回 RunOutcome 表示停止运行（工具预算耗尽、未知副作用或卡循环），返回 None
        则将观察结果交还模型。这里有意采用串行执行：并行副作用会使审批、preimage
        固定值和日志顺序产生歧义。
        """
        for call in calls:
            if ctx.usage.tool_calls >= ctx.budget.max_tool_calls:
                return await self._finish(
                    ctx, RunStatus.STOPPED, StopReason.TOOL_BUDGET_EXHAUSTED, ""
                )
            ctx.usage = ctx.usage.charge_tool_call()
            ctx.move_to(RunStatus.VALIDATING_TOOL)

            execution = await self._pipeline.execute(ctx, step, call)
            ctx.refresh_wall_time(time.monotonic())
            result = execution.result
            ctx.transcript.append(
                ModelMessage(
                    role="tool",
                    content=(
                        f'<tool_output tool="{call.tool_name}">\n'
                        f"{result.to_model_text()}\n</tool_output>"
                    ),
                    tool_call_id=call.call_id,
                )
            )

            if execution.effect_unknown:
                ctx.move_to(RunStatus.EFFECT_UNKNOWN)
                return await self._finish(
                    ctx, RunStatus.EFFECT_UNKNOWN, StopReason.EFFECT_UNKNOWN, ""
                )

            if ctx.usage.wall_time_seconds >= ctx.budget.max_wall_time_seconds:
                return await self._finish(
                    ctx,
                    RunStatus.STOPPED,
                    StopReason.WALL_TIME_EXHAUSTED,
                    "",
                )

            if ctx.status is not RunStatus.RUNNING_MODEL:
                ctx.move_to(RunStatus.RUNNING_MODEL)

            fingerprint = call_fingerprint(
                call.tool_name, call.arguments_json, result.to_model_text()
            )
            if stuck.observe(fingerprint):
                await self._emitter.emit(
                    ctx.run_id,
                    Notice(
                        run_id=ctx.run_id,
                        level="warning",
                        message=(
                            f"stuck loop: {call.tool_name} repeated with identical "
                            "arguments and results"
                        ),
                    ),
                )
                return await self._finish(ctx, RunStatus.STOPPED, StopReason.NO_PROGRESS, "")
        return None

    # -- 模型流式输出 ------------------------------------------------------------

    async def _stream_model(self, ctx: RunContext, step: int, request: ModelRequest) -> ModelResult:
        return await self._streamer.stream(self._model, ctx, step, request)

    # -- 持久化 ------------------------------------------------------------------

    async def _checkpoint(self, ctx: RunContext) -> None:
        ctx.refresh_wall_time(time.monotonic())
        await self._checkpoints.save(ctx)

    async def _finish(
        self,
        ctx: RunContext,
        status: RunStatus,
        stop_reason: StopReason,
        final_text: str,
        gate_reason: str = "",
    ) -> RunOutcome:
        """唯一出口：持久化终态、检查点并发出 run.finished。"""
        ctx.refresh_wall_time(time.monotonic())
        return await self._finalizer.finish(ctx, status, stop_reason, final_text, gate_reason)


def build_run_context_from_checkpoint(
    checkpoint: CheckpointV1,
) -> RunContext:
    """根据检查点重建工作状态（供恢复流程使用）。"""
    ctx = RunContext(
        run_id=RunId(checkpoint.run_id),
        goal=checkpoint.goal,
        mode=PermissionMode(checkpoint.mode),
        budget=checkpoint.budget.to_domain(),
        usage=checkpoint.usage.to_domain(),
        transcript=list(checkpoint.messages),
        ledger=checkpoint.evidence.to_domain(),
        files_read=dict(checkpoint.files_read),
        plan=checkpoint.plan,
        last_seq=checkpoint.last_seq,
    )
    # 恢复后的运行从模型轮次继续。
    ctx.status = RunStatus.RUNNING_MODEL
    return ctx
