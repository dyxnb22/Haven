"""执行器测试：退出码、超时、输出爆炸和取消。"""

import asyncio
import sys
from pathlib import Path

import pytest

from haven.adapters.process_executor import (
    OUTPUT_CAP_BYTES,
    SAVED_OUTPUT_CAP_BYTES,
    ProcessExecutor,
)
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


async def test_saved_output_keeps_early_errors_outside_the_observation_tail(tmp_path: Path) -> None:
    outcome = await ProcessExecutor().run_recipe(
        recipe(f"print('early error'); print('x' * {OUTPUT_CAP_BYTES * 2}); print('last line')"),
        tmp_path,
    )
    assert outcome.truncated and "early error" not in outcome.stdout_tail
    assert outcome.output is not None and not outcome.output.truncated
    assert outcome.output.stdout.startswith("early error\n")
    assert outcome.output.stdout.endswith("last line\n")
    assert len(outcome.stdout_tail.encode()) <= OUTPUT_CAP_BYTES


async def test_saved_output_is_bounded_and_marks_the_missing_middle(tmp_path: Path) -> None:
    outcome = await ProcessExecutor().run_recipe(
        recipe(
            f"import sys; print('BEGIN' + 'x' * {SAVED_OUTPUT_CAP_BYTES * 2} + 'END'); "
            f"sys.stderr.write('ERR_BEGIN' + 'y' * {SAVED_OUTPUT_CAP_BYTES * 2} + 'ERR_END')"
        ),
        tmp_path,
    )
    assert outcome.output is not None and outcome.output.truncated
    for value, start, end in (
        (outcome.output.stdout, "BEGIN", "END\n"),
        (outcome.output.stderr, "ERR_BEGIN", "ERR_END"),
    ):
        assert value.startswith(start) and value.endswith(end)
        assert "output bytes omitted" in value
        assert len(value.encode()) <= SAVED_OUTPUT_CAP_BYTES + 100


async def test_timeout_preserves_output_emitted_before_termination(tmp_path: Path) -> None:
    outcome = await ProcessExecutor().run_recipe(
        recipe("import time; print('before timeout', flush=True); time.sleep(30)", timeout=0.2),
        tmp_path,
    )
    assert outcome.timed_out and outcome.exit_code == 124
    assert outcome.output is not None and "before timeout" in outcome.output.stdout
    assert "before timeout" in outcome.stdout_tail


async def test_pipeline_saved_output_includes_each_stderr_and_timeout_prefix(
    tmp_path: Path,
) -> None:
    executor = ProcessExecutor(launcher=RecordingLauncher())
    first = "import sys; print('input'); print('first error', file=sys.stderr)"
    last = "import sys; print(sys.stdin.read()); print('last error', file=sys.stderr)"
    spec = exec_spec(tmp_path, first)
    from dataclasses import replace

    spec = replace(spec, argv=(PY, "-c", first, "|", PY, "-c", last))
    outcome = await executor.run_exec(spec)
    assert outcome.output is not None
    assert "input" in outcome.output.stdout
    assert "first error" in outcome.output.stderr and "last error" in outcome.output.stderr
    assert not outcome.output.truncated
    first = "import time; print('before pipeline timeout', flush=True); time.sleep(30)"
    spec = replace(spec, argv=(PY, "-c", first, "|", "cat"), timeout_seconds=0.3)
    outcome = await executor.run_exec(spec)
    assert outcome.timed_out and outcome.output is not None
    assert "before pipeline timeout" in outcome.output.stdout


async def test_successful_command(tmp_path: Path) -> None:
    outcome = await ProcessExecutor().run_recipe(recipe("print('ok')"), tmp_path)
    assert outcome.exit_code == 0
    assert "ok" in outcome.stdout_tail
    assert not outcome.timed_out


async def test_run_recipe_with_extra_args(tmp_path: Path) -> None:
    outcome = await ProcessExecutor().run_recipe(
        recipe("import sys; print(sys.argv[1:])"), tmp_path, extra_args=("-k", "foo")
    )
    assert outcome.exit_code == 0
    assert "['-k', 'foo']" in outcome.stdout_tail


async def test_failing_command(tmp_path: Path) -> None:
    outcome = await ProcessExecutor().run_recipe(recipe("import sys; sys.exit(3)"), tmp_path)
    assert outcome.exit_code == 3


