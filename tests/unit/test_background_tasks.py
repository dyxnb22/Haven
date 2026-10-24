"""ADR 0037 后台任务与受控进程池单元测试。"""

import asyncio
import sys
from pathlib import Path

import pytest

from haven.adapters.memory_session import MemorySessionStore
from haven.adapters.process_executor import ProcessExecutor
from haven.adapters.workspace_fs import FsWorkspace
from haven.application.background_tasks import TaskManager
from haven.application.emitter import EventEmitter
from haven.application.state import RunContext
from haven.application.tool_processes import ProcessToolExecutor
from haven.contracts.model import ToolCallProposal
from haven.contracts.tools import (
    RepoExecArgs,
    TaskLogsArgs,
    TaskStartArgs,
    TaskStatusArgs,
    TaskStopArgs,
)
from haven.domain.budget import Budget
from haven.domain.enums import PermissionMode, PolicyDecision, ToolErrorCode, ToolStatus
from haven.domain.ids import RunId
from haven.domain.policy import ToolFacts, evaluate_policy
from tests.integration.fakes import RecordingLauncher


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


@pytest.mark.asyncio
async def test_process_tool_executor_task_handlers(tmp_path: Path) -> None:
    ws = FsWorkspace(tmp_path)
    store = MemorySessionStore()
    emitter = EventEmitter(store, ())
    launcher = RecordingLauncher()
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    executor = ProcessToolExecutor(
        workspace=ws,
        executor=ProcessExecutor(),
        store=store,
        emitter=emitter,
        recipes={},
        launcher=launcher,
        scratch_dir=scratch,
    )

    ctx = RunContext(
        run_id=RunId("run-tasks"),
        goal="background tasks test",
        mode=PermissionMode.INTERACTIVE,
        budget=Budget(),
    )
    preview = None

    # 1. 成功启动后台任务
    start_call = ToolCallProposal(call_id="c_start", tool_name="task.start", arguments_json="{}")
    start_args = TaskStartArgs(
        argv=(sys.executable, "-c", "import time; time.sleep(0.1); print('task done')"),
        cwd=".",
        description="sample background task",
    )
    exec_res = await executor.execute_task_start(
        ctx, start_call, start_args, "ticket_start", preview
    )
    assert exec_res.result.status is ToolStatus.OK
    assert exec_res.result.payload.get("status") == "running"
    task_id = str(exec_res.result.payload["task_id"])

    # 2. 查询存在与不存在的任务状态
    status_call = ToolCallProposal(call_id="c_status", tool_name="task.status", arguments_json="{}")
    status_res = await executor.execute_task_status(
        ctx, status_call, TaskStatusArgs(task_id=task_id), "t_status", preview
    )
    assert status_res.result.status is ToolStatus.OK
    assert status_res.result.payload.get("task_id") == task_id

    missing_status = await executor.execute_task_status(
        ctx, status_call, TaskStatusArgs(task_id="not_exist"), "t_status", preview
    )
    assert missing_status.result.status is ToolStatus.ERROR
    assert missing_status.result.error_code == ToolErrorCode.NOT_FOUND

    # 3. 查询存在与不存在的任务日志
    logs_call = ToolCallProposal(call_id="c_logs", tool_name="task.logs", arguments_json="{}")
    logs_res = await executor.execute_task_logs(
        ctx, logs_call, TaskLogsArgs(task_id=task_id, tail_lines=20), "t_logs", preview
    )
    assert logs_res.result.status is ToolStatus.OK

    missing_logs = await executor.execute_task_logs(
        ctx, logs_call, TaskLogsArgs(task_id="not_exist"), "t_logs", preview
    )
    assert missing_logs.result.status is ToolStatus.ERROR
    assert missing_logs.result.error_code == ToolErrorCode.NOT_FOUND

    # 4. 停止存在与不存在的任务
    stop_call = ToolCallProposal(call_id="c_stop", tool_name="task.stop", arguments_json="{}")
    stop_res = await executor.execute_task_stop(
        ctx, stop_call, TaskStopArgs(task_id=task_id), "t_stop", preview
    )
    assert stop_res.result.status is ToolStatus.OK
    assert stop_res.result.payload.get("status") == "stopped"

    missing_stop = await executor.execute_task_stop(
        ctx, stop_call, TaskStopArgs(task_id="not_exist"), "t_stop", preview
    )
    assert missing_stop.result.status is ToolStatus.ERROR
    assert missing_stop.result.error_code == ToolErrorCode.NOT_FOUND

    await executor.close_tasks()
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_process_tool_executor_no_launcher_edges(tmp_path: Path) -> None:
    ws = FsWorkspace(tmp_path)
    store = MemorySessionStore()
    emitter = EventEmitter(store, ())
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    executor = ProcessToolExecutor(
        workspace=ws,
        executor=ProcessExecutor(),
        store=store,
        emitter=emitter,
        recipes={},
        launcher=None,
        scratch_dir=scratch,
    )

    assert executor.describe_sandbox() == "sandbox: unavailable"

    ctx = RunContext(
        run_id=RunId("run-no-sandbox"),
        goal="no sandbox test",
        mode=PermissionMode.INTERACTIVE,
        budget=Budget(),
    )
    preview = None

    # task.start 无沙箱时拒绝
    start_call = ToolCallProposal(call_id="c1", tool_name="task.start", arguments_json="{}")
    start_args = TaskStartArgs(argv=("echo", "1"), cwd=".")
    start_res = await executor.execute_task_start(ctx, start_call, start_args, "t1", preview)
    assert start_res.result.status is ToolStatus.ERROR
    assert start_res.result.error_code == ToolErrorCode.DENIED

    # repo.exec 无沙箱时抛出 RuntimeError
    exec_call = ToolCallProposal(call_id="c2", tool_name="repo.exec", arguments_json="{}")
    exec_args = RepoExecArgs(argv=("echo", "1"))
    with pytest.raises(RuntimeError, match="refusing to exec without a sandbox backend"):
        await executor.execute_exec(ctx, exec_call, exec_args, "t2", preview)
