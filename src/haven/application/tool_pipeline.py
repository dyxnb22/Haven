"""唯一的工具执行通道。

模型提出的每个操作都按以下顺序经过：
Registry -> Schema 验证 -> 工作区事实 -> 确定性策略
-> 精确审批（需要时）-> 执行票据 -> Executor
-> ToolResult + Evidence + Trace。

模型提案不存在通往副作用的其他路径。
"""

from __future__ import annotations

import asyncio
import json
import shlex
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from haven.application.approvals import ApprovalResponder
from haven.application.emitter import EventEmitter
from haven.application.registry import ToolRegistry, ValidationFailure
from haven.application.state import RunContext
from haven.contracts.events import (
    ApprovalDecided,
    ApprovalRequested,
    DiffPreview,
    EvidenceRecorded,
    ExecutionStarted,
    Notice,
    PlanStepView,
    PlanUpdated,
    PolicyDecided,
    ToolCompleted,
    ToolProposed,
)
from haven.contracts.model import ToolCallProposal
from haven.contracts.tools import (
    PatchCreateOp,
    PatchDeleteOp,
    PatchEditOp,
    PatchMoveOp,
    RecipeSpec,
    RepoApplyPatchArgs,
    RepoCheckArgs,
    RepoCreateArgs,
    RepoDeleteArgs,
    RepoEditArgs,
    RepoExecArgs,
    RepoListArgs,
    RepoMoveArgs,
    RepoReadArgs,
    RepoSearchArgs,
    TaskPlanArgs,
    ToolArgs,
    ToolResult,
)
from haven.domain.approval import ApprovalRequest, compute_approval_digest
from haven.domain.digest import canonical_json, sha256_text
from haven.domain.enums import (
    ApprovalDecision,
    EffectState,
    PermissionMode,
    PolicyDecision,
    RunStatus,
    ToolErrorCode,
    ToolStatus,
)
from haven.domain.evidence import CheckEvidence, DiffEvidence, EditEvidence
from haven.domain.exec_policy import ExecClass, classify_argv
from haven.domain.ids import ApprovalId, ToolCallId, new_approval_id
from haven.domain.policy import ToolFacts, evaluate_policy
from haven.domain.ticket import mint_ticket
from haven.ports.executor import ExecSpec, ExecutorPort
from haven.ports.sandbox import (
    SandboxLauncher,
    SandboxSpec,
    default_private_roots,
    default_readable_roots,
)
from haven.ports.session import ExecutionRecord, SessionStorePort
from haven.ports.workspace import (
    EditPreview,
    PatchOpSpec,
    PatchPreview,
    PatchRollbackError,
    WorkspaceError,
    WorkspacePort,
    WorkspaceSnapshot,
)

#: 工具提案的预览形式：单文件 diff、完整补丁，或无预览（只读工具）。
ToolPreview = EditPreview | PatchPreview | None

#: 每个工具的处理器形状。两张表都以已注册工具名为键，并在
#: `ToolPipeline.__init__` 中构建；单元测试固定要求 `ARGS_MODELS` 中的每个
#: 工具在两张表中都恰好有一个处理器，因此新增工具却没有接线时会明确失败，
#: 而不是静默地落入默认分支。
FactsHandler = Callable[
    ["RunContext", ToolCallProposal, ToolArgs],
    Awaitable[tuple[ToolFacts, ToolPreview]],
]
ExecuteHandler = Callable[
    ["RunContext", ToolCallProposal, ToolArgs, str, ToolPreview],
    Awaitable["ToolExecution"],
]
#: 渲染一张审批卡片：（摘要行，预览正文）。
CardHandler = Callable[[ToolArgs, ToolPreview], tuple[str, str]]

MODEL_PAYLOAD_CHARS = 8_000
PREVIEW_CHARS = 4_000

_ERROR_CODES: dict[str, ToolErrorCode] = {
    "denied": ToolErrorCode.DENIED,
    "not_found": ToolErrorCode.NOT_FOUND,
    "invalid_arguments": ToolErrorCode.INVALID_ARGUMENTS,
    "stale_preimage": ToolErrorCode.STALE_PREIMAGE,
    "ambiguous_match": ToolErrorCode.AMBIGUOUS_MATCH,
    "timeout": ToolErrorCode.TIMEOUT,
    "internal": ToolErrorCode.INTERNAL,
}


