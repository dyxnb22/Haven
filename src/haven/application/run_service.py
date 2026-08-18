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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from haven.application.approvals import ApprovalResponder
from haven.application.context_builder import ContextBuilder
from haven.application.emitter import EventEmitter
from haven.application.profiles import profile_for
from haven.application.registry import ToolRegistry
from haven.application.state import RunContext
from haven.application.tool_pipeline import ToolPipeline
from haven.contracts.checkpoint import (
    BudgetSnapshot,
    CheckpointV1,
    EvidenceSnapshot,
    UsageSnapshot,
)
from haven.contracts.events import (
    AssistantDelta,
    AssistantReasoning,
    ContextBuilt,
    ModelCompleted,
    Notice,
    RequestEnvelope,
    RunCreated,
    RunFinished,
    SteerQueued,
    StepStarted,
    StreamRestarted,
)
from haven.contracts.model import (
    ModelMessage,
    ModelRequest,
    ModelResult,
    ReasoningDelta,
    StreamFinished,
    TextDelta,
    ToolCallProposal,
    ToolCallReady,
    Usage,
    UsageReport,
)
from haven.contracts.tools import RecipeSpec, tool_schemas
from haven.domain.budget import Budget, check_budget
from haven.domain.digest import digest_of
from haven.domain.enums import PermissionMode, RunStatus, StopReason
from haven.domain.evidence import evaluate_evidence_gate
from haven.domain.ids import RunId, new_run_id
from haven.domain.pricing import Pricing
from haven.domain.stuck import StuckLoopDetector, call_fingerprint
from haven.ports.executor import ExecutorPort
from haven.ports.model import ModelPort, ProviderError
from haven.ports.sandbox import SandboxLauncher
from haven.ports.session import SessionStorePort
from haven.ports.workspace import WorkspacePort

MAX_EVIDENCE_NUDGES = 2

#: 对于被提供商的输出 token 限制截断的答案，最多续写这么多次；之后 Haven
#: 会带着警告继续使用部分文本，从而避免一个不停截断的模型耗尽全部预算。
MAX_OUTPUT_CONTINUATIONS = 2

#: 对于既没有文本也没有工具调用的回复（例如只有推理的响应），会重新提示
#: 这么多次，之后运行因没有进展而停止。
MAX_EMPTY_REPLIES = 2

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
MODEL_RETRY_ATTEMPTS = 2
MODEL_RETRY_BASE_DELAY = 1.0
#: 提供商可能通过 `Retry-After` 要求比此值更长的等待，但遵守任意时长的有界
#: 重试循环可能让运行在墙上时钟预算之后仍阻塞数分钟（预算只在轮次之间检查，
#: 睡眠期间不检查）。超过此上限后，运行应失败并等待恢复，而不是一直挂起。
MODEL_RETRY_MAX_DELAY = 60.0


def _retry_delay(attempt: int, retry_after_s: float | None) -> float:
    """下一次模型重试前的等待时间：指数退避与提供商要求的 `Retry-After` 两者取较大值，
    但不超过 `MODEL_RETRY_MAX_DELAY`。遵守 `Retry-After` 可以避免固定退避过短、持续
    冲击要求更长等待的提供商；上限则避免某个响应头阻塞整个运行。"""
    backoff: float = MODEL_RETRY_BASE_DELAY * (2**attempt)
    wait = backoff if retry_after_s is None else max(backoff, retry_after_s)
    return min(wait, MODEL_RETRY_MAX_DELAY)


@dataclass(slots=True)
class _StreamProgress:
    """记录流在失败前是否产生过内容，用于决定重试是否安全。"""

    started: bool = False


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
    #: 模型没有费率卡时为 False，此时 `cost_usd` 是占位值而不是测量结果。
    cost_known: bool
    usage_estimated: bool
    final_text: str


