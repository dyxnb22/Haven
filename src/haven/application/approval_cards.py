"""工具审批卡片的纯渲染逻辑。"""

import json
import shlex
from collections.abc import Callable

from haven.contracts.tools import (
    RecipeProposeArgs,
    RecipeSpec,
    RepoApplyPatchArgs,
    RepoCheckArgs,
    RepoCreateArgs,
    RepoDeleteArgs,
    RepoEditArgs,
    RepoExecArgs,
    RepoMoveArgs,
    TaskInputArgs,
    TaskStartArgs,
    ToolArgs,
)
from haven.domain.exec_policy import ExecClass, classify_argv
from haven.ports.workspace import EditPreview, PatchPreview

ToolPreview = EditPreview | PatchPreview | None
CardHandler = Callable[[ToolArgs, ToolPreview], tuple[str, str]]

PREVIEW_CHARS = 4_000

#: shell 直通的警告语。四处渲染共用同一句，因此不会互相漂移。
_PASSTHROUGH_WARNING = (
    "WARNING: this interprets an arbitrary script, so the command "
    "above does not describe everything it may do."
)


def _passthrough_warning(argv: tuple[str, ...]) -> str | None:
    """命令是 shell 直通时返回警告，否则 None。

    `bash -c '…'`、`python -c '…'` 的解释内容不在命令行里，所以卡片上那一行
    `$ …` 描述不了它可能做的一切。`repo.exec` 与 `task.start` 一直在说这件事；
    `repo.check` 和 `recipe.propose` 却不说——可同一条命令经 recipe 通道执行时，同样
    能碰到沙箱里的任何东西，而卡片给出的权限摘要（"workspace writable, no network"）
    会让人以为已经看全了。
    """
    return _PASSTHROUGH_WARNING if classify_argv(argv) is ExecClass.SHELL_PASSTHROUGH else None


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
            "task.start": self._card_task_start,
            "task.input": self._card_task_input,
            "repo.check": self._card_check,
            "recipe.propose": self._card_recipe_propose,
        }

    def render(self, tool_name: str, args: ToolArgs, preview: ToolPreview) -> tuple[str, str]:
        """按工具类型生成审批摘要和有界预览；未知工具返回空字符串。"""
        if tool_name.startswith("mcp."):
            return self._card_mcp(tool_name, args, preview)
        handler = self.handlers.get(tool_name)
        return handler(args, preview) if handler is not None else ("", "")

    def _card_mcp(self, tool_name: str, args: ToolArgs, preview: ToolPreview) -> tuple[str, str]:
        parts = tool_name.split(".", 2)
        server = parts[1] if len(parts) > 1 else "unknown"
        subtool = parts[2] if len(parts) > 2 else tool_name
        summary = f"call MCP tool {subtool!r} on server {server!r}"
        raw = getattr(args, "arguments", {})
        formatted_args = json.dumps(raw, indent=2, ensure_ascii=False) if raw else "{}"
        preview_text = (
            f"MCP Server: {server}\n"
            f"Tool: {subtool}\n"
            f"Arguments:\n{_clip(formatted_args, PREVIEW_CHARS)}"
        )
        return summary, preview_text

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
        warning = _passthrough_warning(args.argv)
        if warning:
            lines.append(warning)
        summary = f"run {shlex.join(args.argv)} in {args.cwd}{self._intent(args.summary)}"
        return summary, "\n".join(lines)

    def _card_task_start(self, args: ToolArgs, preview: ToolPreview) -> tuple[str, str]:
        if not isinstance(args, TaskStartArgs):
            return "", ""
        lines = [f"$ {shlex.join(args.argv)} (background task)", self._sandbox_description()]
        lines.append(
            "I/O: POSIX PTY (merged output)" if args.terminal else "I/O: stdin/stdout pipes"
        )
        warning = _passthrough_warning(args.argv)
        if warning:
            lines.append(warning)
        summary = (
            f"start background task {shlex.join(args.argv)} in {args.cwd}"
            f"{self._intent(args.description)}"
        )
        return summary, "\n".join(lines)

    def _card_task_input(self, args: ToolArgs, preview: ToolPreview) -> tuple[str, str]:
        if not isinstance(args, TaskInputArgs):
            return "", ""
        # JSON quoting makes newline, escape and terminal control bytes reviewable.
        return (
            f"send input to {args.task_id}",
            f"Text (JSON): {json.dumps(args.data, ensure_ascii=False)}\nEOF: {args.end_of_input}",
        )

    def _card_check(self, args: ToolArgs, preview: ToolPreview) -> tuple[str, str]:
        if not isinstance(args, RepoCheckArgs):
            return "", ""
        # 未注册的 recipe_id 必须走结构化拒绝，而不是抛 KeyError。
        # `ask()` 在流水线的第 5 阶段被调用，位于第 7 阶段那个兜底
        # `except Exception` 之前——一个裸字典索引会直接掀翻整次运行，
        # 模型连一个 ToolResult 都收不到，更谈不上自我纠正。
        recipe = self._recipes.get(args.recipe_id)
        if recipe is None:
            return (
                f"run check recipe {args.recipe_id!r}",
                f"recipe {args.recipe_id!r} is not registered in this run",
            )
        args_desc = f" with args {list(args.args)!r}" if args.args else ""
        summary = (
            f"run check recipe {args.recipe_id!r}{args_desc} "
            "(approving also covers identical re-runs for the rest of this run)"
        )
        network = "allowed" if recipe.allow_network else "denied"
        roots = ", ".join(recipe.readable_roots) if recipe.readable_roots else "none"
        full_argv = (*recipe.argv, *args.args)
        preview_lines = [
            "$ " + shlex.join(full_argv),
            "recipe permissions: workspace writable",
            f"network: {network}",
            f"additional readable roots: {roots}",
        ]
        warning = _passthrough_warning(full_argv)
        if warning:
            preview_lines.append(warning)
        return summary, "\n".join(preview_lines)

    def _card_recipe_propose(self, args: ToolArgs, preview: ToolPreview) -> tuple[str, str]:
        if not isinstance(args, RecipeProposeArgs):
            return "", ""
        summary = f"register ephemeral verification recipe {args.recipe_id!r}"
        lines = [
            "$ " + shlex.join(args.argv),
            f"recipe id: {args.recipe_id}",
            f"timeout: {args.timeout_seconds}s",
            "recipe permissions: workspace writable, no network",
        ]
        warning = _passthrough_warning(args.argv)
        if warning:
            lines.append(warning)
        if args.description:
            lines.append(f"description: {args.description}")
        return summary, "\n".join(lines)


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"... [truncated {len(text) - limit} chars]"
