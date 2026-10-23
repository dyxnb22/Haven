"""ADR 0037 后台任务与受控进程池单元测试。"""

import asyncio
import sys
from pathlib import Path

import pytest

from haven.application.background_tasks import TaskManager
from haven.domain.enums import PermissionMode, PolicyDecision
from haven.domain.policy import ToolFacts, evaluate_policy


@pytest.mark.asyncio
async def test_task_manager_lifecycle(tmp_path: Path) -> None:
    mgr = TaskManager()

    # 启动一个简短后台任务
    cmd = (
        sys.executable,
        "-c",
        (
            "import time, sys; print('hello stdout'); "
            "sys.stderr.write('hello stderr\\n'); time.sleep(0.3); print('done')"
        ),
    )
    task = await mgr.start(argv=cmd, cwd=tmp_path, description="test task")
    assert task.task_id == "task_1"
    assert task.is_running

    # 立即查看状态
    st = mgr.status("task_1")
    assert st["status"] in ("running", "exited")
    assert st["task_id"] == "task_1"

    # 等待执行完毕
    await asyncio.sleep(0.5)
    st_after = mgr.status("task_1")
    assert st_after["status"] == "exited"
    assert st_after["exit_code"] == 0

    # 查看日志
    logs = mgr.logs("task_1")
    assert any("hello stdout" in line for line in logs["stdout"])
    assert any("hello stderr" in line for line in logs["stderr"])
    assert any("done" in line for line in logs["stdout"])


@pytest.mark.asyncio
async def test_task_manager_stop(tmp_path: Path) -> None:
    mgr = TaskManager()
    cmd = (sys.executable, "-c", "import time; time.sleep(30)")
    task = await mgr.start(argv=cmd, cwd=tmp_path, description="sleeper")
    assert task.is_running

    res = await mgr.stop(task.task_id)
    assert res["status"] == "stopped"
    assert not task.is_running


@pytest.mark.asyncio
async def test_task_manager_close_all(tmp_path: Path) -> None:
    mgr = TaskManager()
    cmd = (sys.executable, "-c", "import time; time.sleep(30)")
    t1 = await mgr.start(argv=cmd, cwd=tmp_path)
    t2 = await mgr.start(argv=cmd, cwd=tmp_path)
    assert t1.is_running
    assert t2.is_running

    await mgr.close_all()
    await asyncio.sleep(0.1)
    assert not t1.is_running
    assert not t2.is_running


def test_task_policy_evaluation() -> None:
    # task.start 在有沙箱时策略为 ASK (EXEC_TOOLS)
    facts_start = ToolFacts(
        tool_name="task.start",
        within_workspace=True,
        sandbox_available=True,
    )
    outcome = evaluate_policy(PermissionMode.INTERACTIVE, facts_start)
    assert outcome.decision == PolicyDecision.ASK

    # task.start 无沙箱时硬拒绝
    facts_no_sandbox = ToolFacts(
        tool_name="task.start",
        within_workspace=True,
        sandbox_available=False,
    )
    assert (
        evaluate_policy(PermissionMode.INTERACTIVE, facts_no_sandbox).decision
        == PolicyDecision.DENY
    )

    # task.status, task.logs 为只读工具，自动 ALLOW
    for tool in ("task.status", "task.logs"):
        facts = ToolFacts(tool_name=tool)
        assert evaluate_policy(PermissionMode.INTERACTIVE, facts).decision == PolicyDecision.ALLOW

    # task.stop 为状态工具，自动 ALLOW
    facts_stop = ToolFacts(tool_name="task.stop")
    assert evaluate_policy(PermissionMode.INTERACTIVE, facts_stop).decision == PolicyDecision.ALLOW
