"""执行器端口：已注册的验证配方和沙箱命令。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from haven.contracts.tools import RecipeSpec
from haven.ports.sandbox import SandboxSpec


@dataclass(frozen=True, slots=True)
class CheckOutcome:
    recipe_id: str
    exit_code: int
    duration_ms: int
    stdout_tail: str
    stderr_tail: str
    truncated: bool
    timed_out: bool


@dataclass(frozen=True, slots=True)
class ExecSpec:
    """一条需要在受限环境中运行的命令。

    `argv` 是提议的程序；包装在执行器内部完成，因此系统中只有一个可能忘记执行
    包装的位置。
    """

    argv: tuple[str, ...]
    cwd: Path
    timeout_seconds: float
    sandbox: SandboxSpec


@dataclass(frozen=True, slots=True)
class ExecOutcome:
    exit_code: int
    duration_ms: int
    stdout_tail: str
    stderr_tail: str
    truncated: bool
    timed_out: bool


class ExecutorPort(Protocol):
    async def run_recipe(self, recipe: RecipeSpec, workspace_root: Path) -> CheckOutcome: ...

    async def run_exec(self, spec: ExecSpec) -> ExecOutcome: ...
