"""沙箱进程、检查配方和进程写入归因。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from haven.application.approval_cards import ToolPreview
from haven.application.emitter import EventEmitter
from haven.application.state import RunContext
from haven.application.tool_execution_types import (
    ExecuteHandler,
    ToolExecution,
    clip,
    error_result,
    ok_result,
)
from haven.contracts.events import EvidenceRecorded, Notice
from haven.contracts.model import ToolCallProposal
from haven.contracts.tools import RecipeSpec, RepoCheckArgs, RepoExecArgs, ToolArgs
from haven.domain.enums import EffectState, ToolErrorCode
from haven.domain.evidence import CheckEvidence, EditEvidence
from haven.ports.executor import ExecSpec, ExecutorPort
from haven.ports.sandbox import (
    SandboxLauncher,
    SandboxSpec,
    default_private_roots,
    default_readable_roots,
)
from haven.ports.session import ExecutionRecord, SessionStorePort
from haven.ports.workspace import WorkspacePort, WorkspaceSnapshot


class ProcessToolExecutor:
    """执行受控命令与检查，并把所有工作区写入归入证据账本。"""

    def __init__(
        self,
        *,
        workspace: WorkspacePort,
        executor: ExecutorPort,
        store: SessionStorePort,
        emitter: EventEmitter,
        recipes: dict[str, RecipeSpec],
        launcher: SandboxLauncher | None,
        scratch_dir: Path,
    ) -> None:
        self._workspace = workspace
        self._executor = executor
        self._store = store
        self._emitter = emitter
        self._recipes = recipes
        self._launcher = launcher
        self._scratch_dir = scratch_dir
        self.handlers: dict[str, ExecuteHandler] = {
            "repo.exec": self.execute_exec,
            "repo.check": self.execute_check,
        }

    def replace_scratch_dir(self, scratch_dir: Path) -> None:
        """切换到新运行独占的临时目录；旧目录由 RunService 负责回收。"""
        self._scratch_dir = scratch_dir

    def sandbox_spec(self) -> SandboxSpec:
        """构造只读工作区、可写 scratch、禁网的命令沙箱规格。"""
        return SandboxSpec(
            workspace_root=self._workspace.root,
            scratch_dir=self._scratch_dir,
            writable=False,
            allow_network=False,
            private_roots=default_private_roots(),
            extra_readable_roots=default_readable_roots(),
        )

    def describe_sandbox(self) -> str:
        """返回命令执行沙箱的可读描述，后端不可用时明确标记不可用。"""
        if self._launcher is None:
            return "sandbox: unavailable"
        return self._launcher.describe(self.sandbox_spec())

    async def capture_snapshot(self) -> WorkspaceSnapshot:
        """在线程中计算完整快照，避免 exec/check 冻结事件循环。"""
        return await asyncio.to_thread(self._workspace.capture_snapshot)

    async def execute_exec(
        self,
        ctx: RunContext,
        call: ToolCallProposal,
        args: ToolArgs,
        ticket_digest: str,
        preview: ToolPreview,
    ) -> ToolExecution:
        """在沙箱中执行任意命令，并将前后工作区变化归入证据账本。"""
        assert isinstance(args, RepoExecArgs)
        if self._launcher is None:
            raise RuntimeError("refusing to exec without a sandbox backend")
        before = await self.capture_snapshot()
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
            sandbox=self.sandbox_spec(),
        )
        try:
            outcome = await self._executor.run_exec(spec)
            after = await self.capture_snapshot()
            tampered = await self.record_process_writes(ctx, call.tool_name, before, after)
        except BaseException:
            await self._store.update_execution_state(
                ctx.run_id, call.call_id, EffectState.EFFECT_UNKNOWN
            )
            raise

        await self._store.update_execution_state(ctx.run_id, call.call_id, EffectState.CONFIRMED)
        if tampered:
            return ToolExecution(
                error_result(
                    call,
                    ToolErrorCode.PROTECTED_PATH_TAMPERED,
                    "the command modified protected path(s) "
                    f"{', '.join(tampered)}; this is a boundary violation",
                )
            )
        if outcome.timed_out:
            return ToolExecution(
                error_result(
                    call, ToolErrorCode.TIMEOUT, f"command timed out after {args.timeout_seconds}s"
                )
            )
        return ToolExecution(
            ok_result(
                call,
                {
                    "exit_code": outcome.exit_code,
                    "duration_ms": outcome.duration_ms,
                    "stdout_tail": clip(outcome.stdout_tail, 4000),
                    "stderr_tail": clip(outcome.stderr_tail, 2000),
                    "sandbox": self._launcher.backend,
                },
                truncated=outcome.truncated,
            )
        )

    async def execute_check(
        self,
        ctx: RunContext,
        call: ToolCallProposal,
        args: ToolArgs,
        ticket_digest: str,
        preview: ToolPreview,
    ) -> ToolExecution:
        """执行已注册检查配方，拒绝保护路径被改写的结果并记录检查证据。"""
        assert isinstance(args, RepoCheckArgs)
        recipe = self._recipes[args.recipe_id]
        before = await self.capture_snapshot()
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
        try:
            outcome = await self._executor.run_recipe(recipe, self._workspace.root)
            after = await self.capture_snapshot()
            tampered = await self.record_process_writes(ctx, call.tool_name, before, after)
        except BaseException:
            await self._store.update_execution_state(
                ctx.run_id, call.call_id, EffectState.EFFECT_UNKNOWN
            )
            raise

        await self._store.update_execution_state(ctx.run_id, call.call_id, EffectState.CONFIRMED)
        if tampered:
            return ToolExecution(
                error_result(
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
                error_result(
                    call,
                    ToolErrorCode.TIMEOUT,
                    f"recipe {recipe.id!r} timed out after {recipe.timeout_seconds}s",
                )
            )
        return ToolExecution(
            ok_result(
                call,
                {
                    "recipe_id": recipe.id,
                    "exit_code": outcome.exit_code,
                    "duration_ms": outcome.duration_ms,
                    "stdout_tail": clip(outcome.stdout_tail, 4000),
                    "stderr_tail": clip(outcome.stderr_tail, 2000),
                },
                truncated=outcome.truncated,
            )
        )

    async def record_process_writes(
        self,
        ctx: RunContext,
        tool_name: str,
        before: WorkspaceSnapshot,
        after: WorkspaceSnapshot,
    ) -> list[str]:
        """比较进程前后快照，记录外部编辑并返回被篡改的保护路径。"""
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

        changes = detect_changes(before, after)
        if not changes:
            return tampered
        summary = f"{len(changes)} file(s) changed by {tool_name}: " + ", ".join(
            change.path for change in changes[:5]
        )
        envelope = await self._emitter.emit(
            ctx.run_id,
            EvidenceRecorded(run_id=ctx.run_id, evidence_kind="edit", summary=clip(summary, 200)),
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


@dataclass(frozen=True, slots=True)
class ExternalChange:
    """进程执行前后工作区快照之间的一项外部文件变化。"""

    #: 进程执行期间摘要发生变化的规范化路径。
    path: str
    #: 进程运行前观察到的摘要。
    preimage_digest: str
    #: 进程返回后观察到的摘要。
    postimage_digest: str
    #: 进程运行前保留的文本，用于生成可读差异。
    before_content: str | None


def detect_changes(before: WorkspaceSnapshot, after: WorkspaceSnapshot) -> list[ExternalChange]:
    """从两次工作区快照中提取摘要变化及可用的变更前文本。"""
    changed = sorted(
        path
        for path in before.digests.keys() | after.digests.keys()
        if before.digests.get(path) != after.digests.get(path)
    )
    return [
        ExternalChange(
            path=path,
            preimage_digest=before.digests.get(path, ""),
            postimage_digest=after.digests.get(path, ""),
            before_content=(before.contents.get(path, "") if path in before.digests else None),
        )
        for path in changed
    ]
