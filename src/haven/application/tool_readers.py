"""只读工作区工具和运行内计划/差异查询。"""

from __future__ import annotations

from haven.application.approval_cards import PREVIEW_CHARS, ToolPreview
from haven.application.code_ast import find_symbol_occurrences, outline_source
from haven.application.emitter import EventEmitter
from haven.application.skills import SkillCatalog, SkillError, list_skill_files, read_skill_file
from haven.application.state import RunContext
from haven.application.tool_execution_types import (
    MODEL_PAYLOAD_CHARS,
    ExecuteHandler,
    ToolExecution,
    clip,
    error_result,
    ok_result,
)
from haven.application.web_lookup import fetch_web_page, search_web_docs
from haven.contracts.events import DiffPreview, EvidenceRecorded, PlanStepView, PlanUpdated
from haven.contracts.model import ToolCallProposal
from haven.contracts.tools import (
    RepoListArgs,
    RepoOutlineArgs,
    RepoReadArgs,
    RepoSearchArgs,
    RepoSymbolArgs,
    SkillReadArgs,
    TaskPlanArgs,
    ToolArgs,
    WebFetchArgs,
    WebSearchArgs,
)
from haven.domain.digest import sha256_text
from haven.domain.enums import ToolErrorCode
from haven.domain.evidence import DiffEvidence
from haven.ports.workspace import WorkspaceError, WorkspacePort


