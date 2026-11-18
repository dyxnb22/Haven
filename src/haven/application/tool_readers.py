"""只读工作区工具和运行内计划/差异查询。"""

from __future__ import annotations

import difflib

from haven.application.approval_cards import PREVIEW_CHARS, ToolPreview
from haven.application.code_ast import find_symbol_occurrences, outline_source
from haven.application.emitter import EventEmitter
from haven.application.planning import normalize_plan
from haven.application.skills import SkillCatalog, SkillError, list_skill_files, read_skill_file
from haven.application.state import RunContext
from haven.application.tool_execution_types import (
    MODEL_PAYLOAD_CHARS,
    DelegateHandler,
    ExecuteHandler,
    ToolExecution,
    clip,
    error_result,
    ok_result,
)
from haven.application.web_lookup import fetch_web_page, search_web_docs
from haven.contracts.events import (
    DiffPreview,
    EvidenceRecorded,
    PlanStepView,
    PlanUpdated,
)
from haven.contracts.model import ToolCallProposal
from haven.contracts.tools import (
    AgentDelegateArgs,
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
from haven.contracts.user_input import UserAskArgs, UserInputRequest
from haven.domain.digest import sha256_text
from haven.domain.enums import AgentMode, PlanStatus, ToolErrorCode
from haven.domain.evidence import DiffEvidence
from haven.ports.session import SessionStorePort
from haven.ports.workspace import WorkspaceError, WorkspacePort


class ReadToolExecutor:
    """执行没有外部副作用的读取、计划和 diff 工具。"""

    def __init__(
        self,
        workspace: WorkspacePort,
        emitter: EventEmitter,
        skills: SkillCatalog | None = None,
        *,
        delegate: DelegateHandler | None = None,
        store: SessionStorePort | None = None,
    ) -> None:
        self._workspace = workspace
        self._emitter = emitter
        self._delegate = delegate
        self._store = store
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
            "user.ask": self.execute_ask,
            "web.search": self.execute_web_search,
            "web.fetch": self.execute_web_fetch,
            "agent.delegate": self.execute_agent_delegate,
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

    async def execute_ask(
        self,
        ctx: RunContext,
        call: ToolCallProposal,
        args: ToolArgs,
        ticket_digest: str,
        preview: ToolPreview,
    ) -> ToolExecution:
        """记录追问；界面事件在 Actor 完成暂停和清理后发出。"""
        assert isinstance(args, UserAskArgs)
        request_id = "input-" + sha256_text(f"{ctx.run_id}:{ctx.usage.steps}:{call.call_id}")[:20]
        ctx.pending_input = UserInputRequest(
            request_id=request_id, call_id=call.call_id, questions=args.questions
        )
        return ToolExecution(ok_result(call, {"needs_input": True, "request_id": request_id}))

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
        try:
            plan = normalize_plan(ctx, args.steps)
        except ValueError as exc:
            return ToolExecution(error_result(call, ToolErrorCode.INVALID_ARGUMENTS, str(exc)))
        ctx.plan = plan
        ctx.plan_status = (
            PlanStatus.DRAFTING if ctx.agent_mode is AgentMode.PLAN else PlanStatus.EXECUTING
        )
        await self._emitter.emit(
            ctx.run_id,
            PlanUpdated(
                run_id=ctx.run_id,
                steps=tuple(PlanStepView.model_validate(step.model_dump()) for step in ctx.plan),
                plan_status=ctx.plan_status.value,
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
        if ctx.parent_run_id and ctx.file_scope:
            paths = [
                path
                for path in run_diff.files
                if any(
                    scope == "." or path == scope or path.startswith(scope.rstrip("/") + "/")
                    for scope in ctx.file_scope
                )
            ]
            chunks: list[str] = []
            originals = self._workspace.original_contents()
            for path in paths:
                original = originals.get(path) or ""
                current = ""
                facts = self._workspace.path_facts(path)
                if facts.exists:
                    current = (await self._workspace.read_file(path, 1, 2000)).content
                chunks.extend(
                    difflib.unified_diff(
                        original.splitlines(True),
                        current.splitlines(True),
                        fromfile=f"a/{path}",
                        tofile=f"b/{path}",
                    )
                )
            return ToolExecution(
                ok_result(
                    call,
                    {
                        "files": paths,
                        "diff": clip("".join(chunks), MODEL_PAYLOAD_CHARS),
                    },
                )
            )
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
        diff_digest = (
            await self._store.put_artifact(run_diff.diff.encode("utf-8"))
            if self._store is not None
            else sha256_text(run_diff.diff)
        )
        ctx.ledger = ctx.ledger.with_diff(
            DiffEvidence(
                seq=envelope.seq,
                files_changed=len(run_diff.files),
                insertions=run_diff.insertions,
                deletions=run_diff.deletions,
                diff_digest=diff_digest,
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
        payload = await search_web_docs(args.query, args.max_results)
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
        payload = await fetch_web_page(args.url, args.max_chars)
        if payload.get("status") == "error":
            return ToolExecution(
                error_result(call, ToolErrorCode.INTERNAL, payload.get("error", "fetch failed"))
            )
        return ToolExecution(ok_result(call, payload, truncated=payload.get("truncated", False)))

    async def execute_agent_delegate(
        self,
        ctx: RunContext,
        call: ToolCallProposal,
        args: ToolArgs,
        ticket_digest: str,
        preview: ToolPreview,
    ) -> ToolExecution:
        """向受限只读子 Agent 委派专门子任务（规划书 Phase P5）。"""
        if not isinstance(args, AgentDelegateArgs):
            return ToolExecution(
                error_result(call, ToolErrorCode.INVALID_ARGUMENTS, "invalid delegate args")
            )

        # 1. 深度限制：子 Agent 不能嵌套委派（深度固定为 1）
        if ctx.parent_run_id:
            return ToolExecution(
                error_result(
                    call,
                    ToolErrorCode.DENIED,
                    "Subagents cannot delegate to nested subagents (depth limited to 1)",
                )
            )

        # 2. 预算检查与额度分配
        rem_steps = ctx.budget.max_steps - ctx.usage.steps
        rem_tools = ctx.budget.max_tool_calls - ctx.usage.tool_calls
        if rem_steps <= 1 or rem_tools <= 1:
            return ToolExecution(
                error_result(
                    call,
                    ToolErrorCode.DENIED,
                    "Insufficient parent budget remaining to delegate subagent",
                )
            )

        if self._delegate is None:
            return ToolExecution(
                error_result(call, ToolErrorCode.DENIED, "subagent model runtime is unavailable")
            )
        return await self._delegate(ctx, call, args)
