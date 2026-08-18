"""工具参数和结果契约。

每个工具都有严格的 Pydantic 参数模型；提供给模型的 JSON Schema 从这些模型
生成，因此校验逻辑和文档不会彼此漂移。
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator

from haven.contracts.base import StrictModel
from haven.contracts.model import ToolSchema
from haven.domain.enums import ToolErrorCode, ToolStatus

TOOL_VERSION = "4"


class RepoListArgs(StrictModel):
    """列出工作区内某个目录的条目。"""

    path: str = Field(default=".", description="Directory path relative to the workspace root.")
    max_entries: int = Field(default=200, ge=1, le=500)


class RepoSearchArgs(StrictModel):
    """使用正则表达式搜索文件内容。"""

    pattern: str = Field(min_length=1, max_length=512, description="Regular expression.")
    path: str = Field(default=".", description="Directory to search, relative to workspace root.")
    max_results: int = Field(default=50, ge=1, le=100)


class RepoReadArgs(StrictModel):
    """按行范围读取文件。"""

    path: str = Field(description="File path relative to the workspace root.")
    start_line: int = Field(default=1, ge=1)
    max_lines: int = Field(default=400, ge=1, le=2000)


class RepoEditArgs(StrictModel):
    """在现有文件中将 old_string 的匹配项替换为 new_string。"""

    path: str = Field(description="File path relative to the workspace root.")
    old_string: str = Field(
        min_length=1,
        max_length=65536,
        description=(
            "Exact text to replace, including indentation. Must occur exactly once "
            "unless occurrence or replace_all is set."
        ),
    )
    new_string: str = Field(max_length=65536, description="Replacement text.")
    occurrence: int | None = Field(
        default=None,
        ge=1,
        description="1-based index of the occurrence to replace when old_string is not unique.",
    )
    replace_all: bool = Field(
        default=False, description="Replace every occurrence (use for renames)."
    )
    summary: str = Field(default="", max_length=300, description="One-line intent of this change.")


class RepoCreateArgs(StrictModel):
    """创建新文件。路径已存在时失败。"""

    path: str = Field(description="New file path relative to the workspace root.")
    content: str = Field(max_length=262144, description="Full contents of the new file.")
    summary: str = Field(default="", max_length=300, description="One-line intent of this change.")


class RepoDeleteArgs(StrictModel):
    """删除现有文件。需要审批；文件内容会在审批时固定，因此并发修改会导致
    操作失败并关闭。"""

    path: str = Field(description="File path relative to the workspace root.")
    summary: str = Field(default="", max_length=300, description="One-line intent of this change.")


class RepoMoveArgs(StrictModel):
    """移动或重命名文件。目标已存在时失败，因此移动永远不会静默覆盖文件。"""

    src: str = Field(description="Existing file path relative to the workspace root.")
    dest: str = Field(description="New path relative to the workspace root.")
    summary: str = Field(default="", max_length=300, description="One-line intent of this change.")


class PatchEditOp(StrictModel):
    """替换已经存在的文件中的文本（文件可以在磁盘上存在，也可以由本补丁较早的
    操作创建）。"""

    kind: Literal["edit"] = "edit"
    path: str = Field(description="File path relative to the workspace root.")
    old_string: str = Field(min_length=1, max_length=65536)
    new_string: str = Field(max_length=65536)
    occurrence: int | None = Field(default=None, ge=1)
    replace_all: bool = False


class PatchCreateOp(StrictModel):
    """创建确实全新的文件。"""

    kind: Literal["create"] = "create"
    path: str = Field(description="New file path relative to the workspace root.")
    content: str = Field(max_length=262144)


class PatchDeleteOp(StrictModel):
    """删除现有文件。"""

    kind: Literal["delete"] = "delete"
    path: str = Field(description="File path relative to the workspace root.")


class PatchMoveOp(StrictModel):
    """移动或重命名文件；目标路径不得存在。"""

    kind: Literal["move"] = "move"
    src: str = Field(description="Existing file path relative to the workspace root.")
    dest: str = Field(description="New path relative to the workspace root.")


PatchOp = Annotated[
    PatchEditOp | PatchCreateOp | PatchDeleteOp | PatchMoveOp, Field(discriminator="kind")
]


class RepoApplyPatchArgs(StrictModel):
    """应用一个多文件补丁：包含多个操作、一次审批，并在失败时回滚的原子提交。"""

    operations: tuple[PatchOp, ...] = Field(
        min_length=1,
        max_length=32,
        description=(
            "Ordered operations applied as one transaction. Later operations "
            "see the effects of earlier ones."
        ),
    )
    summary: str = Field(default="", max_length=300, description="One-line intent of this patch.")


class RepoExecArgs(StrictModel):
    """在操作系统沙箱中运行一个程序。"""

    argv: tuple[str, ...] = Field(
        min_length=1,
        max_length=64,
        description=(
            'Program and arguments as separate items, e.g. ["pytest", "-q"]. This '
            "is not a shell line: pipes, globs, and redirection are not interpreted."
        ),
    )
    cwd: str = Field(default=".", description="Working directory relative to the workspace root.")
    timeout_seconds: float = Field(default=60.0, ge=1.0, le=300.0)
    summary: str = Field(default="", max_length=300, description="One-line intent of this run.")

    @field_validator("argv")
    @classmethod
    def _bound_item_length(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        # Field(max_length=...) 限制的是元组，而不是其中的字符串。
        if any(len(item) > 4096 for item in value):
            raise ValueError("each argv item must be at most 4096 characters")
        return value


class PlanStep(StrictModel):
    title: str = Field(min_length=1, max_length=120)
    status: Literal["pending", "in_progress", "done"] = "pending"


class TaskPlanArgs(StrictModel):
    """记录或更新当前任务的有序计划。"""

    steps: tuple[PlanStep, ...] = Field(
        min_length=1, max_length=12, description="Ordered steps, shortest useful list."
    )


class RepoDiffArgs(StrictModel):
    """显示本次运行产生的变更差异（不包括运行前已存在的变更）。"""


class RepoCheckArgs(StrictModel):
    """运行已注册的验证配方（例如项目的测试命令）。"""

    recipe_id: str = Field(min_length=1, max_length=100, description="Registered recipe id.")


ToolArgs = (
    RepoListArgs
    | RepoSearchArgs
    | RepoReadArgs
    | RepoEditArgs
    | RepoCreateArgs
    | RepoDeleteArgs
    | RepoMoveArgs
    | RepoApplyPatchArgs
    | RepoExecArgs
    | RepoDiffArgs
    | RepoCheckArgs
    | TaskPlanArgs
)

ARGS_MODELS: dict[str, type[ToolArgs]] = {
    "repo.list": RepoListArgs,
    "repo.search": RepoSearchArgs,
    "repo.read": RepoReadArgs,
    "repo.edit": RepoEditArgs,
    "repo.create": RepoCreateArgs,
    "repo.delete": RepoDeleteArgs,
    "repo.move": RepoMoveArgs,
    "repo.apply_patch": RepoApplyPatchArgs,
    "repo.exec": RepoExecArgs,
    "repo.diff": RepoDiffArgs,
    "repo.check": RepoCheckArgs,
    "task.plan": TaskPlanArgs,
}

TOOL_DESCRIPTIONS: dict[str, str] = {
    "repo.list": "List directory entries inside the workspace.",
    "repo.search": (
        "Search file contents in the workspace with a regular expression. "
        "Returns matching lines with file and line number."
    ),
    "repo.read": (
        "Read a file from the workspace by line range. You must read a file before you can edit it."
    ),
    "repo.edit": (
        "Replace text in an EXISTING file. Requires user approval. The file must have "
        "been read first and be unchanged since that read. old_string must be unique "
        "unless you set occurrence (1-based) or replace_all (for renames)."
    ),
    "repo.create": (
        "Create a NEW file with the given contents. Requires user approval. Fails if the "
        "path already exists — use repo.edit for existing files. Parent directories are "
        "created as needed."
    ),
    "repo.delete": (
        "Delete an EXISTING file. Requires user approval. The file's content is "
        "pinned when you propose the deletion, so if it changes before you are "
        "approved the delete is refused and you must look again."
    ),
    "repo.move": (
        "Move or rename an existing file to a new path. Requires user approval. "
        "Fails if the destination already exists — delete or edit it first — so a "
        "move never silently overwrites."
    ),
    "repo.apply_patch": (
        "Apply ONE patch spanning several files: an ordered list of edit / "
        "create / delete / move operations approved together as a single "
        "reviewable diff and committed atomically — on any failure the whole "
        "patch is rolled back. Prefer this over a chain of repo.edit calls "
        "whenever a change touches more than one file (refactors, renames with "
        "import updates). The same rules as the single-file tools apply: edit "
        "an existing file only after reading it, create only genuinely new "
        "paths, later operations see the effects of earlier ones."
    ),
    "repo.exec": (
        "Run a program inside an OS sandbox: no network, the workspace is "
        "READ-ONLY (only a scratch directory is writable), your home directory "
        "unreadable. A command that tries to write workspace files fails with a "
        "permission error — change files through repo.edit/create/delete/move "
        "instead. Requires user approval unless the command is a well-known "
        "read-only one. Pass argv as separate items; shell syntax is NOT "
        "interpreted, so name an interpreter explicitly if you need it. Output "
        "is an observation only — it is never verification evidence, so run "
        "repo.check when you need to prove a change works."
    ),
    "repo.diff": "Show the accumulated diff of the changes made in this run.",
    "repo.check": (
        "Run a registered verification recipe (such as the project's tests). "
        "Requires user approval. Only registered recipe ids are accepted."
    ),
    "task.plan": (
        "Record or update your ordered plan for this task. Call it once early on a "
        "multi-step task, then again to mark steps done. The plan is shown to the "
        "user and is re-sent to you every turn, so it survives context truncation."
    ),
}


class ToolResult(StrictModel):
    """反馈给模型和追踪流的结构化、有界结果。"""

    call_id: str
    tool_name: str
    status: ToolStatus
    error_code: ToolErrorCode | None = None
    message: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    truncated: bool = False
    duration_ms: int = 0

    def to_model_text(self) -> str:
        """渲染为模型对话记录中的文本，保证有界且类型清晰。"""
        body: dict[str, Any] = {"status": self.status.value}
        if self.error_code is not None:
            body["error_code"] = self.error_code.value
        if self.message:
            body["message"] = self.message
        if self.payload:
            body["result"] = self.payload
        if self.truncated:
            body["truncated"] = True
        return json.dumps(body, ensure_ascii=False)


class RecipeSpec(StrictModel):
    """已注册的验证命令。模型只能选择其 id。"""

    id: str
    argv: tuple[str, ...] = Field(min_length=1)
    timeout_seconds: float = 120.0
    #: 配方与其他进程一样在沙箱中运行。确实需要网络的检查（例如集成测试套件）
    #: 可以选择启用，因为配方来自用户编写的配置，而不是模型。
    allow_network: bool = False
    #: 此配方除所有配方默认获得的解释器前缀外，还可以读取的额外根目录。工具链
    #: 会把依赖缓存放在 $HOME（`~/.m2`、`~/.gradle`）下，而沙箱默认会隐藏它。
    #: 只有检查配方可以声明这些目录，因为只有它的 argv 由用户编写（ADR 0029）；
    #: 这些目录永远不可写，也永远不提供给 `repo.exec`。
    readable_roots: tuple[str, ...] = ()


def tool_schemas() -> tuple[ToolSchema, ...]:
    """根据参数模型构建面向提供商的工具模式列表。"""
    schemas = []
    for name, model in ARGS_MODELS.items():
        schema = model.model_json_schema()
        schema.pop("title", None)
        schemas.append(
            ToolSchema(
                name=name,
                description=TOOL_DESCRIPTIONS[name],
                parameters=schema,
            )
        )
    return tuple(schemas)
