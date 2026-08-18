"""从已验证工具参数收集确定性工作区事实和预览。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from haven.application.approval_cards import ToolPreview
from haven.application.state import RunContext
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
    ToolArgs,
)
from haven.domain.digest import canonical_json, sha256_text
from haven.domain.exec_policy import classify_argv
from haven.domain.policy import ToolFacts
from haven.ports.sandbox import SandboxLauncher
from haven.ports.workspace import EditPreview, PatchOpSpec, WorkspaceError, WorkspacePort

FactsHandler = Callable[
    [RunContext, ToolCallProposal, ToolArgs],
    Awaitable[tuple[ToolFacts, ToolPreview]],
]


class ToolFactsCollector:
    def __init__(
        self,
        workspace: WorkspacePort,
        recipes: dict[str, RecipeSpec],
        launcher: SandboxLauncher | None,
    ) -> None:
        self._workspace = workspace
        self._recipes = recipes
        self._launcher = launcher
        self.handlers: dict[str, FactsHandler] = {
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

    async def collect(
        self, ctx: RunContext, call: ToolCallProposal, args: ToolArgs
    ) -> tuple[ToolFacts, ToolPreview]:
        handler = self.handlers.get(call.tool_name)
        if handler is None:  # pragma: no cover - 接线测试保证不可达
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
            return _path_denied(call, facts.within_workspace, facts.is_protected, facts.normalized)
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
            return _path_denied(call, facts.within_workspace, facts.is_protected, facts.normalized)
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
            return _path_denied(call, facts.within_workspace, facts.is_protected, facts.normalized)
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
            return _path_denied(call, within, protected, src_facts.normalized)
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
        op_paths: list[str] = []
        for op in args.operations:
            op_paths.extend((op.src, op.dest) if op.kind == "move" else (op.path,))
        all_facts = [self._workspace.path_facts(path) for path in op_paths]
        within = all(fact.within_workspace for fact in all_facts)
        protected = any(fact.is_protected for fact in all_facts)
        if not within or protected:
            return ToolFacts(
                tool_name=call.tool_name,
                within_workspace=within,
                touches_protected_path=protected,
            ), None
        plan = await self._workspace.preview_patch(
            tuple(_to_patch_spec(op) for op in args.operations), ctx.files_read
        )
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
        return ToolFacts(
            tool_name=call.tool_name,
            recipe_registered=args.recipe_id in self._recipes,
        ), None

    async def _facts_stateless(
        self, ctx: RunContext, call: ToolCallProposal, args: ToolArgs
    ) -> tuple[ToolFacts, ToolPreview]:
        return ToolFacts(tool_name=call.tool_name), None


def _path_denied(
    call: ToolCallProposal, within: bool, protected: bool, path: str
) -> tuple[ToolFacts, ToolPreview]:
    return (
        ToolFacts(
            tool_name=call.tool_name,
            within_workspace=within,
            touches_protected_path=protected,
            path=path,
        ),
        None,
    )


def _to_patch_spec(
    op: PatchEditOp | PatchCreateOp | PatchDeleteOp | PatchMoveOp,
) -> PatchOpSpec:
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