@dataclass(frozen=True, slots=True)
class ToolExecution:
    """一次流水线处理返回给运行循环的内容：提供给模型的结构化结果，以及循环必须
    处理的一个标志——`effect_unknown` 会停止运行，使恢复流程能够对中断的副作用分类。"""

    result: ToolResult
    effect_unknown: bool = False


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
        # 每个工具一行，覆盖两个阶段。单元测试
        # test_every_registered_tool_is_fully_wired 会将这些表与 ARGS_MODELS 固定
        # 对照，因此“新增工具”意味着：参数模型 + 策略类 + 每个阶段在这里各一行；
        # 忘记某一行会使整个测试套件失败，而不是让运行在生产中才失败。
        self._facts_handlers: dict[str, FactsHandler] = {
            "repo.list": self._facts_path_read,
            "repo.search": self._facts_path_read,
            "repo.read": self._facts_path_read,
            "repo.edit": self._facts_edit,
            "repo.create": self._facts_create,
            "repo.delete": self._facts_delete,
            "repo.move": self._facts_move,
            "repo.apply_patch": self._facts_patch,
            "repo.exec": self._facts_exec,
            "repo.check": self._facts_check,
            "repo.diff": self._facts_stateless,
            "task.plan": self._facts_stateless,
        }
        # 用户针对每个工具看到的内容。键的形式与上面两张表相同，但只覆盖
        # 可以 ASK 的工具：只读工具和状态工具永远不会进入审批流程。
        # 由 test_every_ask_tool_has_an_approval_card 固定检查。
        self._card_handlers: dict[str, CardHandler] = {
            "repo.edit": self._card_edit,
            "repo.create": self._card_create,
            "repo.delete": self._card_delete,
            "repo.move": self._card_move,
            "repo.apply_patch": self._card_patch,
            "repo.exec": self._card_exec,
            "repo.check": self._card_check,
        }
        self._execute_handlers: dict[str, ExecuteHandler] = {
            "repo.list": self._execute_list,
            "repo.search": self._execute_search,
            "repo.read": self._execute_read,
            "repo.edit": self._execute_write_adapter,
            "repo.create": self._execute_write_adapter,
            "repo.delete": self._execute_delete_adapter,
            "repo.move": self._execute_move_adapter,
            "repo.apply_patch": self._execute_patch_adapter,
            "repo.exec": self._execute_exec_adapter,
            "repo.check": self._execute_check_adapter,
            "repo.diff": self._execute_diff,
            "task.plan": self._execute_plan,
        }

    async def execute(self, ctx: RunContext, step: int, call: ToolCallProposal) -> ToolExecution:
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
        """分发到对应工具的事实处理器。

        registry 已经根据 ARGS_MODELS 验证了 `call.tool_name`，连接测试也将此表固定为
        同一组键，因此按构造不可能查找失败；回退分支只是为了温和失败（返回空事实，
        策略随后会拒绝任何副作用工具）。
        """
        handler = self._facts_handlers.get(call.tool_name)
        if handler is None:  # pragma: no cover - wiring 测试已固定保证此处不可能到达
            return ToolFacts(tool_name=call.tool_name), None
        return await handler(ctx, call, args)

    async def _facts_path_read(
        self, ctx: RunContext, call: ToolCallProposal, args: ToolArgs
    ) -> tuple[ToolFacts, ToolPreview]:
        assert isinstance(args, RepoListArgs | RepoSearchArgs | RepoReadArgs)
        facts = self._workspace.path_facts(args.path)
        return (
            ToolFacts(
                tool_name=call.tool_name,
                within_workspace=facts.within_workspace,
                touches_protected_path=facts.is_protected,
                path=facts.normalized,
            ),
            None,
        )

    async def _facts_edit(
        self, ctx: RunContext, call: ToolCallProposal, args: ToolArgs
    ) -> tuple[ToolFacts, ToolPreview]:
        assert isinstance(args, RepoEditArgs)
        facts = self._workspace.path_facts(args.path)
        if not facts.within_workspace or facts.is_protected:
            return (
                ToolFacts(
                    tool_name=call.tool_name,
                    within_workspace=facts.within_workspace,
                    touches_protected_path=facts.is_protected,
                    path=facts.normalized,
                ),
                None,
            )
        recorded = ctx.files_read.get(facts.normalized)
        if recorded is None:
            raise WorkspaceError(
                "invalid_arguments",
                f"read {facts.normalized!r} with repo.read before editing it",
            )
        if facts.digest != recorded:
            raise WorkspaceError(
                "stale_preimage",
                f"{facts.normalized!r} changed since it was last read; read it again",
            )
        preview = await self._workspace.preview_edit(
            args.path,
            args.old_string,
            args.new_string,
            occurrence=args.occurrence,
            replace_all=args.replace_all,
        )
        return (
            ToolFacts(
                tool_name=call.tool_name,
                within_workspace=True,
                touches_protected_path=False,
                preimage_digest=preview.preimage_digest,
                path=facts.normalized,
            ),
            preview,
        )

    async def _facts_create(
        self, ctx: RunContext, call: ToolCallProposal, args: ToolArgs
    ) -> tuple[ToolFacts, ToolPreview]:
        assert isinstance(args, RepoCreateArgs)
        facts = self._workspace.path_facts(args.path)
        if not facts.within_workspace or facts.is_protected:
            return (
                ToolFacts(
                    tool_name=call.tool_name,
                    within_workspace=facts.within_workspace,
                    touches_protected_path=facts.is_protected,
                    path=facts.normalized,
                ),
                None,
            )
        # 如果路径已经存在就抛出异常，因此 create 绝不会静默覆盖代理尚未
        # 读取的文件。
        preview = await self._workspace.preview_create(args.path, args.content)
        return (
            ToolFacts(
                tool_name=call.tool_name,
                within_workspace=True,
                touches_protected_path=False,
                preimage_digest=None,
                path=facts.normalized,
            ),
            preview,
        )

    async def _facts_delete(
        self, ctx: RunContext, call: ToolCallProposal, args: ToolArgs
    ) -> tuple[ToolFacts, ToolPreview]:
        assert isinstance(args, RepoDeleteArgs)
        facts = self._workspace.path_facts(args.path)
        if not facts.within_workspace or facts.is_protected:
            return (
                ToolFacts(
                    tool_name=call.tool_name,
                    within_workspace=facts.within_workspace,
                    touches_protected_path=facts.is_protected,
                    path=facts.normalized,
                ),
                None,
            )
        # 文件不存在时抛出 not_found；流水线会将其转换为结构化结果。用户会
        # 在预览中看到内容，因此不要求之前先 read——preimage 已经固定。
        preview = await self._workspace.preview_delete(args.path)
        return (
            ToolFacts(
                tool_name=call.tool_name,
                within_workspace=True,
                touches_protected_path=False,
                preimage_digest=preview.preimage_digest,
                path=facts.normalized,
            ),
            preview,
        )

    async def _facts_move(
        self, ctx: RunContext, call: ToolCallProposal, args: ToolArgs
    ) -> tuple[ToolFacts, ToolPreview]:
        assert isinstance(args, RepoMoveArgs)
        src_facts = self._workspace.path_facts(args.src)
        dest_facts = self._workspace.path_facts(args.dest)
        within = src_facts.within_workspace and dest_facts.within_workspace
        protected = src_facts.is_protected or dest_facts.is_protected
        if not within or protected:
            return (
                ToolFacts(
                    tool_name=call.tool_name,
                    within_workspace=within,
                    touches_protected_path=protected,
                    path=src_facts.normalized,
                ),
                None,
            )
        removal, addition = await self._workspace.preview_move(args.src, args.dest)
        combined = EditPreview(
            path=f"{removal.path} -> {addition.path}",
            diff=removal.diff + addition.diff,
            preimage_digest=removal.preimage_digest,
            postimage_digest=addition.postimage_digest,
            insertions=addition.insertions,
            deletions=removal.deletions,
        )
        return (
            ToolFacts(
                tool_name=call.tool_name,
                within_workspace=True,
                touches_protected_path=False,
                preimage_digest=removal.preimage_digest,
                path=src_facts.normalized,
            ),
            combined,
        )

    async def _facts_patch(
        self, ctx: RunContext, call: ToolCallProposal, args: ToolArgs
    ) -> tuple[ToolFacts, ToolPreview]:
        assert isinstance(args, RepoApplyPatchArgs)
        # 先收集硬事实：每个操作的每条路径都必须位于工作区内且不受保护，
        # 否则策略会在进行任何预览工作前硬拒绝。
        op_paths: list[str] = []
        for op in args.operations:
            if op.kind == "move":
                op_paths += [op.src, op.dest]
            else:
                op_paths.append(op.path)
        all_facts = [self._workspace.path_facts(p) for p in op_paths]
        within = all(f.within_workspace for f in all_facts)
        protected = any(f.is_protected for f in all_facts)
        if not within or protected:
            return (
                ToolFacts(
                    tool_name=call.tool_name,
                    within_workspace=within,
                    touches_protected_path=protected,
                ),
                None,
            )
        plan = await self._workspace.preview_patch(
            tuple(_to_patch_spec(op) for op in args.operations), ctx.files_read
        )
        # 审批绑定所有被触及文件的固定内容的聚合值：对规范化的
        # {path: preimage} 映射计算一个摘要，因此任意文件发生漂移都会使
        # 整个审批失效。
        aggregate = sha256_text(canonical_json(dict(sorted(plan.preimages.items()))))
        return (
            ToolFacts(
                tool_name=call.tool_name,
                within_workspace=True,
                touches_protected_path=False,
                preimage_digest=aggregate,
            ),
            plan,
        )

    async def _facts_exec(
        self, ctx: RunContext, call: ToolCallProposal, args: ToolArgs
    ) -> tuple[ToolFacts, ToolPreview]:
        assert isinstance(args, RepoExecArgs)
        facts = self._workspace.path_facts(args.cwd)
        return (
            ToolFacts(
                tool_name=call.tool_name,
                within_workspace=facts.within_workspace,
                touches_protected_path=facts.is_protected,
                exec_class=classify_argv(args.argv).value,
                sandbox_available=self._launcher is not None and self._launcher.available(),
                path=facts.normalized,
            ),
            None,
        )

    async def _facts_check(
        self, ctx: RunContext, call: ToolCallProposal, args: ToolArgs
    ) -> tuple[ToolFacts, ToolPreview]:
        assert isinstance(args, RepoCheckArgs)
        return (
            ToolFacts(
                tool_name=call.tool_name,
                recipe_registered=args.recipe_id in self._recipes,
            ),
            None,
        )

    async def _facts_stateless(
        self, ctx: RunContext, call: ToolCallProposal, args: ToolArgs
    ) -> tuple[ToolFacts, ToolPreview]:
        # repo.diff（只读、不接受路径参数）和 task.plan（只接触运行状态）：
        # 磁盘上没有需要固定的内容。
        return ToolFacts(tool_name=call.tool_name), None

    # -- 审批 --------------------------------------------------------------------

    def _approval_card(
        self, call: ToolCallProposal, args: ToolArgs, preview: ToolPreview
    ) -> tuple[str, str]:
        """人工针对一个提案看到的内容：（摘要、预览文本）。

        与 facts 和 execute 表一样按工具区分，使审批流程本身只关注摘要、授权和消费。
        没有卡片条目的工具不会被策略发送到这里（只读工具和状态工具属于这种情况）；
        `_card_handlers` 由 `tests/unit/test_policy.py` 固定为可 ASK 工具集合，因此
        需要 ASK 的工具不可能带着空摘要悄悄到达人工面前。

        每个处理器开头都有一个返回空字符串的 isinstance 守卫。这是为了让 mypy 缩小
        `ToolArgs` 联合类型，而不是真实分支：流水线到达这里之前，registry 已经根据
        这个确切的工具名称验证过参数，因此不可能不匹配。发生渲染疏漏时返回空卡片而
        不是抛出异常，因为渲染问题绝不能在审批中途拖垮运行。
        """
        handler = self._card_handlers.get(call.tool_name)
        return handler(args, preview) if handler is not None else ("", "")

    @staticmethod
    def _intent(summary: str) -> str:
        """模型自己提供的一行理由（如果有则追加到卡片中）。

        这是审批卡片上的不可信文本，因此会附加在程序构建的摘要之后，而不是替换摘要：
        人工始终先看到 Haven 判定的操作内容，后面才是模型的说法。
        """
        return f": {summary}" if summary else ""

    def _card_patch(self, args: ToolArgs, preview: ToolPreview) -> tuple[str, str]:
        if not (isinstance(args, RepoApplyPatchArgs) and isinstance(preview, PatchPreview)):
            return "", ""
        summary = (
            f"apply patch: {len(args.operations)} operation(s) across "
            f"{len(preview.effects)} file(s) "
            f"(+{preview.insertions} -{preview.deletions}){self._intent(args.summary)}"
        )
        return summary, _clip(preview.diff, PREVIEW_CHARS)

    def _card_edit(self, args: ToolArgs, preview: ToolPreview) -> tuple[str, str]:
        if not (isinstance(args, RepoEditArgs) and isinstance(preview, EditPreview)):
            return "", ""
        scope = ""
        if args.replace_all:
            scope = " [all occurrences]"
        elif args.occurrence is not None:
            scope = f" [occurrence {args.occurrence}]"
        summary = (
            f"edit {preview.path} (+{preview.insertions} -{preview.deletions})"
            f"{scope}{self._intent(args.summary)}"
        )
        return summary, _clip(preview.diff, PREVIEW_CHARS)

    def _card_create(self, args: ToolArgs, preview: ToolPreview) -> tuple[str, str]:
        if not (isinstance(args, RepoCreateArgs) and isinstance(preview, EditPreview)):
            return "", ""
        summary = (
            f"create {preview.path} ({preview.insertions} new line(s)){self._intent(args.summary)}"
        )
        return summary, _clip(preview.diff, PREVIEW_CHARS)

    def _card_delete(self, args: ToolArgs, preview: ToolPreview) -> tuple[str, str]:
        if not (isinstance(args, RepoDeleteArgs) and isinstance(preview, EditPreview)):
            return "", ""
        summary = f"delete {preview.path} ({preview.deletions} line(s)){self._intent(args.summary)}"
        return summary, _clip(preview.diff, PREVIEW_CHARS)

    def _card_move(self, args: ToolArgs, preview: ToolPreview) -> tuple[str, str]:
        if not (isinstance(args, RepoMoveArgs) and isinstance(preview, EditPreview)):
            return "", ""
        return f"move {preview.path}{self._intent(args.summary)}", _clip(
            preview.diff, PREVIEW_CHARS
        )

    def _card_exec(self, args: ToolArgs, preview: ToolPreview) -> tuple[str, str]:
        if not isinstance(args, RepoExecArgs):
            return "", ""
        lines = [f"$ {shlex.join(args.argv)}", self._describe_sandbox()]
        if classify_argv(args.argv) is ExecClass.SHELL_PASSTHROUGH:
            lines.append(
                "WARNING: this interprets an arbitrary script, so the command "
                "above does not describe everything it may do."
            )
        summary = f"run {shlex.join(args.argv)} in {args.cwd}{self._intent(args.summary)}"
        return summary, "\n".join(lines)

    def _card_check(self, args: ToolArgs, preview: ToolPreview) -> tuple[str, str]:
        if not isinstance(args, RepoCheckArgs):
            return "", ""
        recipe = self._recipes[args.recipe_id]
        summary = (
            f"run check recipe {args.recipe_id!r} "
            "(approving also covers identical re-runs for the rest of this run)"
        )
        return summary, "$ " + " ".join(recipe.argv)

    async def _ask_approval(
        self,
        ctx: RunContext,
        call: ToolCallProposal,
        args: ToolArgs,
        canonical_args: str,
        preview: ToolPreview,
        facts: ToolFacts,
    ) -> tuple[bool, str | None, str]:
        summary, preview_text = self._approval_card(call, args, preview)

        digest = compute_approval_digest(
            workspace_digest=self._workspace.workspace_digest,
            tool_name=call.tool_name,
            tool_version=self._registry.version,
            canonical_args_json=canonical_args,
            preimage_digest=facts.preimage_digest,
            preview_digest=sha256_text(preview_text) if preview_text else None,
        )

        if isinstance(args, RepoCheckArgs) and digest in ctx.standing_check_grants:
            # 持续授权（ADR 0025）：用户已经在本次运行中批准过字节级相同的检查
            # （相同的 recipe id 和 argv、相同工作区、相同工具版本——摘要绑定了
            # 全部内容）。铸造并消耗一个新的单次审批，使日志仍为每次执行保留
            # 一条审批记录，宣布这次授权，然后跳过模态窗口。只有 repo.check
            # 有资格使用：它运行用户注册的配方，而重复执行正是验证循环的正常
            # 形态；写操作和 exec 始终重新询问。
            approval_id = new_approval_id()
            await self._store.record_approval(approval_id, ctx.run_id, digest)
            await self._store.decide_approval(approval_id, ApprovalDecision.APPROVED)
            await self._emitter.emit(
                ctx.run_id,
                Notice(
                    run_id=ctx.run_id,
                    level="info",
                    message=(
                        f"standing approval: check recipe {args.recipe_id!r} was "
                        "approved earlier in this run; re-running without asking"
                    ),
                ),
            )
            await self._emitter.emit(
                ctx.run_id,
                ApprovalDecided(
                    run_id=ctx.run_id,
                    approval_id=approval_id,
                    decision=ApprovalDecision.APPROVED.value,
                ),
            )
            if not await self._store.consume_approval(approval_id, digest):
                ctx.move_to(RunStatus.RUNNING_MODEL)
                return False, None, "approval could not be consumed (stale or reused)"
            ctx.move_to(RunStatus.EXECUTING_TOOL)
            return True, str(approval_id), ""

        approval_id = new_approval_id()
        await self._store.record_approval(approval_id, ctx.run_id, digest)
        request = ApprovalRequest(
            approval_id=approval_id,
            run_id=ctx.run_id,
            call_id=ToolCallId(call.call_id),
            tool_name=call.tool_name,
            summary=summary,
            risk=evaluate_policy(self._mode, facts).risk,
            request_digest=digest,
            preview=preview_text,
        )
        ctx.move_to(RunStatus.WAITING_APPROVAL)
        await self._emitter.emit(
            ctx.run_id,
            ApprovalRequested(
                run_id=ctx.run_id,
                call_id=call.call_id,
                approval_id=approval_id,
                tool_name=call.tool_name,
                summary=summary,
                preview=preview_text,
                risk=request.risk.value,
                request_digest=digest,
            ),
        )

        decision = await self._approvals.respond(request)

        await self._store.decide_approval(approval_id, decision)
        await self._emitter.emit(
            ctx.run_id,
            ApprovalDecided(run_id=ctx.run_id, approval_id=approval_id, decision=decision.value),
        )
        if decision is not ApprovalDecision.APPROVED:
            ctx.move_to(RunStatus.RUNNING_MODEL)
            return False, None, "the user rejected this action"

        # 单次消耗且绑定摘要：第二次消耗或摘要发生漂移时失败关闭。
        if not await self._store.consume_approval(approval_id, digest):
            ctx.move_to(RunStatus.RUNNING_MODEL)
            return False, None, "approval could not be consumed (stale or reused)"
        if isinstance(args, RepoCheckArgs):
            # 用户第一次批准此完全相同的检查时，会启用卡片上宣布的运行范围
            # 持续授权（ADR 0025）。拒绝永远不会启用任何授权——只有批准后
            # 才会执行到这一行。
            ctx.standing_check_grants.add(digest)
        ctx.move_to(RunStatus.EXECUTING_TOOL)
        return True, str(approval_id), ""

    # -- 执行 --------------------------------------------------------------------

    async def _snapshot(self) -> WorkspaceSnapshot:
        """在事件循环之外为整个目录树计算摘要。

        进程写入归因（ADR 0012）会在每次 exec/check 前后做快照，因此每次进程调用运行
        两次，成本为 O(repo)：在 12.8 万行 checkout 上测得每次约 150ms。如果内联运行，
        每次 check 会冻结循环约 300ms——无法流式输出、渲染或打开审批模态框——所以放到
        worker 线程中。工作内容本身不变：仍然完整计算每个文件的摘要，因为摘要使进程
        写入可检测（对于受保护路径，在 Linux 上也正是它使篡改完全可检测，ADR 0018）。
        用大小上限或 mtime 统计来降低成本，会把一个已测得并非问题的成本换成可规避的门禁。
        """
        return await asyncio.to_thread(self._workspace.capture_snapshot)

    async def _run_ticketed(
        self,
        ctx: RunContext,
        call: ToolCallProposal,
        args: ToolArgs,
        ticket_digest: str,
        preview: ToolPreview,
    ) -> ToolExecution:
        """分发到对应工具的执行处理器（与 facts 使用相同的键集合）。"""
        handler = self._execute_handlers.get(call.tool_name)
        if handler is None:  # pragma: no cover - wiring 测试已固定保证此处不可能到达
            return ToolExecution(
                _error(call, ToolErrorCode.UNKNOWN_TOOL, f"no executor for {call.tool_name!r}")
            )
        return await handler(ctx, call, args, ticket_digest, preview)

    async def _execute_list(
        self,
        ctx: RunContext,
        call: ToolCallProposal,
        args: ToolArgs,
        ticket_digest: str,
        preview: ToolPreview,
    ) -> ToolExecution:
        assert isinstance(args, RepoListArgs)
        listing = await self._workspace.list_dir(args.path, args.max_entries)
        return ToolExecution(
            _ok(
                call,
                {
                    "path": listing.path,
                    "entries": [
                        {"name": e.name, "dir": e.is_dir, "size": e.size_bytes}
                        for e in listing.entries
                    ],
                },
                truncated=listing.truncated,
            )
        )

    async def _execute_search(
        self,
        ctx: RunContext,
        call: ToolCallProposal,
        args: ToolArgs,
        ticket_digest: str,
        preview: ToolPreview,
    ) -> ToolExecution:
        assert isinstance(args, RepoSearchArgs)
        found = await self._workspace.search(args.pattern, args.path, args.max_results)
        return ToolExecution(
            _ok(
                call,
                {
                    "matches": [
                        {"path": m.path, "line": m.line_number, "text": m.line}
                        for m in found.matches
                    ],
                    "files_scanned": found.files_scanned,
                },
                truncated=found.truncated,
            )
        )

    async def _execute_read(
        self,
        ctx: RunContext,
        call: ToolCallProposal,
        args: ToolArgs,
        ticket_digest: str,
        preview: ToolPreview,
    ) -> ToolExecution:
        assert isinstance(args, RepoReadArgs)
        read = await self._workspace.read_file(args.path, args.start_line, args.max_lines)
        ctx.files_read[read.path] = read.digest
        return ToolExecution(
            _ok(
                call,
                {
                    "path": read.path,
                    "start_line": read.start_line,
                    "end_line": read.end_line,
                    "total_lines": read.total_lines,
                    "content": read.content,
                },
                truncated=read.truncated,
            )
        )

    async def _execute_write_adapter(
        self,
        ctx: RunContext,
        call: ToolCallProposal,
        args: ToolArgs,
        ticket_digest: str,
        preview: ToolPreview,
    ) -> ToolExecution:
        assert isinstance(args, RepoEditArgs | RepoCreateArgs)
        assert not isinstance(preview, PatchPreview)
        return await self._execute_write(ctx, call, args, ticket_digest, preview)

    async def _execute_delete_adapter(
        self,
        ctx: RunContext,
        call: ToolCallProposal,
        args: ToolArgs,
        ticket_digest: str,
        preview: ToolPreview,
    ) -> ToolExecution:
        assert isinstance(args, RepoDeleteArgs)
        assert not isinstance(preview, PatchPreview)
        return await self._execute_delete(ctx, call, args, ticket_digest, preview)

    async def _execute_move_adapter(
        self,
        ctx: RunContext,
        call: ToolCallProposal,
        args: ToolArgs,
        ticket_digest: str,
        preview: ToolPreview,
    ) -> ToolExecution:
        assert isinstance(args, RepoMoveArgs)
        assert not isinstance(preview, PatchPreview)
        return await self._execute_move(ctx, call, args, ticket_digest, preview)

    async def _execute_patch_adapter(
        self,
        ctx: RunContext,
        call: ToolCallProposal,
        args: ToolArgs,
        ticket_digest: str,
        preview: ToolPreview,
    ) -> ToolExecution:
        assert isinstance(args, RepoApplyPatchArgs)
        assert isinstance(preview, PatchPreview)  # 事实收集已构建该预览
        return await self._execute_patch(ctx, call, ticket_digest, preview)

    async def _execute_exec_adapter(
        self,
        ctx: RunContext,
        call: ToolCallProposal,
        args: ToolArgs,
        ticket_digest: str,
        preview: ToolPreview,
    ) -> ToolExecution:
        assert isinstance(args, RepoExecArgs)
        return await self._execute_exec(ctx, call, args, ticket_digest)

    async def _execute_check_adapter(
        self,
        ctx: RunContext,
        call: ToolCallProposal,
        args: ToolArgs,
        ticket_digest: str,
        preview: ToolPreview,
    ) -> ToolExecution:
        assert isinstance(args, RepoCheckArgs)
        return await self._execute_check(ctx, call, args, ticket_digest)

    async def _execute_plan(
        self,
        ctx: RunContext,
        call: ToolCallProposal,
        args: ToolArgs,
        ticket_digest: str,
        preview: ToolPreview,
    ) -> ToolExecution:
        assert isinstance(args, TaskPlanArgs)
        ctx.plan = tuple(args.steps)
        await self._emitter.emit(
            ctx.run_id,
            PlanUpdated(
                run_id=ctx.run_id,
                steps=tuple(
                    PlanStepView(title=_clip(step.title, 120), status=step.status)
                    for step in ctx.plan
                ),
            ),
        )
        done = sum(1 for step in ctx.plan if step.status == "done")
        return ToolExecution(_ok(call, {"steps": len(ctx.plan), "done": done, "recorded": True}))

    async def _execute_diff(
        self,
        ctx: RunContext,
        call: ToolCallProposal,
        args: ToolArgs,
        ticket_digest: str,
        preview: ToolPreview,
    ) -> ToolExecution:
        run_diff = await self._workspace.run_diff()
        envelope = await self._emitter.emit(
            ctx.run_id,
            DiffPreview(
                run_id=ctx.run_id,
                files_changed=len(run_diff.files),
                insertions=run_diff.insertions,
                deletions=run_diff.deletions,
                preview=_clip(run_diff.diff, PREVIEW_CHARS),
            ),
        )
        ctx.ledger = ctx.ledger.with_diff(
            DiffEvidence(
                seq=envelope.seq,
                files_changed=len(run_diff.files),
                insertions=run_diff.insertions,
                deletions=run_diff.deletions,
                diff_digest=sha256_text(run_diff.diff),
            )
        )
        await self._emitter.emit(
            ctx.run_id,
            EvidenceRecorded(
                run_id=ctx.run_id,
                evidence_kind="diff",
                summary=(
                    f"{len(run_diff.files)} file(s), +{run_diff.insertions} -{run_diff.deletions}"
                ),
            ),
        )
        return ToolExecution(
            _ok(
                call,
                {
                    "files": list(run_diff.files),
                    "insertions": run_diff.insertions,
                    "deletions": run_diff.deletions,
                    "diff": _clip(run_diff.diff, MODEL_PAYLOAD_CHARS),
                },
                truncated=run_diff.truncated,
            )
        )

    async def _execute_write(
        self,
        ctx: RunContext,
        call: ToolCallProposal,
        args: RepoEditArgs | RepoCreateArgs,
        ticket_digest: str,
        preview: EditPreview | None,
    ) -> ToolExecution:
        assert preview is not None  # 事实收集对写操作始终会构建预览
        # 预览的 postimage 会在任何字节落盘前记录：如果进程在写入过程中退出，
        # 恢复逻辑可以将“文件已匹配预期 postimage”分类为 confirmed，而不是
        # unknown。
        await self._store.record_execution(
            ExecutionRecord(
                call_id=call.call_id,
                run_id=ctx.run_id,
                ticket_digest=ticket_digest,
                tool_name=call.tool_name,
                effect_state=EffectState.STARTED,
                preimage_digest=preview.preimage_digest,
                postimage_digest=preview.postimage_digest,
                path=preview.path,
            )
        )
        try:
            if isinstance(args, RepoCreateArgs):
                outcome = await self._workspace.apply_create(args.path, args.content)
            else:
                outcome = await self._workspace.apply_edit(
                    args.path,
                    args.old_string,
                    args.new_string,
                    preview.preimage_digest,
                    occurrence=args.occurrence,
                    replace_all=args.replace_all,
                )
        except WorkspaceError as exc:
            await self._store.update_execution_state(call.call_id, EffectState.FAILED)
            return ToolExecution(_error(call, _map_ws_code(exc), str(exc)))
        except BaseException:
            # 写入过程中崩溃或取消：副作用状态未知，绝不能静默重放。
            await self._store.update_execution_state(call.call_id, EffectState.EFFECT_UNKNOWN)
            raise

        await self._store.update_execution_state(
            call.call_id, EffectState.CONFIRMED, outcome.postimage_digest
        )
        # 代理现在已经知道该文件的确切内容，因此之后编辑它时无需再次读取，
        # 也能合法地绑定 preimage。
        ctx.files_read[outcome.path] = outcome.postimage_digest
        verb = "created" if isinstance(args, RepoCreateArgs) else "edited"
        envelope = await self._emitter.emit(
            ctx.run_id,
            EvidenceRecorded(
                run_id=ctx.run_id,
                evidence_kind="edit",
                summary=f"{verb} {outcome.path}: postimage {outcome.postimage_digest[:12]}",
            ),
        )
        ctx.ledger = ctx.ledger.with_edit(
            EditEvidence(
                seq=envelope.seq,
                path=outcome.path,
                preimage_digest=outcome.preimage_digest,
                postimage_digest=outcome.postimage_digest,
            )
        )
        return ToolExecution(
            _ok(
                call,
                {
                    "path": outcome.path,
                    "applied": True,
                    "postimage_digest": outcome.postimage_digest,
                    "diff": _clip(preview.diff, MODEL_PAYLOAD_CHARS),
                },
            )
        )

    async def _execute_delete(
        self,
        ctx: RunContext,
        call: ToolCallProposal,
        args: RepoDeleteArgs,
        ticket_digest: str,
        preview: EditPreview | None,
    ) -> ToolExecution:
        # 使用审批绑定的 preimage，而不是重新读取：apply_delete 会将磁盘文件
        # 与它比较，因此审批后发生变化时会失败关闭。
        assert preview is not None  # 事实收集对 delete 始终会构建预览
        preimage = preview.preimage_digest
        await self._store.record_execution(
            ExecutionRecord(
                call_id=call.call_id,
                run_id=ctx.run_id,
                ticket_digest=ticket_digest,
                tool_name=call.tool_name,
                effect_state=EffectState.STARTED,
                preimage_digest=preimage,
                postimage_digest="",
                path=args.path,
            )
        )
        try:
            outcome = await self._workspace.apply_delete(args.path, preimage)
        except WorkspaceError as exc:
            await self._store.update_execution_state(call.call_id, EffectState.FAILED)
            return ToolExecution(_error(call, _map_ws_code(exc), str(exc)))
        except BaseException:
            await self._store.update_execution_state(call.call_id, EffectState.EFFECT_UNKNOWN)
            raise

        await self._store.update_execution_state(call.call_id, EffectState.CONFIRMED)
        ctx.files_read.pop(outcome.path, None)
        envelope = await self._emitter.emit(
            ctx.run_id,
            EvidenceRecorded(
                run_id=ctx.run_id, evidence_kind="edit", summary=f"deleted {outcome.path}"
            ),
        )
        ctx.ledger = ctx.ledger.with_edit(
            EditEvidence(
                seq=envelope.seq,
                path=outcome.path,
                preimage_digest=outcome.preimage_digest,
                postimage_digest="",
            )
        )
        return ToolExecution(_ok(call, {"path": outcome.path, "deleted": True}))

    async def _execute_move(
        self,
        ctx: RunContext,
        call: ToolCallProposal,
        args: RepoMoveArgs,
        ticket_digest: str,
        preview: EditPreview | None,
    ) -> ToolExecution:
        # 使用源文件经审批绑定的 preimage，因此如果源文件在审批和执行之间
        # 发生变化，apply_move 会失败关闭。
        assert preview is not None  # 事实收集对 move 始终会构建预览
        preimage = preview.preimage_digest
        # dest_path 使恢复逻辑能够检查中断移动的两端：move 不会改变内容，
        # 因此源/目标是否存在加上 preimage 摘要，可以分类除“复制完成后崩溃”
        # 之外的每个崩溃点。
        await self._store.record_execution(
            ExecutionRecord(
                call_id=call.call_id,
                run_id=ctx.run_id,
                ticket_digest=ticket_digest,
                tool_name=call.tool_name,
                effect_state=EffectState.STARTED,
                preimage_digest=preimage,
                postimage_digest="",
                path=args.src,
                dest_path=args.dest,
            )
        )
        try:
            removal, addition = await self._workspace.apply_move(args.src, args.dest, preimage)
        except WorkspaceError as exc:
            await self._store.update_execution_state(call.call_id, EffectState.FAILED)
            return ToolExecution(_error(call, _map_ws_code(exc), str(exc)))
        except BaseException:
            await self._store.update_execution_state(call.call_id, EffectState.EFFECT_UNKNOWN)
            raise

        await self._store.update_execution_state(
            call.call_id, EffectState.CONFIRMED, addition.postimage_digest
        )
        ctx.files_read.pop(removal.path, None)
        # 代理现在知道目标内容（它们与源文件内容相同）。
        ctx.files_read[addition.path] = addition.postimage_digest
        envelope = await self._emitter.emit(
            ctx.run_id,
            EvidenceRecorded(
                run_id=ctx.run_id,
                evidence_kind="edit",
                summary=f"moved {removal.path} -> {addition.path}",
            ),
        )
        # 两半都是本次运行的改动：删除和新增。
        ctx.ledger = ctx.ledger.with_edit(
            EditEvidence(
                seq=envelope.seq,
                path=removal.path,
                preimage_digest=removal.preimage_digest,
                postimage_digest="",
            )
        ).with_edit(
            EditEvidence(
                seq=envelope.seq,
                path=addition.path,
                preimage_digest="",
                postimage_digest=addition.postimage_digest,
            )
        )
        return ToolExecution(_ok(call, {"src": removal.path, "dest": addition.path, "moved": True}))

    async def _execute_patch(
        self,
        ctx: RunContext,
        call: ToolCallProposal,
        ticket_digest: str,
        plan: PatchPreview,
    ) -> ToolExecution:
        # 补丁会按组成它的文件副作用写入日志——每个副作用都像单操作工具一样
        # 带有预期 postimage——因此中断的补丁可以由现有恢复规则逐文件分类。
        for index, effect in enumerate(plan.effects):
            await self._store.record_execution(
                ExecutionRecord(
                    call_id=f"{call.call_id}#{index}",
                    run_id=ctx.run_id,
                    ticket_digest=ticket_digest,
                    tool_name=effect.tool_shape,
                    effect_state=EffectState.STARTED,
                    preimage_digest=effect.preimage_digest,
                    postimage_digest=effect.expected_postimage,
                    path=effect.path,
                )
            )

        async def mark_all(state: EffectState) -> None:
            for index in range(len(plan.effects)):
                await self._store.update_execution_state(f"{call.call_id}#{index}", state)

        try:
            outcomes = await self._workspace.apply_patch(plan)
        except WorkspaceError as exc:
            # 包括“失败且已干净回滚”：目录树未改变，因此副作用是普通失败，
            # 而不是 unknown。
            await mark_all(EffectState.FAILED)
            return ToolExecution(_error(call, _map_ws_code(exc), str(exc)))
        except PatchRollbackError as exc:
            # 确定性代码无法撤销的部分状态：将其暴露为 unknown 副作用，使运行
            # 停止，并在用户调和每个已记录的子副作用前阻止恢复。
            await mark_all(EffectState.EFFECT_UNKNOWN)
            return ToolExecution(
                _error(call, ToolErrorCode.INTERNAL, str(exc)), effect_unknown=True
            )
        except BaseException:
            await mark_all(EffectState.EFFECT_UNKNOWN)
            raise

        by_path = {outcome.path: outcome for outcome in outcomes}
        for index, effect in enumerate(plan.effects):
            await self._store.update_execution_state(
                f"{call.call_id}#{index}",
                EffectState.CONFIRMED,
                by_path[effect.path].postimage_digest,
            )

        envelope = await self._emitter.emit(
            ctx.run_id,
            EvidenceRecorded(
                run_id=ctx.run_id,
                evidence_kind="edit",
                summary=(f"patch: {len(outcomes)} file(s), +{plan.insertions} -{plan.deletions}"),
            ),
        )
        for outcome in outcomes:
            ctx.ledger = ctx.ledger.with_edit(
                EditEvidence(
                    seq=envelope.seq,
                    path=outcome.path,
                    preimage_digest=outcome.preimage_digest,
                    postimage_digest=outcome.postimage_digest,
                )
            )
            if outcome.postimage_digest:
                # 代理现在已经知道该文件的确切内容。
                ctx.files_read[outcome.path] = outcome.postimage_digest
            else:
                ctx.files_read.pop(outcome.path, None)

        return ToolExecution(
            _ok(
                call,
                {
                    "applied": True,
                    "files_changed": len(outcomes),
                    "insertions": plan.insertions,
                    "deletions": plan.deletions,
                    "files": [outcome.path for outcome in outcomes],
                    "diff": _clip(plan.diff, MODEL_PAYLOAD_CHARS),
                },
            )
        )

    def _sandbox_spec(self) -> SandboxSpec:
        # 模型提出的 exec 对工作区是只读的：只有临时目录可写。真正的源码变更
        # 必须通过经过审计的 edit/create/delete/move 工具完成；这也堵住了 Linux
        # 上 Landlock 无法从可写工作区中挖出 `.git` 的漏洞——exec 完全不能写入
        # 工作区（ADR 0017）。
        return SandboxSpec(
            workspace_root=self._workspace.root,
            scratch_dir=self._scratch_dir,
            writable=False,
            allow_network=False,
            private_roots=default_private_roots(),
            extra_readable_roots=default_readable_roots(),
        )

    def _describe_sandbox(self) -> str:
        if self._launcher is None:
            return "sandbox: unavailable"
        return self._launcher.describe(self._sandbox_spec())

    async def _execute_exec(
        self, ctx: RunContext, call: ToolCallProposal, args: RepoExecArgs, ticket_digest: str
    ) -> ToolExecution:
        if self._launcher is None:
            # 不可达：没有后端时策略会拒绝 exec（`sandbox_available` 事实）。
            # 这里使用 raise 而不是 assert，因为 `python -O` 会移除断言，而
            # 没有 launcher 时执行器会直接运行未包装的命令；被移除的保护不会
            # 在这里失败，反而会运行一个不受限制的进程，之后才崩溃。“没有沙箱
            # 就没有 exec，任何配置都不能覆盖这一点”（ADR 0009）必须在所有
            # 解释器标志下成立。
            raise RuntimeError("refusing to exec without a sandbox backend")
        await self._store.record_execution(
            ExecutionRecord(
                call_id=call.call_id,
                run_id=ctx.run_id,
                ticket_digest=ticket_digest,
                tool_name=call.tool_name,
                effect_state=EffectState.STARTED,
                preimage_digest="",
                postimage_digest="",
                path=args.cwd,
            )
        )
        spec = ExecSpec(
            argv=args.argv,
            cwd=self._workspace.root / args.cwd,
            timeout_seconds=args.timeout_seconds,
            sandbox=self._sandbox_spec(),
        )
        before = await self._snapshot()
        try:
            outcome = await self._executor.run_exec(spec)
        except BaseException:
            await self._store.update_execution_state(call.call_id, EffectState.EFFECT_UNKNOWN)
            raise

        # 非零退出也是一次完成的执行，而不是 unknown 副作用。
        await self._store.update_execution_state(call.call_id, EffectState.CONFIRMED)
        # 命令修改的所有文件都会归因给它并作为 edit 证据，因此通过 exec 的
        # 写入无法逃过 Evidence Gate（ADR 0012）。
        tampered = await self._record_process_writes(
            ctx, call.tool_name, before, await self._snapshot()
        )
        if tampered:
            return ToolExecution(
                _error(
                    call,
                    ToolErrorCode.PROTECTED_PATH_TAMPERED,
                    "the command modified protected path(s) "
                    f"{', '.join(tampered)}; this is a boundary violation",
                )
            )
        if outcome.timed_out:
            return ToolExecution(
                _error(
                    call, ToolErrorCode.TIMEOUT, f"command timed out after {args.timeout_seconds}s"
                )
            )
        # 特意不在这里记录证据：只有已注册的 check recipe 才能满足 Evidence Gate。
        return ToolExecution(
            _ok(
                call,
                {
                    "exit_code": outcome.exit_code,
                    "duration_ms": outcome.duration_ms,
                    "stdout_tail": _clip(outcome.stdout_tail, 4000),
                    "stderr_tail": _clip(outcome.stderr_tail, 2000),
                    "sandbox": self._launcher.backend,
                },
                truncated=outcome.truncated,
            )
        )

    async def _record_process_writes(
        self,
        ctx: RunContext,
        tool_name: str,
        before: WorkspaceSnapshot,
        after: WorkspaceSnapshot,
    ) -> list[str]:
        """将进程造成的任何工作区变更归因到证据账本。

        过去只有 edit/create 会写入证据，因此进程造成的文件变更对 Evidence Gate
        不可见（ADR 0012）。这里比较前后快照，并将每个变更都转为编辑证据，因此
        无论运行通过哪个工具修改目录树，都必须满足相同的证据标准。

        返回进程修改过的受保护路径，以便调用方直接让工具调用失败（ADR 0018）——
        控制平面的篡改必须成为硬结果，而不能只是注释。
        """
        # 进程运行期间受保护路径发生变化，属于操作系统沙箱无法阻止的篡改
        # （Landlock 无法在可写工作区中保护 `.git`）。将其作为错误暴露出来，
        # 使审计轨迹能够归因，而不是静默发生——这正是该漏洞中“不可见”的一半。
        tampered = sorted(
            name
            for name in before.protected_digests.keys() | after.protected_digests.keys()
            if before.protected_digests.get(name) != after.protected_digests.get(name)
        )
        for name in tampered:
            await self._emitter.emit(
                ctx.run_id,
                Notice(
                    run_id=ctx.run_id,
                    level="error",
                    message=f"{tool_name} modified a protected path ({name}); this must not happen",
                ),
            )

        changes = _detect_changes(before, after)
        if not changes:
            return tampered
        summary = f"{len(changes)} file(s) changed by {tool_name}: " + ", ".join(
            change.path for change in changes[:5]
        )
        envelope = await self._emitter.emit(
            ctx.run_id,
            EvidenceRecorded(run_id=ctx.run_id, evidence_kind="edit", summary=_clip(summary, 200)),
        )
        for change in changes:
            self._workspace.register_run_original(change.path, change.before_content)
            ctx.ledger = ctx.ledger.with_edit(
                EditEvidence(
                    seq=envelope.seq,
                    path=change.path,
                    preimage_digest=change.preimage_digest,
                    postimage_digest=change.postimage_digest,
                )
            )
        return tampered

    async def _execute_check(
        self, ctx: RunContext, call: ToolCallProposal, args: RepoCheckArgs, ticket_digest: str
    ) -> ToolExecution:
        recipe = self._recipes[args.recipe_id]
        await self._store.record_execution(
            ExecutionRecord(
                call_id=call.call_id,
                run_id=ctx.run_id,
                ticket_digest=ticket_digest,
                tool_name=call.tool_name,
                effect_state=EffectState.STARTED,
                preimage_digest="",
                postimage_digest="",
                path="",
            )
        )
        before = await self._snapshot()
        try:
            outcome = await self._executor.run_recipe(recipe, self._workspace.root)
        except BaseException:
            await self._store.update_execution_state(call.call_id, EffectState.EFFECT_UNKNOWN)
            raise

        await self._store.update_execution_state(call.call_id, EffectState.CONFIRMED)
        # 会修改目录树的检查（例如格式化器）会像其他写操作一样记录在检查证据
        # 之前，使门禁能够看到这次修改。
        tampered = await self._record_process_writes(
            ctx, call.tool_name, before, await self._snapshot()
        )
        if tampered:
            # 重写控制平面的检查不是验证：不会记录 check 证据，因此本次运行
            # 不能用它满足 Evidence Gate，调用本身也会失败（ADR 0018）。
            return ToolExecution(
                _error(
                    call,
                    ToolErrorCode.PROTECTED_PATH_TAMPERED,
                    f"recipe {recipe.id!r} modified protected path(s) "
                    f"{', '.join(tampered)}; the check does not count as verification",
                )
            )
        envelope = await self._emitter.emit(
            ctx.run_id,
            EvidenceRecorded(
                run_id=ctx.run_id,
                evidence_kind="check",
                summary=(
                    f"{recipe.id}: exit={outcome.exit_code} in {outcome.duration_ms}ms"
                    + (" (timed out)" if outcome.timed_out else "")
                ),
            ),
        )
        ctx.ledger = ctx.ledger.with_check(
            CheckEvidence(
                seq=envelope.seq,
                recipe_id=recipe.id,
                exit_code=outcome.exit_code,
                duration_ms=outcome.duration_ms,
                truncated=outcome.truncated,
            )
        )
        if outcome.timed_out:
            return ToolExecution(
                _error(
                    call,
                    ToolErrorCode.TIMEOUT,
                    f"recipe {recipe.id!r} timed out after {recipe.timeout_seconds}s",
                )
            )
        return ToolExecution(
            _ok(
                call,
                {
                    "recipe_id": recipe.id,
                    "exit_code": outcome.exit_code,
                    "duration_ms": outcome.duration_ms,
                    "stdout_tail": _clip(outcome.stdout_tail, 4000),
                    "stderr_tail": _clip(outcome.stderr_tail, 2000),
                },
                truncated=outcome.truncated,
            )
        )

    # -- 共享 -------------------------------------------------------------------

    async def _finish(
        self,
        ctx: RunContext,
        call: ToolCallProposal,
        result: ToolResult,
        started: float,
        effect_unknown: bool = False,
    ) -> ToolExecution:
        duration_ms = int((time.monotonic() - started) * 1000)
        result = result.model_copy(update={"duration_ms": duration_ms})
        await self._emitter.emit(
            ctx.run_id,
            ToolCompleted(
                run_id=ctx.run_id,
                call_id=call.call_id,
                tool_name=call.tool_name,
                status=result.status.value,
                error_code=result.error_code.value if result.error_code else "",
                summary=_clip(result.message or _summarize_payload(result), 200),
                truncated=result.truncated,
                duration_ms=duration_ms,
            ),
        )
        return ToolExecution(result=result, effect_unknown=effect_unknown)


