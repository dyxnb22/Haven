"""唯一的工具执行通道。

模型提出的每个操作都按以下顺序经过：
Registry -> Schema 验证 -> 工作区事实 -> 确定性策略
-> 精确审批（需要时）-> 执行票据 -> Executor
-> ToolResult + Evidence + Trace。

模型提案不存在通往副作用的其他路径。
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from haven.application.approval_cards import (
    ApprovalCardRenderer,
    CardHandler,
    ToolPreview,
)
from haven.application.approval_coordinator import ApprovalCoordinator
from haven.application.approvals import ApprovalResponder
from haven.application.emitter import EventEmitter
from haven.application.registry import ToolRegistry, ValidationFailure
from haven.application.state import RunContext
from haven.application.tool_execution import (
    ExecuteHandler,
    ToolExecutor,
    _clip,
    _error,
    _map_ws_code,
)
from haven.application.tool_execution import ToolExecution as ToolExecution
from haven.application.tool_facts import FactsHandler, ToolFactsCollector
from haven.contracts.events import (
    ExecutionStarted,
    PolicyDecided,
    ToolProposed,
)
from haven.contracts.model import ToolCallProposal
from haven.contracts.tools import (
    RecipeSpec,
    RepoDeleteArgs,
    RepoEditArgs,
    RepoMoveArgs,
    ToolArgs,
    ToolResult,
)
from haven.domain.digest import canonical_json
from haven.domain.enums import (
    PermissionMode,
    PolicyDecision,
    RunStatus,
    ToolErrorCode,
)
from haven.domain.ids import ApprovalId, ToolCallId
from haven.domain.policy import ToolFacts, evaluate_policy
from haven.domain.ticket import mint_ticket
from haven.ports.executor import ExecutorPort
from haven.ports.sandbox import SandboxLauncher
from haven.ports.session import SessionStorePort
from haven.ports.workspace import (
    PatchPreview,
    WorkspaceError,
    WorkspacePort,
)


#: 每个工具的处理器形状。两张表都以已注册工具名为键，并在
#: `ToolPipeline.__init__` 中构建；单元测试固定要求 `ARGS_MODELS` 中的每个
#: 工具在两张表中都恰好有一个处理器，因此新增工具却没有接线时会明确失败，
#: 而不是静默地落入默认分支。
class ToolPipeline:
    """每个 RunService 对应一个实例；`execute` 端到端执行一个提案。

    `execute` 内的编号注释与模块文档字符串中的阶段顺序一致（1-2 registry/schema，
    3 facts，4 policy，5 approval，6 ticket，7 execute + evidence）。在第 7 阶段，
    读取工具执行时不写入执行日志（没有需要恢复的副作用）；写入工具会在实际 I/O 前后
    记录 STARTED -> CONFIRMED/FAILED，因此两者之间发生崩溃时可以分类；`repo.exec` /
    `repo.check` 还会在前后对目录树做快照，以归因进程写入（ADR 0012），并在受保护路径
    被篡改时让调用失败（ADR 0018）。
    """

    def __init__(
        self,
        *,
        workspace: WorkspacePort,
        executor: ExecutorPort,
        store: SessionStorePort,
        emitter: EventEmitter,
        approvals: ApprovalResponder,
        registry: ToolRegistry,
        recipes: dict[str, RecipeSpec],
        mode: PermissionMode,
        launcher: SandboxLauncher | None = None,
        scratch_dir: Path | None = None,
    ) -> None:
        self._workspace = workspace
        self._executor = executor
        self._store = store
        self._emitter = emitter
        self._approvals = approvals
        self._registry = registry
        self._recipes = recipes
        self._mode = mode
        self._launcher = launcher
        self._scratch_dir = scratch_dir or Path(tempfile.gettempdir()) / "haven-scratch"
        self._tool_executor = ToolExecutor(
            workspace=workspace,
            executor=executor,
            store=store,
            emitter=emitter,
            recipes=recipes,
            launcher=launcher,
            scratch_dir=self._scratch_dir,
        )
        # 每个工具一行，覆盖两个阶段。单元测试
        # test_every_registered_tool_is_fully_wired 会将这些表与 ARGS_MODELS 固定
        # 对照，因此“新增工具”意味着：参数模型 + 策略类 + 每个阶段在这里各一行；
        # 忘记某一行会使整个测试套件失败，而不是让运行在生产中才失败。
        self._facts_collector = ToolFactsCollector(workspace, recipes, launcher)
        # 兼容现有接线测试；映射的所有权在 collector。
        self._facts_handlers: dict[str, FactsHandler] = self._facts_collector.handlers
        # 用户针对每个工具看到的内容。键的形式与上面两张表相同，但只覆盖
        # 可以 ASK 的工具：只读工具和状态工具永远不会进入审批流程。
        # 由 test_every_ask_tool_has_an_approval_card 固定检查。
        self._card_renderer = ApprovalCardRenderer(recipes, self._tool_executor.describe_sandbox)
        self._approval_coordinator = ApprovalCoordinator(
            workspace=workspace,
            store=store,
            emitter=emitter,
            approvals=approvals,
            registry=registry,
            renderer=self._card_renderer,
            mode=mode,
        )
        # 兼容现有接线测试；映射的所有权在 renderer。
        self._card_handlers: dict[str, CardHandler] = self._card_renderer.handlers
        # 兼容现有接线测试；映射的所有权在 executor。
        self._execute_handlers: dict[str, ExecuteHandler] = self._tool_executor.handlers

    def replace_scratch_dir(self, scratch_dir: Path) -> None:
        """在连续运行之间替换进程工具使用的独占临时目录。"""
        self._scratch_dir = scratch_dir
        self._tool_executor.replace_scratch_dir(scratch_dir)

    async def execute(self, ctx: RunContext, step: int, call: ToolCallProposal) -> ToolExecution:
        """沿唯一安全流水线处理一个模型工具提议并返回观察结果。"""
        started = time.monotonic()
        await self._emitter.emit(
            ctx.run_id,
            ToolProposed(
                run_id=ctx.run_id,
                step=step,
                call_id=call.call_id,
                tool_name=call.tool_name,
                args_summary=_clip(call.arguments_json, 200),
            ),
        )

        # 1-2. 注册表查找 + 严格 schema 校验。
        validated = self._registry.validate(call.tool_name, call.arguments_json)
        if isinstance(validated, ValidationFailure):
            code = (
                ToolErrorCode.UNKNOWN_TOOL
                if validated.code == "unknown_tool"
                else ToolErrorCode.INVALID_ARGUMENTS
            )
            return await self._finish(ctx, call, _error(call, code, validated.message), started)

        # 3. 程序收集的工作区事实（绝不由模型控制）。
        try:
            facts, preview = await self._collect_facts(ctx, call, validated)
        except WorkspaceError as exc:
            return await self._finish(ctx, call, _error(call, _map_ws_code(exc), str(exc)), started)

        # 4. 确定性策略。
        outcome = evaluate_policy(self._mode, facts)
        await self._emitter.emit(
            ctx.run_id,
            PolicyDecided(
                run_id=ctx.run_id,
                call_id=call.call_id,
                decision=outcome.decision.value,
                reason_code=outcome.reason_code,
                risk=outcome.risk.value,
            ),
        )
        if outcome.decision is PolicyDecision.DENY:
            return await self._finish(
                ctx,
                call,
                _error(call, ToolErrorCode.DENIED, f"denied by policy: {outcome.reason_code}"),
                started,
            )

        # 5. 策略返回 ASK 时进行精确审批。
        approval_id: str | None = None
        canonical_args = canonical_json(validated.model_dump())
        preimage = facts.preimage_digest
        if outcome.decision is PolicyDecision.ASK:
            approved, approval_id, message = await self._ask_approval(
                ctx, call, validated, canonical_args, preview, facts
            )
            if not approved:
                return await self._finish(
                    ctx,
                    call,
                    _error(call, ToolErrorCode.APPROVAL_REJECTED, message),
                    started,
                )
            # 在用户作出决定后重新验证 preimage（TOCTOU 防护）。所有在审批时
            # 固定文件内容的工具——edit、delete、move 的源文件以及 patch 中的
            # 每个文件——都与当前磁盘内容重新比较，因此审批和执行之间发生
            # 变化时会失败关闭。
            guarded_path: str | None = None
            if isinstance(validated, RepoEditArgs | RepoDeleteArgs):
                guarded_path = validated.path
            elif isinstance(validated, RepoMoveArgs):
                guarded_path = validated.src
            if guarded_path is not None:
                current = self._workspace.path_facts(guarded_path)
                if current.digest != preimage:
                    return await self._finish(
                        ctx,
                        call,
                        _error(
                            call,
                            ToolErrorCode.STALE_PREIMAGE,
                            "file changed between approval and execution",
                        ),
                        started,
                    )
            if isinstance(preview, PatchPreview):
                stale = [
                    path
                    for path, digest in preview.preimages.items()
                    if self._workspace.path_facts(path).digest != digest
                ] + [
                    effect.path
                    for effect in preview.effects
                    if effect.tool_shape == "repo.create"
                    and self._workspace.path_facts(effect.path).exists
                ]
                if stale:
                    return await self._finish(
                        ctx,
                        call,
                        _error(
                            call,
                            ToolErrorCode.STALE_PREIMAGE,
                            "file(s) changed between approval and execution: "
                            + ", ".join(sorted(stale)),
                        ),
                        started,
                    )

        # 6. 铸造执行票据；原始模型 JSON 在这里停止流动。
        if ctx.status is not RunStatus.EXECUTING_TOOL:
            ctx.move_to(RunStatus.EXECUTING_TOOL)
        ticket = mint_ticket(
            call_id=ToolCallId(call.call_id),
            tool_name=call.tool_name,
            tool_version=self._registry.version,
            canonical_args_json=canonical_args,
            workspace_digest=self._workspace.workspace_digest,
            preimage_digest=preimage,
            approval_id=ApprovalId(approval_id) if approval_id is not None else None,
        )
        await self._emitter.emit(
            ctx.run_id,
            ExecutionStarted(
                run_id=ctx.run_id,
                call_id=call.call_id,
                tool_name=call.tool_name,
                ticket_digest=ticket.ticket_digest,
                sandbox_backend=self._launcher.backend if self._launcher is not None else "",
            ),
        )

        # 7. 执行并确认事实。任何工作区失败都会变成结构化 ToolResult：不变量是
        # 工具调用绝不会向代理循环抛出异常，因此一条错误路径不会终止整个运行。
        try:
            execution = await self._run_ticketed(
                ctx, call, validated, ticket.ticket_digest, preview
            )
        except WorkspaceError as exc:
            return await self._finish(ctx, call, _error(call, _map_ws_code(exc), str(exc)), started)
        return await self._finish(ctx, call, execution.result, started, execution.effect_unknown)

    # -- 事实 -------------------------------------------------------------------

    async def _collect_facts(
        self, ctx: RunContext, call: ToolCallProposal, args: ToolArgs
    ) -> tuple[ToolFacts, ToolPreview]:
        return await self._facts_collector.collect(ctx, call, args)

    async def _ask_approval(
        self,
        ctx: RunContext,
        call: ToolCallProposal,
        args: ToolArgs,
        canonical_args: str,
        preview: ToolPreview,
        facts: ToolFacts,
    ) -> tuple[bool, str | None, str]:
        return await self._approval_coordinator.ask(ctx, call, args, canonical_args, preview, facts)

    async def _run_ticketed(
        self,
        ctx: RunContext,
        call: ToolCallProposal,
        args: ToolArgs,
        ticket_digest: str,
        preview: ToolPreview,
    ) -> ToolExecution:
        return await self._tool_executor.run_ticketed(ctx, call, args, ticket_digest, preview)

    async def _finish(
        self,
        ctx: RunContext,
        call: ToolCallProposal,
        result: ToolResult,
        started: float,
        effect_unknown: bool = False,
    ) -> ToolExecution:
        return await self._tool_executor.finish(ctx, call, result, started, effect_unknown)
