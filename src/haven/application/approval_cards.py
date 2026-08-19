"""工具审批卡片的纯渲染逻辑。"""

import shlex
from collections.abc import Callable

from haven.contracts.tools import (
    RecipeSpec,
    RepoApplyPatchArgs,
    RepoCheckArgs,
    RepoCreateArgs,
    RepoDeleteArgs,
    RepoEditArgs,
    RepoExecArgs,
    RepoMoveArgs,
    ToolArgs,
)
from haven.domain.exec_policy import ExecClass, classify_argv
from haven.ports.workspace import EditPreview, PatchPreview

ToolPreview = EditPreview | PatchPreview | None
CardHandler = Callable[[ToolArgs, ToolPreview], tuple[str, str]]

PREVIEW_CHARS = 4_000


class ApprovalCardRenderer:
    """把工具提议渲染成用户可审查的审批卡片。"""

    def __init__(
        self, recipes: dict[str, RecipeSpec], sandbox_description: Callable[[], str]
    ) -> None:
        self._recipes = recipes
        self._sandbox_description = sandbox_description
        self.handlers: dict[str, CardHandler] = {
            "repo.edit": self._card_edit,
            "repo.create": self._card_create,
            "repo.delete": self._card_delete,
            "repo.move": self._card_move,
            "repo.apply_patch": self._card_patch,
            "repo.exec": self._card_exec,
            "repo.check": self._card_check,
        }

    def render(self, tool_name: str, args: ToolArgs, preview: ToolPreview) -> tuple[str, str]:
        """按工具类型生成审批摘要和有界预览；未知工具返回空字符串。"""
        handler = self.handlers.get(tool_name)
        return handler(args, preview) if handler is not None else ("", "")

    @staticmethod
    def _intent(summary: str) -> str:
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
        lines = [f"$ {shlex.join(args.argv)}", self._sandbox_description()]
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
        network = "allowed" if recipe.allow_network else "denied"
        roots = ", ".join(recipe.readable_roots) if recipe.readable_roots else "none"
        preview_lines = [
            "$ " + shlex.join(recipe.argv),
            "recipe permissions: workspace writable",
            f"network: {network}",
            f"additional readable roots: {roots}",
        ]
        return summary, "\n".join(preview_lines)


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"... [truncated {len(text) - limit} chars]"