async def test_timeout_terminates_process(tmp_path: Path) -> None:
    outcome = await ProcessExecutor().run_recipe(
        recipe("import time; time.sleep(60)", timeout=0.5), tmp_path
    )
    assert outcome.timed_out
    assert outcome.exit_code == 124

    marker = tmp_path / "grandchild-survived"
    child = f"import time; time.sleep(0.5); open({str(marker)!r}, 'w').write('bad')"
    parent = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
        "time.sleep(60)"
    )
    outcome = await ProcessExecutor().run_recipe(recipe(parent, timeout=0.2), tmp_path)
    assert outcome.timed_out
    await asyncio.sleep(0.6)
    assert not marker.exists(), "a timed-out command left a live grandchild"

    background_marker = tmp_path / "background-survived"
    background_child = (
        f"import time; time.sleep(0.5); open({str(background_marker)!r}, chr(119)).write(chr(98))"
    )
    background = (
        "import subprocess, sys; "
        f"subprocess.Popen([sys.executable, '-c', "
        f"{background_child!r}], "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)"
    )
    outcome = await ProcessExecutor().run_recipe(recipe(background), tmp_path)
    assert outcome.exit_code == 0
    await asyncio.sleep(0.6)
    assert not background_marker.exists(), "a completed command left a live background child"

    inherited_marker = tmp_path / "inherited-pipe-child-survived"
    inherited_child = (
        f"import time; time.sleep(0.5); open({str(inherited_marker)!r}, 'w').write('bad')"
    )
    inherited = (
        f"import subprocess, sys; subprocess.Popen([sys.executable, '-c', {inherited_child!r}])"
    )
    outcome = await ProcessExecutor().run_recipe(recipe(inherited), tmp_path)
    assert outcome.exit_code == 0
    await asyncio.sleep(0.6)
    assert not inherited_marker.exists(), "an inherited pipe delayed descendant cleanup"


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
        """只有一个包装位置：检查与 exec 受到同样的限制。"""
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

    async def test_recipe_ignores_workspace_scratch_symlink(self, tmp_path: Path) -> None:
        """仓库不能用旧固定路径的符号链接扩大 recipe 的可写边界。"""
        outside = tmp_path.parent / f"{tmp_path.name}-outside"
        outside.mkdir()
        legacy_scratch = tmp_path / ".haven-scratch"
        legacy_scratch.symlink_to(outside, target_is_directory=True)
        launcher = RecordingLauncher()

        outcome = await ProcessExecutor(launcher=launcher).run_recipe(
            recipe(
                "import os; from pathlib import Path; "
                "p = Path(os.environ['TMPDIR']); (p / 'marker').write_text('x'); print(p)"
            ),
            tmp_path,
        )

        used_scratch = launcher.calls[0][1].scratch_dir
        assert outcome.exit_code == 0
        assert used_scratch != legacy_scratch
        assert not used_scratch.exists(), "standalone recipe scratch must be reclaimed"
        assert legacy_scratch.is_symlink()
        assert not (outside / "marker").exists()

    async def test_a_missing_program_is_a_result_not_an_exception(self, tmp_path: Path) -> None:
        """在 Linux 上发现：系统没有 `make` 时，create_subprocess_exec 会抛出
        FileNotFoundError；该异常会逃入代理循环，违反工具调用始终返回结构化结果的
        不变量。"""
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

    async def test_run_exec_pipeline(self, tmp_path: Path) -> None:
        executor = ProcessExecutor()
        code1 = "import sys; print('apple\\nbanana\\ncherry')"
        code2 = (
            "import sys; lines = [l.strip() for l in sys.stdin if 'an' in l];"
            " print('\\n'.join(lines))"
        )
        spec = ExecSpec(
            argv=(PY, "-c", code1, "|", PY, "-c", code2),
            cwd=tmp_path,
            timeout_seconds=10.0,
            sandbox=SandboxSpec(
                workspace_root=tmp_path, scratch_dir=tmp_path / "scratch", writable=True
            ),
        )
        outcome = await executor.run_exec(spec)
        assert outcome.exit_code == 0
        assert outcome.stdout_tail.strip() == "banana"
        assert outcome.timed_out is False

    async def test_run_exec_pipeline_early_exit(self, tmp_path: Path) -> None:
        executor = ProcessExecutor()
        code1 = "import sys; [print(f'line {i}') for i in range(10000)]"
        code2 = "import sys; [print(sys.stdin.readline().strip()) for _ in range(2)]"
        spec = ExecSpec(
            argv=(PY, "-c", code1, "|", PY, "-c", code2),
            cwd=tmp_path,
            timeout_seconds=10.0,
            sandbox=SandboxSpec(
                workspace_root=tmp_path, scratch_dir=tmp_path / "scratch", writable=True
            ),
        )
        outcome = await executor.run_exec(spec)
        assert outcome.exit_code == 0
        assert "line 0" in outcome.stdout_tail
        assert "line 1" in outcome.stdout_tail

    async def test_run_exec_pipeline_missing_command_is_result(self, tmp_path: Path) -> None:
        executor = ProcessExecutor()
        spec = ExecSpec(
            argv=(PY, "-c", "print('hi')", "|", "definitely-not-real-prog"),
            cwd=tmp_path,
            timeout_seconds=10.0,
            sandbox=SandboxSpec(
                workspace_root=tmp_path, scratch_dir=tmp_path / "scratch", writable=True
            ),
        )
        outcome = await executor.run_exec(spec)
        assert outcome.exit_code == 127
        assert outcome.timed_out is False
