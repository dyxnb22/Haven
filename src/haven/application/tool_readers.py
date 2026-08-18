"""只读工作区工具和运行内计划/差异查询。"""

from __future__ import annotations

from haven.application.approval_cards import PREVIEW_CHARS, ToolPreview
from haven.application.emitter import EventEmitter
from haven.application.state import RunContext
from haven.application.tool_execution_types import (
    MODEL_PAYLOAD_CHARS,
    ExecuteHandler,
    ToolExecution,
    clip,
    ok_result,
)
from haven.contracts.events import DiffPreview, EvidenceRecorded, PlanStepView, PlanUpdated
from haven.contracts.model import ToolCallProposal
from haven.contracts.tools import (
    RepoListArgs,
    RepoReadArgs,
    RepoSearchArgs,
    TaskPlanArgs,
    ToolArgs,
)
from haven.domain.digest import sha256_text
from haven.domain.evidence import DiffEvidence
from haven.ports.workspace import WorkspacePort


class ReadToolExecutor:
    """执行没有外部副作用的读取、计划和 diff 工具。"""

    def __init__(self, workspace: WorkspacePort, emitter: EventEmitter) -> None:
        self._workspace = workspace
        self._emitter = emitter
        self.handlers: dict[str, ExecuteHandler] = {
            "repo.list": self.execute_list,
            "repo.search": self.execute_search,
            "repo.read": self.execute_read,
            "repo.diff": self.execute_diff,
            "task.plan": self.execute_plan,
        }

    async def execute_list(
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
            ok_result(
                call,
                {
                    "path": listing.path,
                    "entries": [
                        {"name": entry.name, "dir": entry.is_dir, "size": entry.size_bytes}
                        for entry in listing.entries
                    ],
                },
                truncated=listing.truncated,
            )
        )

    async def execute_search(
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
            ok_result(
                call,
                {
                    "matches": [
                        {"path": match.path, "line": match.line_number, "text": match.line}
                        for match in found.matches
                    ],
                    "files_scanned": found.files_scanned,
                },
                truncated=found.truncated,
            )
        )

    async def execute_read(
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
            ok_result(
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

    async def execute_plan(
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
                    PlanStepView(title=clip(step.title, 120), status=step.status)
                    for step in ctx.plan
                ),
            ),
        )
        done = sum(1 for step in ctx.plan if step.status == "done")
        return ToolExecution(
            ok_result(call, {"steps": len(ctx.plan), "done": done, "recorded": True})
        )

    async def execute_diff(
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
                preview=clip(run_diff.diff, PREVIEW_CHARS),
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
            ok_result(
                call,
                {
                    "files": list(run_diff.files),
                    "insertions": run_diff.insertions,
                    "deletions": run_diff.deletions,
                    "diff": clip(run_diff.diff, MODEL_PAYLOAD_CHARS),
                },
                truncated=run_diff.truncated,
            )
        )
