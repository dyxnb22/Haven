"""Executor port for registered verification recipes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from haven.contracts.tools import RecipeSpec


@dataclass(frozen=True, slots=True)
class CheckOutcome:
    recipe_id: str
    exit_code: int
    duration_ms: int
    stdout_tail: str
    stderr_tail: str
    truncated: bool
    timed_out: bool


class ExecutorPort(Protocol):
    async def run_recipe(self, recipe: RecipeSpec, workspace_root: Path) -> CheckOutcome: ...