@dataclass(frozen=True, slots=True)
class _ExternalChange:
    path: str
    preimage_digest: str
    postimage_digest: str
    before_content: str


def _detect_changes(before: WorkspaceSnapshot, after: WorkspaceSnapshot) -> list[_ExternalChange]:
    """找出两个快照之间摘要新增、消失或发生移动的文件。

    该函数只依赖两个摘要映射，因此变更集合完全由快照决定。删除会产生空 postimage，
    创建会产生空 preimage——这与 edit/create 路径已有的约定一致。
    """
    changed = sorted(
        path
        for path in before.digests.keys() | after.digests.keys()
        if before.digests.get(path) != after.digests.get(path)
    )
    return [
        _ExternalChange(
            path=path,
            preimage_digest=before.digests.get(path, ""),
            postimage_digest=after.digests.get(path, ""),
            before_content=before.contents.get(path, ""),
        )
        for path in changed
    ]


def _to_patch_spec(
    op: PatchEditOp | PatchCreateOp | PatchDeleteOp | PatchMoveOp,
) -> PatchOpSpec:
    """将契约操作转换为与 port 无关的 spec（工作区永远不会看到 pydantic 模型）。"""
    if isinstance(op, PatchEditOp):
        return PatchOpSpec(
            kind="edit",
            path=op.path,
            old=op.old_string,
            new=op.new_string,
            occurrence=op.occurrence,
            replace_all=op.replace_all,
        )
    if isinstance(op, PatchCreateOp):
        return PatchOpSpec(kind="create", path=op.path, content=op.content)
    if isinstance(op, PatchDeleteOp):
        return PatchOpSpec(kind="delete", path=op.path)
    return PatchOpSpec(kind="move", src=op.src, dest=op.dest)


def _ok(call: ToolCallProposal, payload: dict[str, object], truncated: bool = False) -> ToolResult:
    return ToolResult(
        call_id=call.call_id,
        tool_name=call.tool_name,
        status=ToolStatus.OK,
        payload=dict(payload),
        truncated=truncated,
    )


def _error(call: ToolCallProposal, code: ToolErrorCode, message: str) -> ToolResult:
    return ToolResult(
        call_id=call.call_id,
        tool_name=call.tool_name,
        status=ToolStatus.ERROR,
        error_code=code,
        message=message,
    )


def _map_ws_code(exc: WorkspaceError) -> ToolErrorCode:
    return _ERROR_CODES.get(exc.code, ToolErrorCode.INTERNAL)


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"... [truncated {len(text) - limit} chars]"


def _summarize_payload(result: ToolResult) -> str:
    if not result.payload:
        return result.status.value
    return _clip(json.dumps(result.payload, ensure_ascii=False, default=str), 200)
