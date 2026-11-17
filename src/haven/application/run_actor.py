"""RunActor：单次运行的自治执行单元。

每个 RunActor 独占生命周期与状态：
- 专属的 RunContext（运行上下文、账本、消息链）；
- 独立的转向指令队列（steer_queue）；
- 独立的临时目录（scratch_dir），执行完毕后安全清理；
- 独立的流水线（ToolPipeline）与卡循环检测器（StuckLoopDetector）；
- 独立的候选答案收敛与证据门禁状态（AnswerAssembly / AnswerResolver）。
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
from collections import deque
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from haven.application.answer_resolution import (
    AnswerAssembly,
    AnswerResolver,
)
from haven.application.approvals import ApprovalResponder
from haven.application.context_builder import ContextBuilder
from haven.application.emitter import EventEmitter
from haven.application.mcp_client import MCPClientManager
from haven.application.mcp_resources import load_imported_resources
from haven.application.model_router import ModelChoice, ModelRouter
from haven.application.model_stream import ModelStreamer
from haven.application.profiles import DEFAULT_PROFILE, ModelProfile, profile_for
from haven.application.registry import ToolRegistry
from haven.application.run_persistence import CheckpointManager, RunFinalizer, RunOutcome
from haven.application.run_telemetry import RunTelemetry
from haven.application.skills import Skill, SkillCatalog
from haven.application.state import RunContext
from haven.application.tool_execution_types import ToolExecution, clip, error_result
from haven.application.tool_pipeline import ToolPipeline
from haven.contracts.capability import CapabilitySnapshot, build_capability_snapshot
from haven.contracts.events import (
    ChildFinished,
    ChildStarted,
    ContextBuilt,
    ModelCompleted,
    Notice,
    RunCreated,
    SteerQueued,
    StepStarted,
    ToolCompleted,
)
from haven.contracts.mcp import MCPServerConfig
from haven.contracts.model import (
    ModelMessage,
    ModelRequest,
    ModelResult,
    ToolCallProposal,
    ToolSchema,
)
from haven.contracts.model_call import ModelCallInfo
from haven.contracts.project_memory import render_project_notes
from haven.contracts.tools import AgentDelegateArgs, RecipeSpec, ToolResult
from haven.domain.budget import Budget, check_accumulated_budget, check_budget
from haven.domain.digest import sha256_text
from haven.domain.enums import (
    AgentMode,
    ApprovalMode,
    EffectState,
    PermissionMode,
    RunStatus,
    StopReason,
    ToolErrorCode,
    ToolStatus,
)
from haven.domain.ids import RunId
from haven.domain.policy import CHILD_READ_TOOLS, PLAN_TOOLS
from haven.domain.pricing import Pricing
from haven.domain.stuck import StuckLoopDetector, call_fingerprint
from haven.ports.executor import ExecutorPort
from haven.ports.model import ModelPort, ProviderError
from haven.ports.sandbox import SandboxLauncher
from haven.ports.session import SessionStorePort
from haven.ports.workspace import WorkspacePort

MAX_CONTEXT_OVERFLOW_RETRIES = 2
CONTEXT_OVERFLOW_SHRINK = 0.6


class RunActor:
    """驱动单次 Run 上下文的自治执行单元。"""

    def __init__(
        self,
        *,
        ctx: RunContext,
        model: ModelPort,
        workspace: WorkspacePort,
        executor: ExecutorPort,
        store: SessionStorePort,
        emitter: EventEmitter,
        approvals: ApprovalResponder,
        recipes: dict[str, RecipeSpec],
        mode: PermissionMode,
        profile: ModelProfile,
        context_chars: int,
        supports_prefix: bool,
        pricing: Pricing,
        telemetry: RunTelemetry,
        checkpoints: CheckpointManager,
        finalizer: RunFinalizer,
        skills: SkillCatalog,
        selected_skills: tuple[Skill, ...] = (),
        launcher: SandboxLauncher | None = None,
        approval_mode: ApprovalMode = ApprovalMode.ASK,
        mcp_manager: MCPClientManager | None = None,
        registry: ToolRegistry | None = None,
        project_guidance: str = "",
        scratch_dir: Path | None = None,
        streamer: ModelStreamer | None = None,
        mcp_servers: tuple[MCPServerConfig, ...] = (),
        pricing_is_override: bool = True,
        context_is_override: bool = False,
        resolve_profile: Callable[[str], ModelProfile] = profile_for,
    ) -> None:
        self._ctx = ctx
        self._model = model
        self._streamer = streamer if streamer is not None else ModelStreamer(emitter)
        self._workspace = workspace
        self._executor = executor
        self._store = store
        self._emitter = emitter
        self._approvals = approvals
        self._recipes = recipes
        self._mode = mode
        self._profile = profile
        self._profile_for = resolve_profile
        self._context_chars = context_chars
        self._context_is_override = context_is_override
        self._overflow_caps: dict[str, int] = {}
        if isinstance(model, ModelRouter):
            supports_prefix = supports_prefix and all(
                self._profile_for(port.model_name).supports_assistant_prefix
                for port in (model.fast_model, model.deep_model)
            )
        self._supports_prefix = supports_prefix
        self._pricing = pricing
        self._pricing_is_override = pricing_is_override
        self._telemetry = telemetry
        self._checkpoints = checkpoints
        self._finalizer = finalizer
        self._skills = skills
        self._selected_skills = selected_skills
        self._launcher = launcher
        self._project_guidance = project_guidance
        self._mcp_servers = mcp_servers
        self._mcp_manager = mcp_manager
        self._registry = registry if registry is not None else ToolRegistry()

        self._steer_queue: deque[str] = deque()
        self._is_driving: bool = False
        self._closed: bool = False

        if scratch_dir is not None:
            self._scratch_dir = scratch_dir
            self._owns_scratch = False
        else:
            self._scratch_dir = Path(tempfile.mkdtemp(prefix=f"haven-scratch-{ctx.run_id[:8]}-"))
            self._owns_scratch = True

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
            skills=skills,
            approval_mode=approval_mode,
            mcp_manager=mcp_manager,
            delegate=self._delegate,
        )

        self._answers = AnswerResolver(
            emitter=emitter,
            workspace=workspace,
            verification_available=lambda: bool(self._recipes),
            supports_prefix=self._supports_prefix,
            finish=self.finish,
            checkpoint=self.checkpoint,
        )

        mcp_tools = self._mcp_manager.tools if self._mcp_manager else ()
        self._snapshot = build_capability_snapshot(mcp_tools)
        if not self._ctx.catalog_digest:
            self._ctx.catalog_digest = self._snapshot.catalog_digest
        self._ctx.capabilities = self._snapshot

        self._builder = ContextBuilder(
            goal=ctx.goal,
            tools=self._model_tools(),
            budget=ctx.budget,
            recipe_ids=tuple(self._recipes),
            project_guidance=self._project_guidance,
            project_notes=(
                render_project_notes(ctx.guidance_snapshot.notes) if ctx.guidance_snapshot else ""
            ),
            verification=ctx.verification,
            skills=self._skills.skills,
            selected_skills=selected_skills,
            imported_resources=() if ctx.parent_run_id else load_imported_resources(workspace.root),
            sandbox_backend=self._launcher.backend if self._launcher is not None else "",
            max_context_chars=self._context_chars,
            reasoning_effort=self._profile.reasoning_effort,
            agent_mode=ctx.agent_mode,
            investigation=bool(ctx.parent_run_id),
        )

        self._stuck = StuckLoopDetector()
        self._answer = AnswerAssembly()

    # -- 属性访问器 -----------------------------------------------------------

    def _model_tools(self) -> tuple[ToolSchema, ...]:
        """规划仅暴露可观察工具；流水线仍独立检查未暴露的越权调用。"""
        tools = self._snapshot.to_tool_schemas()
        if self._ctx.parent_run_id:
            return tuple(tool for tool in tools if tool.name in CHILD_READ_TOOLS)
        if self._ctx.agent_mode is not AgentMode.PLAN:
            return tools
        read_only_mcp = {
            tool.namespaced_name
            for tool in (self._mcp_manager.tools if self._mcp_manager else ())
            if tool.is_read_only
        }
        return tuple(tool for tool in tools if tool.name in PLAN_TOOLS | read_only_mcp)

    async def _delegate(
        self, parent: RunContext, call: ToolCallProposal, args: AgentDelegateArgs
    ) -> ToolExecution:
        scope = []
        for path in args.file_scope or ():
            facts = self._workspace.path_facts(path)
            if not facts.within_workspace or facts.is_protected:
                return ToolExecution(
                    error_result(call, ToolErrorCode.DENIED, "invalid child scope")
                )
            scope.append(facts.normalized)
        remaining = (
            parent.budget.max_wall_time_seconds - parent.usage.wall_time_seconds,
            parent.budget.max_input_tokens - parent.usage.input_tokens,
            parent.budget.max_output_tokens - parent.usage.output_tokens,
            parent.budget.max_cost_usd - parent.usage.cost_usd,
        )
        if min(remaining) <= 0:
            return ToolExecution(
                error_result(call, ToolErrorCode.DENIED, "parent budget exhausted")
            )
        budget = Budget(
            max_steps=min(args.max_steps, parent.budget.max_steps - parent.usage.steps - 1),
            max_tool_calls=min(
                args.max_tool_calls, parent.budget.max_tool_calls - parent.usage.tool_calls - 1
            ),
            max_wall_time_seconds=remaining[0],
            max_input_tokens=int(remaining[1]),
            max_output_tokens=int(remaining[2]),
            max_cost_usd=remaining[3],
        )
        child_id = RunId(
            "child-" + sha256_text(f"{parent.run_id}:{parent.usage.steps}:{call.call_id}")[:20]
        )
        goal = (
            f"You are a read-only {args.role}. Investigate this task: {args.task}\n"
            "Read the relevant code and give substantive findings with file and line citations. "
            "For reviews, identify defects and their consequences; state uncertainty honestly. "
            "Do not delegate, ask the user, modify files, or run processes.\n"
            f"Allowed paths: {', '.join(scope) if scope else 'the workspace'}. "
            "When scoped, pass one allowed path explicitly to every search/read tool."
        )
        child = RunContext(
            run_id=child_id,
            goal=goal,
            mode=PermissionMode.READ_ONLY,
            budget=budget,
            parent_run_id=parent.run_id,
            root_run_id=parent.root_run_id or parent.run_id,
            file_scope=tuple(scope),
            model_selection=parent.model_selection,
        )
        await self._emitter.emit(
            parent.run_id,
            ChildStarted(
                child_run_id=child_id,
                parent_run_id=parent.run_id,
                role=args.role,
                task=args.task,
                allocated_steps=budget.max_steps,
                allocated_tool_calls=budget.max_tool_calls,
            ),
        )
        await self._store.create_run(
            child_id,
            str(self._workspace.root),
            self._workspace.workspace_digest,
            goal,
            child.mode.value,
        )
        await self._emitter.emit(
            child_id,
            RunCreated(
                run_id=child_id,
                workspace=str(self._workspace.root),
                workspace_digest=self._workspace.workspace_digest,
                goal=goal,
                mode=child.mode.value,
                model_name=self._model.model_name,
                model_preset=child.model_selection.preset_name if child.model_selection else "",
                parent_run_id=parent.run_id,
                max_steps=budget.max_steps,
            ),
        )
        checkpoints = CheckpointManager(
            self._store, self._workspace, self._emitter, include_originals=False
        )
        actor = RunActor(
            ctx=child,
            model=self._model,
            workspace=self._workspace,
            executor=self._executor,
            store=self._store,
            emitter=self._emitter,
            approvals=self._approvals,
            recipes={},
            mode=PermissionMode.READ_ONLY,
            profile=self._profile,
            context_chars=self._context_chars,
            supports_prefix=self._supports_prefix,
            pricing=self._pricing,
            pricing_is_override=self._pricing_is_override,
            context_is_override=self._context_is_override,
            resolve_profile=self._profile_for,
            telemetry=RunTelemetry(self._emitter, self._pricing),
            checkpoints=checkpoints,
            finalizer=RunFinalizer(
                self._store, self._emitter, checkpoints, cost_known=self._pricing.is_known
            ),
            skills=SkillCatalog(),
        )
        outcome = None
        try:
            outcome = await actor.drive()
        finally:
            usage = child.usage
            parent.usage = replace(
                parent.usage,
                steps=parent.usage.steps + usage.steps,
                tool_calls=parent.usage.tool_calls + usage.tool_calls,
            ).charge_tokens(
                usage.input_tokens,
                usage.output_tokens,
                usage.cost_usd,
                estimated=usage.usage_estimated,
                cached_input_tokens=usage.cached_input_tokens,
                cost_known=usage.cost_known,
            )
            await self._emitter.emit(
                parent.run_id,
                ChildFinished(
                    child_run_id=child_id,
                    parent_run_id=parent.run_id,
                    role=args.role,
                    status=outcome.status.value if outcome else "cancelled",
                    steps_used=usage.steps,
                    tool_calls_used=usage.tool_calls,
                    outcome_summary=clip(outcome.final_text if outcome else "cancelled", 300),
                ),
            )
        return ToolExecution(
            ToolResult(
                call_id=call.call_id,
                tool_name=call.tool_name,
                status=ToolStatus.OK if outcome.status is RunStatus.SUCCEEDED else ToolStatus.ERROR,
                error_code=None
                if outcome.status is RunStatus.SUCCEEDED
                else ToolErrorCode.INTERNAL,
                payload={
                    "role": args.role,
                    "child_run_id": child_id,
                    "status": outcome.status.value,
                    "answer": clip(outcome.final_text, 6000),
                    "evidence": [
                        {"path": path, "digest": digest}
                        for path, digest in child.files_read.items()
                    ],
                },
            )
        )

    @property
    def ctx(self) -> RunContext:
        """本次执行绑定的 RunContext。"""
        return self._ctx

    @property
    def capabilities(self) -> CapabilitySnapshot:
        """本次执行冻结的能力快照。"""
        return self._snapshot

    @property
    def catalog_digest(self) -> str:
        """本次执行冻结的能力快照摘要。"""
        return self._snapshot.catalog_digest

    @property
    def run_id(self) -> str:
        """本次运行的唯一标识符。"""
        return self._ctx.run_id

    @property
    def scratch_dir(self) -> Path:
        """本次运行专用的临时目录。"""
        return self._scratch_dir

    @property
    def steer_queue(self) -> deque[str]:
        """本次运行的人工转向指令队列。"""
        return self._steer_queue

    @property
    def is_closed(self) -> bool:
        """Actor 是否已被关闭。"""
        return self._closed

    @property
    def approval_mode(self) -> ApprovalMode:
        """当前的审批分级模式（ask / smart / full）。"""
        return self._pipeline.approval_mode

    def set_approval_mode(self, mode: ApprovalMode) -> None:
        """更新本运行的审批分级模式。"""
        self._pipeline.set_approval_mode(mode)

    # -- Steering 与生命周期 --------------------------------------------------

    async def steer(self, text: str) -> bool:
        """为本运行排队用户输入，在下一个模型调用轮次边界投递。"""
        text = text.strip()
        if not text or self._closed or not self._is_driving:
            return False
        if len(text) > 4000:
            raise ValueError("steering text must be at most 4000 characters")
        self._steer_queue.append(text)
        await self._emitter.emit(
            self._ctx.run_id,
            SteerQueued(run_id=self._ctx.run_id, text=text),
        )
        return True

    def drain_steering(self) -> list[str]:
        """排空并返回当前所有排队的转向输入。"""
        drained: list[str] = []
        while self._steer_queue:
            drained.append(self._steer_queue.popleft())
        return drained

    async def checkpoint(self, ctx: RunContext) -> None:
        """记录状态检查点。"""
        ctx.refresh_wall_time(time.monotonic())
        await self._checkpoints.save(ctx)

    async def finish(
        self,
        ctx: RunContext,
        status: RunStatus,
        stop_reason: StopReason,
        final_text: str,
        gate_reason: str = "",
    ) -> RunOutcome:
        """关闭工具流水线并收口运行终态与证据门禁。"""
        await self._pipeline.close()
        ctx.refresh_wall_time(time.monotonic())
        return await self._finalizer.finish(ctx, status, stop_reason, final_text, gate_reason)

    async def close(self) -> None:
        """安全释放流水线资源与本 Actor 专属的临时目录。"""
        if self._closed:
            return
        self._closed = True
        try:
            await self._pipeline.close()
        finally:
            if self._owns_scratch and self._scratch_dir.exists():
                shutil.rmtree(self._scratch_dir, ignore_errors=True)

    # -- 主循环执行 -----------------------------------------------------------

    async def drive(self) -> RunOutcome:
        """执行本轮次的 Model -> Tool -> Evidence -> Checkpoint 循环。"""
        ctx = self._ctx
        if self._mcp_servers and self._mcp_manager is not None and not self._mcp_manager.tools:
            mcp_tools = await self._mcp_manager.initialize()
            self._registry.register_mcp_tools(mcp_tools)

        envelope_digest = ""
        ctx.start_timing(time.monotonic())
        answer = self._answer
        stuck = self._stuck

        self._is_driving = True
        try:
            while True:
                ctx.refresh_wall_time(time.monotonic())
                if stop := check_budget(ctx.budget, ctx.usage):
                    return await self.finish(ctx, RunStatus.STOPPED, stop, answer.final_text)

                ctx.usage = ctx.usage.charge_step()
                step = ctx.usage.steps
                if ctx.status is RunStatus.CREATED:
                    ctx.move_to(RunStatus.RUNNING_MODEL)
                await self._emitter.emit(ctx.run_id, StepStarted(run_id=ctx.run_id, step=step))

                for steered in self.drain_steering():
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

                choice = self._choose_model(ctx, step)
                model_name = choice.model.model_name
                profile = (
                    self._profile_for(model_name)
                    if isinstance(self._model, ModelRouter)
                    else self._profile
                )
                max_chars = self._context_chars
                if not self._context_is_override:
                    max_chars = min(max_chars, profile.max_context_chars)
                max_chars = min(max_chars, self._overflow_caps.get(model_name, max_chars))
                self._builder.configure_model(max_chars, profile.reasoning_effort)
                step_outcome = await self._call_model_step(
                    ctx, step, choice.model, self._builder, envelope_digest, answer.final_text
                )
                if isinstance(step_outcome, RunOutcome):
                    return step_outcome
                request, result, envelope_digest = step_outcome

                pricing = self._pricing if self._pricing_is_override else profile.pricing
                charge = self._telemetry.charge_usage(ctx, request, result, pricing=pricing)
                usage = charge.usage
                pricing_source = (
                    "user" if self._pricing_is_override else f"model-profile:{profile.name}"
                )
                ctx.refresh_wall_time(time.monotonic())
                await self._emitter.emit(
                    ctx.run_id,
                    ModelCompleted(
                        run_id=ctx.run_id,
                        step=step,
                        text=result.text,
                        tool_call_count=len(result.tool_calls),
                        input_tokens=usage.input_tokens,
                        output_tokens=usage.output_tokens,
                        usage_estimated=usage.estimated,
                        ttft_ms=result.ttft_ms,
                        duration_ms=result.duration_ms,
                        finish_reason=result.finish_reason,
                        reasoning_tokens=usage.reasoning_tokens,
                        cached_input_tokens=usage.cached_input_tokens,
                        model_info=ModelCallInfo(
                            model_name=model_name,
                            selection_reason=choice.reason,
                            profile_name=profile.name,
                            profile_source="fallback"
                            if profile.name == DEFAULT_PROFILE.name
                            else "builtin",
                            context_chars=self._builder.max_context_chars,
                            context_window_tokens=profile.context_window_tokens,
                            output_limit_tokens=profile.output_limit_tokens,
                            reasoning_supported=profile.reasoning_supported,
                            reasoning_effort=request.reasoning_effort,
                            reasoning_replay=profile.requires_tool_call_reasoning,
                            prefix_continuation=self._supports_prefix,
                            usage_source="estimated" if usage.estimated else "provider",
                            cost_usd=charge.cost_usd,
                            cost_known=charge.cost_known,
                            pricing_source=pricing_source if charge.cost_known else "unknown",
                        ),
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
                    return await self.finish(ctx, RunStatus.STOPPED, stop, answer.final_text)

                if result.tool_calls:
                    answer.reset_partial()
                    stopped = await self.handle_tool_calls(
                        step, result.tool_calls, stuck, builder=self._builder
                    )
                    await self.checkpoint(ctx)
                    if stopped is not None:
                        return stopped
                    continue

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
            for record in started:
                await self._store.update_execution_state(
                    ctx.run_id, record.call_id, EffectState.EFFECT_UNKNOWN
                )
            uncertain = bool(started) or any(
                record.effect_state is EffectState.EFFECT_UNKNOWN for record in executions
            )
            if uncertain:
                await self.finish(
                    ctx,
                    RunStatus.EFFECT_UNKNOWN,
                    StopReason.EFFECT_UNKNOWN,
                    answer.final_text,
                )
            else:
                await self.finish(ctx, RunStatus.CANCELLED, StopReason.CANCELLED, answer.final_text)
            raise
        finally:
            self._is_driving = False
            self.drain_steering()
            await self.close()

    async def _call_model_step(
        self,
        ctx: RunContext,
        step: int,
        model: ModelPort,
        builder: ContextBuilder,
        envelope_digest: str,
        answer_final_text: str,
    ) -> tuple[ModelRequest, ModelResult, str] | RunOutcome:
        """构建上下文、记录遥测并向模型发起流式请求，对 context_overflow 进行有界恢复。"""
        overflow_retries = 0
        while True:
            request, segments = builder.build(
                ctx.transcript, ctx.usage, ctx.plan, handoff=ctx.handoff, ledger=ctx.ledger
            )
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
                    result = await self._streamer.stream(model, ctx, step, request)
                return request, result, envelope_digest
            except TimeoutError:
                ctx.refresh_wall_time(time.monotonic())
                return await self.finish(
                    ctx,
                    RunStatus.STOPPED,
                    StopReason.WALL_TIME_EXHAUSTED,
                    answer_final_text,
                )
            except ProviderError as exc:
                if (
                    exc.code == "context_overflow"
                    and overflow_retries < MAX_CONTEXT_OVERFLOW_RETRIES
                ):
                    overflow_retries += 1
                    new_budget = builder.reduce_budget(CONTEXT_OVERFLOW_SHRINK)
                    self._overflow_caps[model.model_name] = new_budget
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
                        message=f"provider error ({exc.code}): {exc} [model {model.model_name}]",
                    ),
                )
                return await self.finish(
                    ctx, RunStatus.FAILED, StopReason.PROVIDER_ERROR, answer_final_text
                )

    def _choose_model(self, ctx: RunContext, step: int) -> ModelChoice:
        if isinstance(self._model, ModelRouter):
            return self._model.choose_model(ctx, step)
        return ModelChoice(self._model, "single")

    async def handle_tool_calls(
        self,
        step: int,
        calls: tuple[ToolCallProposal, ...],
        stuck: StuckLoopDetector | None = None,
        builder: ContextBuilder | None = None,
    ) -> RunOutcome | None:
        """严格按顺序通过流水线执行一轮中的工具调用。"""
        detector = stuck if stuck is not None else self._stuck
        ctx = self._ctx
        # 追问是本批次的屏障：任何同批提案都必须等用户回答后重新提出。
        ask = next((call for call in calls if call.tool_name == "user.ask"), None)
        if ask is not None:
            for skipped in calls:
                if skipped is ask:
                    continue
                result = error_result(
                    skipped,
                    ToolErrorCode.DENIED,
                    "Not executed: user.ask pauses this batch. Reissue after the user answers.",
                )
                ctx.transcript.append(
                    ModelMessage(
                        role="tool", content=result.to_model_text(), tool_call_id=skipped.call_id
                    )
                )
                await self._emitter.emit(
                    ctx.run_id,
                    ToolCompleted(
                        run_id=ctx.run_id,
                        call_id=skipped.call_id,
                        tool_name=skipped.tool_name,
                        status="error",
                        error_code="denied",
                        summary="deferred until user input",
                        truncated=False,
                        duration_ms=0,
                    ),
                )
            calls = (ask,)
        for call in calls:
            if ctx.usage.tool_calls >= ctx.budget.max_tool_calls:
                return await self.finish(
                    ctx, RunStatus.STOPPED, StopReason.TOOL_BUDGET_EXHAUSTED, ""
                )
            ctx.usage = ctx.usage.charge_tool_call()
            ctx.move_to(RunStatus.VALIDATING_TOOL)

            execution = await self._pipeline.execute(ctx, step, call)
            ctx.refresh_wall_time(time.monotonic())
            result = execution.result
            if ctx.pending_input is not None:
                # 工具回复在收到答案时补齐；暂停期间不会构建不完整的模型请求。
                await self._pipeline.close()
                ctx.refresh_wall_time(time.monotonic())
                return await self._finalizer.pause_for_input(ctx)
            if (
                call.tool_name == "recipe.propose"
                and result.status is ToolStatus.OK
                and builder is not None
            ):
                for r_id, r_spec in self._pipeline.recipes.items():
                    self._recipes[r_id] = r_spec
                    builder.register_recipe(r_id)
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
                return await self.finish(
                    ctx, RunStatus.EFFECT_UNKNOWN, StopReason.EFFECT_UNKNOWN, ""
                )

            if ctx.usage.wall_time_seconds >= ctx.budget.max_wall_time_seconds:
                return await self.finish(
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
            if detector.observe(fingerprint):
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
                return await self.finish(ctx, RunStatus.STOPPED, StopReason.NO_PROGRESS, "")
        return None
