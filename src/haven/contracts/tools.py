"""Tool argument and result contracts.

Each tool has a strict Pydantic args model; the JSON Schema handed to the
model is generated from these models, so validation and documentation can
never drift apart.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import Field, field_validator

from haven.contracts.base import StrictModel
from haven.contracts.model import ToolSchema
from haven.domain.enums import ToolErrorCode, ToolStatus

TOOL_VERSION = "3"


class RepoListArgs(StrictModel):
    """List entries of a directory inside the workspace."""

    path: str = Field(default=".", description="Directory path relative to the workspace root.")
    max_entries: int = Field(default=200, ge=1, le=500)


class RepoSearchArgs(StrictModel):
    """Search file contents with a regular expression."""

    pattern: str = Field(min_length=1, max_length=512, description="Regular expression.")
    path: str = Field(default=".", description="Directory to search, relative to workspace root.")
    max_results: int = Field(default=50, ge=1, le=100)


class RepoReadArgs(StrictModel):
    """Read a file by line range."""

    path: str = Field(description="File path relative to the workspace root.")
    start_line: int = Field(default=1, ge=1)
    max_lines: int = Field(default=400, ge=1, le=2000)


class RepoEditArgs(StrictModel):
    """Replace occurrences of old_string with new_string in an existing file."""

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
    """Create a new file. Fails if the path already exists."""

    path: str = Field(description="New file path relative to the workspace root.")
    content: str = Field(max_length=262144, description="Full contents of the new file.")
    summary: str = Field(default="", max_length=300, description="One-line intent of this change.")


class RepoDeleteArgs(StrictModel):
    """Delete an existing file. Requires approval; the file's content is pinned
    at approval time so a concurrent change fails closed."""

    path: str = Field(description="File path relative to the workspace root.")
    summary: str = Field(default="", max_length=300, description="One-line intent of this change.")


class RepoMoveArgs(StrictModel):
    """Move or rename a file. Fails if the destination already exists, so a move
    can never silently overwrite."""

    src: str = Field(description="Existing file path relative to the workspace root.")
    dest: str = Field(description="New path relative to the workspace root.")
    summary: str = Field(default="", max_length=300, description="One-line intent of this change.")


class RepoExecArgs(StrictModel):
    """Run one program inside an OS sandbox."""

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
        # Field(max_length=...) bounds the tuple, not the strings inside it.
        if any(len(item) > 4096 for item in value):
            raise ValueError("each argv item must be at most 4096 characters")
        return value


class PlanStep(StrictModel):
    title: str = Field(min_length=1, max_length=120)
    status: Literal["pending", "in_progress", "done"] = "pending"


class TaskPlanArgs(StrictModel):
    """Record or update the ordered plan for the current task."""

    steps: tuple[PlanStep, ...] = Field(
        min_length=1, max_length=12, description="Ordered steps, shortest useful list."
    )


class RepoDiffArgs(StrictModel):
    """Show the diff of changes made by this run (not pre-existing changes)."""


class RepoCheckArgs(StrictModel):
    """Run a registered verification recipe (e.g. the project's test command)."""

    recipe_id: str = Field(min_length=1, max_length=100, description="Registered recipe id.")


ToolArgs = (
    RepoListArgs
    | RepoSearchArgs
    | RepoReadArgs
    | RepoEditArgs
    | RepoCreateArgs
    | RepoDeleteArgs
    | RepoMoveArgs
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
    """Structured, bounded result fed back to the model and the trace."""

    call_id: str
    tool_name: str
    status: ToolStatus
    error_code: ToolErrorCode | None = None
    message: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    truncated: bool = False
    duration_ms: int = 0

    def to_model_text(self) -> str:
        """Render for the model transcript, bounded and clearly typed."""
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
    """A registered verification command. The model can only pick an id."""

    id: str
    argv: tuple[str, ...] = Field(min_length=1)
    timeout_seconds: float = 120.0
    #: Recipes run sandboxed like any other process. A check that genuinely
    #: needs the network (an integration suite) can opt in, because the recipe
    #: comes from user-authored config rather than from the model.
    allow_network: bool = False


def tool_schemas() -> tuple[ToolSchema, ...]:
    """Build the provider-facing tool schema list from the args models."""
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