@dataclass
class _AnswerAssembly:
    """用于组装一个最终答案的跨轮状态。

    `parts` 保存提供商因输出 token 上限而截断的答案片段，并在有界续写之间拼接；计数器
    限制两条恢复循环；`final_text` 是最后组装出的答案，每条停止路径都会将其作为运行
    的最终文本报告。
    """

    parts: list[str] = field(default_factory=list)
    continuations: int = 0
    empty_replies: int = 0
    final_text: str = ""


class RunService:
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
        # 每个服务一个临时目录，运行结束时删除。它使必须写入某处的沙箱工具
        # 不需要获得工作区之外的写权限。
        self._scratch_dir = Path(tempfile.mkdtemp(prefix="haven-scratch-"))
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

    # -- 入口 -----------------------------------------------------------------

    async def run(self, goal: str) -> RunOutcome:
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

    async def steer(self, text: str) -> bool:
        """为活跃运行排队用户输入，并在下一轮边界投递。

        当前模型调用和正在执行的工具都不会被打断；文本会在下一次模型请求前变成 user
        消息。没有活跃运行时返回 False（调用方应改为启动运行或后续轮次）。排队文本
        会立即写入日志，因此可以跨越崩溃保留。
        """
        text = text.strip()
        if not text or self._active_run_id is None:
            return False
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
        started = time.monotonic()
        elapsed_base = ctx.usage.wall_time_seconds
        # 输出截断和空回复的恢复状态（截断答案绝不能被静默接受为完整答案）。
        answer = _AnswerAssembly()

        self._active_run_id = ctx.run_id
        self._steer_queue.clear()
        try:
            while True:
                ctx.usage = ctx.usage.with_wall_time(elapsed_base + (time.monotonic() - started))
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
                    envelope_digest = await self._record_envelope(
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
                        result = await self._stream_model(ctx, step, request)
                        break
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

                self._charge_usage(ctx, request, result)
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

                if result.tool_calls:
                    stopped = await self._handle_tool_calls(ctx, step, result.tool_calls, stuck)
                    await self._checkpoint(ctx)
                    if stopped is not None:
                        return stopped
                    continue

                # 没有工具调用：该回复是候选最终答案。
                # 先有界地恢复截断或空回复，再由 Evidence Gate 决定——模型的声明
                # 永远不能代替门禁判断。
                recovered = await self._recover_incomplete_reply(ctx, result, answer)
                if isinstance(recovered, RunOutcome):
                    return recovered
                if recovered:
                    continue
                outcome = await self._finish_with_gate(ctx, result, answer)
                if outcome is not None:
                    return outcome
        except asyncio.CancelledError:
            await self._finish(ctx, RunStatus.CANCELLED, StopReason.CANCELLED, answer.final_text)
            raise
        finally:
            self._active_run_id = None
            if self._steer_queue:
                # 未投递的 steering 不能泄漏到后续运行；排队事件会留在日志中
                # 作为记录。
                self._steer_queue.clear()
            shutil.rmtree(self._scratch_dir, ignore_errors=True)

    async def _recover_incomplete_reply(
        self, ctx: RunContext, result: ModelResult, answer: _AnswerAssembly
    ) -> RunOutcome | bool:
        """截断/空回复恢复机制，两条循环都有上限。

        返回 RunOutcome 表示停止运行（重复的空回复）；返回 True 表示已排队续写或重新
        提示（继续下一轮）；返回 False 表示回复已经完整到足以进入证据门禁。
        """
        # 被提供商输出 token 限制截断的答案不是最终答案。请求它继续——次数
        # 必须有界，不能让一个不断截断的模型耗尽预算。
        if result.finish_reason == "length" and answer.continuations < MAX_OUTPUT_CONTINUATIONS:
            answer.continuations += 1
            answer.parts.append(result.text)
            await self._emitter.emit(
                ctx.run_id,
                Notice(
                    run_id=ctx.run_id,
                    level="warning",
                    message=(
                        "answer hit the output token limit; requesting a "
                        f"continuation ({answer.continuations}/{MAX_OUTPUT_CONTINUATIONS})"
                    ),
                ),
            )
            if self._supports_prefix:
                # 原生前缀续写（ADR 0022）：将部分答案作为 assistant“前缀”重新
                # 发出，让模型原地扩展——没有接缝重复，也不增加 user 轮次。适配器
                # 会识别末尾的 assistant 消息，并连同提供商的 prefix 标志发送。
                ctx.transcript.append(
                    ModelMessage(role="assistant", content=result.text, is_prefix=True)
                )
            else:
                # 对话式垫片：要求下一轮继续。接缝处可能重复内容，而且会多消耗
                # 一次完整请求，但不需要提供商特定的支持。
                ctx.transcript.append(
                    ModelMessage(
                        role="user",
                        content=(
                            "Your previous message was cut off at the output token "
                            "limit. Continue exactly from where it stopped, without "
                            "repeating anything. If no answer text was produced yet, "
                            "give the answer directly and concisely."
                        ),
                    )
                )
            return True

        # 既没有文本也没有工具调用的回复（只有推理的响应）会以空答案通过
        # no-edit 门禁。重新提示，但次数必须有界。
        if not result.text.strip() and not answer.parts:
            answer.empty_replies += 1
            if answer.empty_replies > MAX_EMPTY_REPLIES:
                await self._emitter.emit(
                    ctx.run_id,
                    Notice(
                        run_id=ctx.run_id,
                        level="error",
                        message="model repeatedly returned no content and no tool calls",
                    ),
                )
                return await self._finish(
                    ctx, RunStatus.STOPPED, StopReason.NO_PROGRESS, answer.final_text
                )
            await self._emitter.emit(
                ctx.run_id,
                Notice(
                    run_id=ctx.run_id,
                    level="warning",
                    message="model returned no content; asking again "
                    f"({answer.empty_replies}/{MAX_EMPTY_REPLIES})",
                ),
            )
            ctx.transcript.append(
                ModelMessage(
                    role="user",
                    content=(
                        "Your reply contained no answer text and no tool calls. "
                        "Reply with either a tool call or your answer."
                    ),
                )
            )
            return True

        return False

    async def _finish_with_gate(
        self, ctx: RunContext, result: ModelResult, answer: _AnswerAssembly
    ) -> RunOutcome | None:
        """组装最终答案并应用证据门禁。

        返回运行结果；如果门禁拒绝了答案，并且已经向模型发送提示要求其生成证据
        （次数受 MAX_EVIDENCE_NUDGES 限制），则返回 None，由调用方继续下一轮。
        """
        answer.final_text = "".join((*answer.parts, result.text))
        answer.parts = []
        if result.finish_reason == "length":
            await self._emitter.emit(
                ctx.run_id,
                Notice(
                    run_id=ctx.run_id,
                    level="warning",
                    message=(
                        "answer still truncated after "
                        f"{MAX_OUTPUT_CONTINUATIONS} continuations; "
                        "proceeding with the partial answer"
                    ),
                ),
            )
        ctx.move_to(RunStatus.VERIFYING)
        # 重新读取累计 diff，使审查看到的是当前磁盘内容，而不是过时事件
        # 记录的内容。
        diff_text = (await self._workspace.run_diff()).diff if ctx.ledger.has_edits else ""
        gate = evaluate_evidence_gate(
            ctx.ledger, diff_text, verification_available=bool(self._recipes)
        )
        if gate.passed:
            reason = (
                StopReason.EVIDENCE_SATISFIED if ctx.ledger.has_edits else StopReason.FINAL_ANSWER
            )
            return await self._finish(
                ctx, RunStatus.SUCCEEDED, reason, answer.final_text, gate.reason_code
            )

        if gate.terminal:
            # 在这里发送 nudge 会循环到预算耗尽，却没有任何成功可能，还会报告
            # 错误的停止原因。
            await self._emitter.emit(
                ctx.run_id,
                Notice(run_id=ctx.run_id, level="error", message=gate.detail),
            )
            return await self._finish(
                ctx,
                RunStatus.STOPPED,
                StopReason.VERIFICATION_UNAVAILABLE,
                answer.final_text,
                gate.reason_code,
            )

        ctx.nudges += 1
        if ctx.nudges > MAX_EVIDENCE_NUDGES:
            return await self._finish(
                ctx,
                RunStatus.STOPPED,
                StopReason.EVIDENCE_MISSING,
                answer.final_text,
                gate.reason_code,
            )
        await self._emitter.emit(
            ctx.run_id,
            Notice(
                run_id=ctx.run_id,
                level="warning",
                message=f"final answer rejected by evidence gate: {gate.detail}",
            ),
        )
        ctx.transcript.append(
            ModelMessage(
                role="user",
                content=(
                    "Your answer was NOT accepted as success: "
                    f"{gate.detail} Run repo.diff and a repo.check recipe to "
                    "produce fresh evidence, then answer again."
                ),
            )
        )
        ctx.move_to(RunStatus.RUNNING_MODEL)
        await self._checkpoint(ctx)
        return None

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

    async def _record_envelope(
        self, ctx: RunContext, step: int, request: ModelRequest, previous: str
    ) -> str:
        """在模型可见的请求信封发生变化时记录日志。

        返回当前摘要，使调用方可以在下一步比较。系统提示和工具集按设计在一次运行中
        保持稳定（ADR 0008），因此实际通常每次运行只写入一个事件；只有在影响模型行为
        的内容发生移动时才会写入第二个事件，而这正是轨迹中值得关注的情况。
        """
        system = next((m.content for m in request.messages if m.role == "system"), "")
        tool_names = tuple(tool.name for tool in request.tools)
        digest = digest_of(
            {
                "system": system,
                "tools": list(tool_names),
                "reasoning_effort": request.reasoning_effort or "",
                "max_output_tokens": request.max_output_tokens or 0,
            }
        )
        if digest == previous:
            return digest
        await self._emitter.emit(
            ctx.run_id,
            RequestEnvelope(
                run_id=ctx.run_id,
                step=step,
                reason="initial" if not previous else "changed",
                system_prompt_digest=digest,
                system_prompt_chars=len(system),
                tool_names=tool_names,
                reasoning_effort=request.reasoning_effort or "",
                max_output_tokens=request.max_output_tokens or 0,
            ),
        )
        return digest

    # -- 模型流式输出 ------------------------------------------------------------

    async def _stream_model(self, ctx: RunContext, step: int, request: ModelRequest) -> ModelResult:
        """流式处理一轮模型调用，只在能够证明安全时重试。

        模型调用没有副作用，因此重试连接失败不会重复应用任何操作——工具调用则从不
        重试。部分流式输出的轮次也可以安全重试：组装中的文本和工具调用只属于当前尝试，
        会被丢弃；在轮次完成前，任何内容都不会进入 transcript 或工具流水线。只有已经
        展示给用户的文本会过时，因此会通知 UI 重置。
        """
        for attempt in range(MODEL_RETRY_ATTEMPTS + 1):
            progress = _StreamProgress()
            try:
                return await self._stream_once(ctx, step, request, progress)
            except ProviderError as exc:
                exhausted = attempt == MODEL_RETRY_ATTEMPTS
                if not exc.retryable or exhausted:
                    raise
                if progress.started:
                    await self._emitter.emit(
                        ctx.run_id, StreamRestarted(run_id=ctx.run_id, step=step)
                    )
                delay = _retry_delay(attempt, exc.retry_after_s)
                await self._emitter.emit(
                    ctx.run_id,
                    Notice(
                        run_id=ctx.run_id,
                        level="warning",
                        message=(
                            f"provider error ({exc.code}); retrying in {delay:.1f}s "
                            f"({attempt + 1}/{MODEL_RETRY_ATTEMPTS})"
                        ),
                    ),
                )
                await asyncio.sleep(delay)
        raise ProviderError("server", "model retry loop exhausted")

    async def _stream_once(
        self,
        ctx: RunContext,
        step: int,
        request: ModelRequest,
        progress: _StreamProgress,
    ) -> ModelResult:
        started = time.monotonic()
        ttft_ms = 0
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls: list[ToolCallProposal] = []
        usage = Usage()
        finish: Literal["stop", "tool_calls", "length", "error"] = "stop"

        async for event in self._model.generate_stream(request):
            progress.started = True
            if ttft_ms == 0:
                ttft_ms = max(1, int((time.monotonic() - started) * 1000))
            if isinstance(event, TextDelta):
                text_parts.append(event.text)
                await self._emitter.emit(
                    ctx.run_id,
                    AssistantDelta(run_id=ctx.run_id, step=step, text=event.text),
                )
            elif isinstance(event, ReasoningDelta):
                # 用于让用户看到长时间推理，也用于线协议重放（ADR 0014）；但特意
                # 不放进 `text`：推理不是答案，绝不能作为 assistant 内容重新进入
                # transcript（对话记录）。
                reasoning_parts.append(event.text)
                await self._emitter.emit(
                    ctx.run_id,
                    AssistantReasoning(run_id=ctx.run_id, step=step, text=event.text),
                )
            elif isinstance(event, ToolCallReady):
                tool_calls.append(event.call)
            elif isinstance(event, UsageReport):
                usage = event.usage
            elif isinstance(event, StreamFinished):
                finish = event.finish_reason

        return ModelResult(
            text="".join(text_parts),
            tool_calls=tuple(tool_calls),
            usage=usage,
            finish_reason="tool_calls" if tool_calls else finish,
            ttft_ms=ttft_ms,
            duration_ms=int((time.monotonic() - started) * 1000),
            provider_reasoning="".join(reasoning_parts),
        )

    def _charge_usage(self, ctx: RunContext, request: ModelRequest, result: ModelResult) -> None:
        usage = result.usage
        estimated = False
        input_tokens = usage.input_tokens
        output_tokens = usage.output_tokens
        if input_tokens == 0 and output_tokens == 0:
            # 提供商没有返回用量：采用保守估算，并明确说明这一点。
            estimated = True
            input_tokens = sum(len(m.content) for m in request.messages) // 4
            output_tokens = max(1, len(result.text) // 4)
        cost = self._pricing.cost(input_tokens, output_tokens, usage.cached_input_tokens)
        ctx.usage = ctx.usage.charge_tokens(
            input_tokens,
            output_tokens,
            cost,
            estimated=estimated,
            cached_input_tokens=usage.cached_input_tokens,
        )

    # -- 持久化 ------------------------------------------------------------------

    async def _checkpoint(self, ctx: RunContext) -> None:
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

    async def _finish(
        self,
        ctx: RunContext,
        status: RunStatus,
        stop_reason: StopReason,
        final_text: str,
        gate_reason: str = "",
    ) -> RunOutcome:
        """唯一出口：每次运行都恰好在这里结束一次。

        持久化最终状态和检查点，并发出 `run.finished`。声称 SUCCEEDED 的调用方已经
        通过证据门禁（见 `_drive` 的最终答案分支）；此方法不会提升状态，只记录决定
        及其停止原因。
        """
        if ctx.status is not status:
            ctx.status = status  # 直接设置：_finish 的目标始终接近终态
        await self._store.update_run_status(ctx.run_id, status, stop_reason.value)
        await self._checkpoint(ctx)
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
                cost_usd=round(ctx.usage.cost_usd, 6),
                cost_known=self._pricing.is_known,
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
            cost_usd=round(ctx.usage.cost_usd, 6),
            cost_known=self._pricing.is_known,
            usage_estimated=ctx.usage.usage_estimated,
            final_text=final_text,
        )


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