class ReadToolExecutor:
    """执行没有外部副作用的读取、计划和 diff 工具。"""

    def __init__(
        self,
        workspace: WorkspacePort,
        emitter: EventEmitter,
        skills: SkillCatalog | None = None,
    ) -> None:
        self._workspace = workspace
        self._emitter = emitter
        # 技能目录由组合根注入；离线或没有技能时为 None，此时 `skill.read` 返回
        # 一个结构化的 unknown_skill，而不是崩溃。
        self._skills = skills if skills is not None else SkillCatalog()
        self.handlers: dict[str, ExecuteHandler] = {
            "repo.list": self.execute_list,
            "repo.search": self.execute_search,
            "repo.read": self.execute_read,
            "repo.outline": self.execute_outline,
            "repo.symbol": self.execute_symbol,
            "skill.read": self.execute_skill_read,
            "repo.diff": self.execute_diff,
            "task.plan": self.execute_plan,
            "web.search": self.execute_web_search,
            "web.fetch": self.execute_web_fetch,
        }

    async def execute_skill_read(
        self,
        ctx: RunContext,
        call: ToolCallProposal,
        args: ToolArgs,
        ticket_digest: str,
        preview: ToolPreview,
    ) -> ToolExecution:
        """读取一个技能的正文或其目录下的辅助文件（ADR 0032）。

        刻意不写 `ctx.files_read`：那个字典按工作区规范化路径索引，被编辑的前像绑定
        消费；一个技能路径混进去只会让某条相对路径莫名其妙地满足"已读"条件。
        """
        assert isinstance(args, SkillReadArgs)
        skill = next((s for s in self._skills.skills if s.name == args.name), None)
        if skill is None:
            available = ", ".join(self._skills.names()) or "(none discovered)"
            return ToolExecution(
                error_result(
                    call,
                    ToolErrorCode.NOT_FOUND,
                    f"no discovered skill named {args.name!r}; available: {available}",
                )
            )
        try:
            content, truncated = read_skill_file(self._skills, skill, args.path)
        except SkillError as exc:
            # 失败即关闭，但以一个结构化的 ToolResult 呈现：模型需要知道为什么读不到，
            # 而不是收到一个异常。
            return ToolExecution(error_result(call, ToolErrorCode.DENIED, str(exc)))
        companion_files = list_skill_files(self._skills, skill)
        return ToolExecution(
            ok_result(
                call,
                {
                    "skill": skill.name,
                    "scope": skill.scope,
                    "digest": skill.digest,
                    "path": args.path,
                    "found": True,
                    "companion_files": list(companion_files),
                    "content": content[:MODEL_PAYLOAD_CHARS],
                },
                truncated=truncated,
            )
        )

    async def execute_list(
        self,
        ctx: RunContext,
        call: ToolCallProposal,
        args: ToolArgs,
        ticket_digest: str,
        preview: ToolPreview,
    ) -> ToolExecution:
        """列出工作区目录，并将条目元数据转换为工具结果。"""
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
        """搜索工作区文本，返回匹配行和扫描文件数。"""
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
        """读取指定行区间，并登记文件摘要以绑定后续编辑前像。"""
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

    async def execute_outline(
        self,
        ctx: RunContext,
        call: ToolCallProposal,
        args: ToolArgs,
        ticket_digest: str,
        preview: ToolPreview,
    ) -> ToolExecution:
        """提取指定源码文件的结构大纲（ADR 0035）。"""
        assert isinstance(args, RepoOutlineArgs)
        facts = self._workspace.path_facts(args.path)
        if not facts.exists:
            return ToolExecution(
                error_result(call, ToolErrorCode.NOT_FOUND, f"path not found: {args.path}")
            )
        if not facts.is_file:
            return ToolExecution(
                error_result(call, ToolErrorCode.DENIED, f"path is not a file: {args.path}")
            )
        try:
            read = await self._workspace.read_file(facts.normalized, 1, 5000)
        except WorkspaceError as exc:
            return ToolExecution(error_result(call, ToolErrorCode.DENIED, str(exc)))
        outline = outline_source(read.content, filename=facts.normalized)
        return ToolExecution(ok_result(call, outline))

    async def execute_symbol(
        self,
        ctx: RunContext,
        call: ToolCallProposal,
        args: ToolArgs,
        ticket_digest: str,
        preview: ToolPreview,
    ) -> ToolExecution:
        """快速在工作区内查找符号的定义与引用（ADR 0035）。"""
        assert isinstance(args, RepoSymbolArgs)
        if args.path:
            facts = self._workspace.path_facts(args.path)
            if not facts.exists:
                return ToolExecution(
                    error_result(call, ToolErrorCode.NOT_FOUND, f"path not found: {args.path}")
                )
            target_path: str | None = facts.normalized
        else:
            target_path = None
        results = find_symbol_occurrences(
            workspace_root=self._workspace.root,
            symbol_name=args.name,
            target_path=target_path,
        )
        if "error" in results:
            return ToolExecution(
                error_result(call, ToolErrorCode.INVALID_ARGUMENTS, results["error"])
            )
        return ToolExecution(ok_result(call, results, truncated=results.get("truncated", False)))

    async def execute_plan(
        self,
        ctx: RunContext,
        call: ToolCallProposal,
        args: ToolArgs,
        ticket_digest: str,
        preview: ToolPreview,
    ) -> ToolExecution:
        """替换运行中的任务计划并广播计划更新事件。"""
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
        """读取运行基线差异，记录 diff 证据并返回有界预览。"""
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

    async def execute_web_search(
        self,
        ctx: RunContext,
        call: ToolCallProposal,
        args: ToolArgs,
        ticket_digest: str,
        preview: ToolPreview,
    ) -> ToolExecution:
        assert isinstance(args, WebSearchArgs)
        payload = search_web_docs(args.query, args.max_results)
        return ToolExecution(ok_result(call, payload))

    async def execute_web_fetch(
        self,
        ctx: RunContext,
        call: ToolCallProposal,
        args: ToolArgs,
        ticket_digest: str,
        preview: ToolPreview,
    ) -> ToolExecution:
        assert isinstance(args, WebFetchArgs)
        payload = fetch_web_page(args.url, args.max_chars)
        if payload.get("status") == "error":
            return ToolExecution(
                error_result(call, ToolErrorCode.INTERNAL, payload.get("error", "fetch failed"))
            )
        return ToolExecution(ok_result(call, payload, truncated=payload.get("truncated", False)))
