"""真实管道/PTY 交互，以及输入权限经唯一流水线检查。"""

import asyncio
import os
import sys
from pathlib import Path

import pytest

from haven.application.background_tasks import TaskManager
from haven.contracts.events import ApprovalRequested, PolicyDecided
from haven.domain.enums import AgentMode, ApprovalMode, PermissionMode, RunStatus
from tests.integration.harness import Harness, finish, make_repo, text, tool


async def wait_output(manager: TaskManager, task_id: str, expected: str) -> None:
    async with asyncio.timeout(4):
        while expected not in "\n".join(manager.logs(task_id)["stdout"]):
            await asyncio.sleep(0.01)


@pytest.mark.parametrize(
    "terminal",
    [False, pytest.param(True, marks=pytest.mark.skipif(os.name != "posix", reason="POSIX PTY"))],
)
async def test_interactive_prompt_input_eof_and_scrubbed_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, terminal: bool
) -> None:
    monkeypatch.setenv("HAVEN_TEST_PRIVATE_TOKEN", "not-for-children")
    manager = TaskManager()
    script = (
        "import sys,os; "
        "print('tty='+str(sys.stdin.isatty()),flush=True); "
        "print('private='+str('HAVEN_TEST_PRIVATE_TOKEN' in os.environ),flush=True); "
        "name=input('name? '); print('hello '+name,flush=True); "
        "print('eof='+str(sys.stdin.read()==''),flush=True)"
    )
    try:
        task = await manager.start(
            argv=(sys.executable, "-c", script), cwd=tmp_path, terminal=terminal
        )
        await wait_output(manager, task.task_id, "name? ")
        output = "\n".join(manager.logs(task.task_id)["stdout"])
        assert f"tty={terminal}" in output
        assert "private=False" in output
        sent = await manager.write_input(task.task_id, "小明\n")
        assert sent["bytes_sent"] == len("小明\n".encode())
        await wait_output(manager, task.task_id, "hello 小明")
        await manager.write_input(task.task_id, "", end_of_input=True)
        await wait_output(manager, task.task_id, "eof=True")
        assert task.process is not None
        await asyncio.wait_for(task.process.wait(), 3)
        assert task.process.returncode == 0
        with pytest.raises(ValueError, match="closed|exited"):
            await manager.write_input(task.task_id, "late")
        assert (await manager.write_input("unknown", "x"))["status"] == "not_found"
    finally:
        await manager.close_all()
    assert task.master_fd is None


async def test_long_unterminated_output_is_visible_and_bounded(tmp_path: Path) -> None:
    manager = TaskManager()
    try:
        task = await manager.start(
            argv=(
                sys.executable,
                "-c",
                "import time; print('x'*250000,end='',flush=True); time.sleep(30)",
            ),
            cwd=tmp_path,
        )
        await wait_output(manager, task.task_id, "x" * 8000)
        assert all(len(line) <= 8192 for line in manager.logs(task.task_id)["stdout"])
    finally:
        await manager.close_all()


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups")
async def test_stop_cleans_descendants(tmp_path: Path) -> None:
    marker = tmp_path / "orphan-wrote.txt"
    child = (
        "import time,pathlib; time.sleep(0.6); pathlib.Path('orphan-wrote.txt').write_text('bad')"
    )
    parent = (
        f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{child!r}]); "
        "print('ready',flush=True); time.sleep(30)"
    )
    manager = TaskManager()
    try:
        task = await manager.start(argv=(sys.executable, "-c", parent), cwd=tmp_path, terminal=True)
        await wait_output(manager, task.task_id, "ready")
        assert (await manager.stop(task.task_id))["status"] == "stopped"
        await asyncio.sleep(0.7)
        assert not marker.exists()
        assert task.master_fd is None
    finally:
        await manager.close_all()


@pytest.mark.parametrize("approval_mode", [ApprovalMode.ASK, ApprovalMode.SMART, ApprovalMode.FULL])
async def test_task_input_exact_approval_and_mode_enforcement(
    tmp_path: Path, approval_mode: ApprovalMode
) -> None:
    harness = Harness(
        make_repo(tmp_path),
        [
            [
                tool("start", "task.start", argv=[sys.executable, "-c", "input(); input()"]),
                finish("tool_calls"),
            ],
            [tool("input", "task.input", task_id="task_1", data="hello\n"), finish("tool_calls")],
            [text("done"), finish()],
        ],
        approval_mode=approval_mode,
    )
    outcome = await harness.service.run("test interactive input")
    assert outcome.status is RunStatus.SUCCEEDED
    requests = [
        e
        for e in harness.sink.events_of("approval.requested")
        if isinstance(e, ApprovalRequested) and "send input" in e.summary
    ]
    if approval_mode is ApprovalMode.FULL:
        assert not requests
    else:
        assert len(requests) == 1
        assert '"hello\\n"' in requests[0].preview
        assert requests[0].risk == "high"
    denied = Harness(
        make_repo(tmp_path / "denied"),
        [
            [tool("input", "task.input", task_id="task_1", data="hello\n"), finish("tool_calls")],
            [text("done"), finish()],
        ],
        mode=PermissionMode.READ_ONLY,
    )
    await denied.service.run("read only task")
    assert any(
        isinstance(e, PolicyDecided) and e.reason_code == "read_only_mode"
        for e in denied.sink.events_of("policy.decided")
    )
    planner = Harness(
        make_repo(tmp_path / "plan"),
        [
            [tool("input", "task.input", task_id="task_1", data="hello\n"), finish("tool_calls")],
            [text("no changes"), finish()],
        ],
    )
    await planner.service.run("plan this task", agent_mode=AgentMode.PLAN)
    assert any(
        isinstance(e, PolicyDecided) and e.reason_code == "plan_mode"
        for e in planner.sink.events_of("policy.decided")
    )
