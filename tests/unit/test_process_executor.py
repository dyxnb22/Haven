"""Executor tests: exit codes, timeout, output bombs, cancellation."""

import asyncio
import sys
from pathlib import Path

import pytest

from haven.adapters.process_executor import OUTPUT_CAP_BYTES, ProcessExecutor
from haven.contracts.tools import RecipeSpec

PY = sys.executable


def recipe(*code: str, timeout: float = 30.0) -> RecipeSpec:
    return RecipeSpec(id="test", argv=(PY, "-c", *code), timeout_seconds=timeout)


async def test_successful_command(tmp_path: Path) -> None:
    outcome = await ProcessExecutor().run_recipe(recipe("print('ok')"), tmp_path)
    assert outcome.exit_code == 0
    assert "ok" in outcome.stdout_tail
    assert not outcome.timed_out


async def test_failing_command(tmp_path: Path) -> None:
    outcome = await ProcessExecutor().run_recipe(recipe("import sys; sys.exit(3)"), tmp_path)
    assert outcome.exit_code == 3


async def test_timeout_terminates_process(tmp_path: Path) -> None:
    outcome = await ProcessExecutor().run_recipe(
        recipe("import time; time.sleep(60)", timeout=0.5), tmp_path
    )
    assert outcome.timed_out
    assert outcome.exit_code == 124


async def test_output_bomb_is_bounded(tmp_path: Path) -> None:
    outcome = await ProcessExecutor().run_recipe(recipe("print('x' * 100_000_000)"), tmp_path)
    assert outcome.exit_code == 0
    assert outcome.truncated
    assert len(outcome.stdout_tail.encode()) <= OUTPUT_CAP_BYTES


async def test_cancellation_kills_process(tmp_path: Path) -> None:
    task = asyncio.create_task(
        ProcessExecutor().run_recipe(recipe("import time; time.sleep(60)"), tmp_path)
    )
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_env_is_scrubbed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HAVEN_SECRET_TOKEN", "leak-me")
    outcome = await ProcessExecutor().run_recipe(
        recipe("import os; print(os.environ.get('HAVEN_SECRET_TOKEN', 'MISSING'))"),
        tmp_path,
    )
    assert "MISSING" in outcome.stdout_tail
    assert "leak-me" not in outcome.stdout_tail
