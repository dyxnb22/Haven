"""Executor tests: exit codes, timeout, output bombs, cancellation."""

import asyncio
import sys
from pathlib import Path

import pytest

from haven.adapters.process_executor import OUTPUT_CAP_BYTES, ProcessExecutor
from haven.contracts.tools import RecipeSpec
from haven.ports.executor import ExecSpec
from haven.ports.sandbox import SandboxSpec
from tests.integration.fakes import RecordingLauncher

PY = sys.executable


def recipe(*code: str, timeout: float = 30.0) -> RecipeSpec:
    return RecipeSpec(id="test", argv=(PY, "-c", *code), timeout_seconds=timeout)


def exec_spec(tmp_path: Path, *code: str, timeout: float = 10.0) -> ExecSpec:
    return ExecSpec(
        argv=(PY, "-c", *code),
        cwd=tmp_path,
        timeout_seconds=timeout,
        sandbox=SandboxSpec(
            workspace_root=tmp_path, scratch_dir=tmp_path / "scratch", writable=True
        ),
    )


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


class TestRunExec:
    async def test_captures_stdout_and_exit_code(self, tmp_path: Path) -> None:
        executor = ProcessExecutor(launcher=RecordingLauncher())
        outcome = await executor.run_exec(exec_spec(tmp_path, "print('hello')"))
        assert outcome.exit_code == 0
        assert "hello" in outcome.stdout_tail

    async def test_nonzero_exit_is_reported_not_raised(self, tmp_path: Path) -> None:
        executor = ProcessExecutor(launcher=RecordingLauncher())
        outcome = await executor.run_exec(exec_spec(tmp_path, "raise SystemExit(3)"))
        assert outcome.exit_code == 3
        assert outcome.timed_out is False

    async def test_timeout_reports_124(self, tmp_path: Path) -> None:
        executor = ProcessExecutor(launcher=RecordingLauncher())
        outcome = await executor.run_exec(
            exec_spec(tmp_path, "import time; time.sleep(30)", timeout=1.0)
        )
        assert outcome.timed_out is True
        assert outcome.exit_code == 124

    async def test_every_exec_is_wrapped(self, tmp_path: Path) -> None:
        launcher = RecordingLauncher()
        await ProcessExecutor(launcher=launcher).run_exec(exec_spec(tmp_path, "pass"))
        assert len(launcher.calls) == 1

    async def test_recipes_are_wrapped_too(self, tmp_path: Path) -> None:
        """One wrapping site: a check is as confined as an exec."""
        launcher = RecordingLauncher()
        await ProcessExecutor(launcher=launcher).run_recipe(recipe("pass"), tmp_path)
        assert len(launcher.calls) == 1

    async def test_recipe_network_opt_in_reaches_the_spec(self, tmp_path: Path) -> None:
        launcher = RecordingLauncher()
        opted_in = RecipeSpec(id="net", argv=(PY, "-c", "pass"), allow_network=True)
        await ProcessExecutor(launcher=launcher).run_recipe(opted_in, tmp_path)
        assert launcher.calls[0][1].allow_network is True

    async def test_recipes_deny_network_by_default(self, tmp_path: Path) -> None:
        launcher = RecordingLauncher()
        await ProcessExecutor(launcher=launcher).run_recipe(recipe("pass"), tmp_path)
        assert launcher.calls[0][1].allow_network is False

    async def test_a_missing_program_is_a_result_not_an_exception(self, tmp_path: Path) -> None:
        """Found on Linux, where `make` is absent: create_subprocess_exec raises
        FileNotFoundError, which would escape into the agent loop and violate
        the invariant that a tool call always returns a structured result."""
        executor = ProcessExecutor(launcher=RecordingLauncher())
        outcome = await executor.run_exec(
            ExecSpec(
                argv=("definitely-not-a-real-program-xyz",),
                cwd=tmp_path,
                timeout_seconds=10.0,
                sandbox=SandboxSpec(
                    workspace_root=tmp_path, scratch_dir=tmp_path / "scratch", writable=True
                ),
            )
        )
        assert outcome.exit_code == 127
        assert "definitely-not-a-real-program-xyz" in outcome.stderr_tail
        assert outcome.timed_out is False

    async def test_a_missing_recipe_program_is_also_a_result(self, tmp_path: Path) -> None:
        executor = ProcessExecutor(launcher=RecordingLauncher())
        missing = RecipeSpec(id="ghost", argv=("definitely-not-a-real-program-xyz",))
        outcome = await executor.run_recipe(missing, tmp_path)
        assert outcome.exit_code == 127

    async def test_scratch_dir_is_exported_as_tmpdir(self, tmp_path: Path) -> None:
        executor = ProcessExecutor(launcher=RecordingLauncher())
        outcome = await executor.run_exec(
            exec_spec(tmp_path, "import os; print(os.environ['TMPDIR'])")
        )
        assert "scratch" in outcome.stdout_tail
