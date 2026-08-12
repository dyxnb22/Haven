"""Executor port: registered verification recipes and sandboxed commands."""

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
    """One command to run confined.

    `argv` is the program as proposed; wrapping happens inside the executor so
    there is exactly one place that can forget to do it.
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
