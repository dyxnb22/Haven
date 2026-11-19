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
from haven.ports.sandbox import SandboxSpec
from tests.integration.fakes import RecordingLauncher


def _sandbox(root: Path) -> SandboxSpec:
    """一个最小可用的沙箱规格；`RecordingLauncher` 记录它但并不真正限制。"""
    return SandboxSpec(workspace_root=root, scratch_dir=root / "scratch", writable=False)


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
    task = await mgr.start(
        argv=cmd,
        cwd=tmp_path,
        description="test task",
        sandbox_spec=_sandbox(tmp_path),
        launcher=RecordingLauncher(),
    )
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
    task = await mgr.start(
        argv=cmd,
        cwd=tmp_path,
        description="sleeper",
        sandbox_spec=_sandbox(tmp_path),
        launcher=RecordingLauncher(),
    )
    assert task.is_running

    res = await mgr.stop(task.task_id)
    assert res["status"] == "stopped"
    assert not task.is_running


@pytest.mark.asyncio
async def test_task_manager_close_all(tmp_path: Path) -> None:
    mgr = TaskManager()
    cmd = (sys.executable, "-c", "import time; time.sleep(30)")
    launcher = RecordingLauncher()
    t1 = await mgr.start(argv=cmd, cwd=tmp_path, sandbox_spec=_sandbox(tmp_path), launcher=launcher)
    t2 = await mgr.start(argv=cmd, cwd=tmp_path, sandbox_spec=_sandbox(tmp_path), launcher=launcher)
    assert t1.is_running
    assert t2.is_running

    await mgr.close_all()
    await asyncio.sleep(0.1)
    assert not t1.is_running
    assert not t2.is_running


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["launcher", "spec"])
async def test_a_task_without_a_sandbox_is_refused_not_run_bare(
    tmp_path: Path, missing: str
) -> None:
    """缺沙箱必须拒绝启动，而不是光着把命令跑起来。

    `TaskManager.start` 曾经写成 `launcher.wrap(...) if (launcher and spec) else argv`：
    漏传任何一样，命令就整个脱离沙箱，而日志里看不出异常。调用方自己也预检，但真正
    创建进程的是这一层，边界该由它把守。
    """
    mgr = TaskManager()
    kwargs: dict[str, object] = {
        "argv": (sys.executable, "-c", "import time; time.sleep(30)"),
        "cwd": tmp_path,
    }
    if missing == "launcher":
        kwargs["sandbox_spec"] = _sandbox(tmp_path)
    else:
        kwargs["launcher"] = RecordingLauncher()
    with pytest.raises(ValueError, match="without a sandbox"):
        await mgr.start(**kwargs)  # type: ignore[arg-type]
    # 拒绝必须是先于进程创建的：一个都没起来。
    assert mgr.status("task_1")["status"] == "not_found"


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


class TestProtectedBaselineIsPerRun:
    """`_snapshot_before_process` 的基线必须按 run_id 归属。

    复用它能省掉一次对整个 `.git` 的全量重新哈希，但执行器若跨运行存活，用户在两次
    运行之间对 `.git` 做的修改就会被记成"这次调用篡改了受保护路径"——一条凭空出现
    的边界违规。换 run_id 就必须重新算基线。
    """

    @staticmethod
    def _executor(tmp_path: Path) -> ProcessToolExecutor:
        scratch = tmp_path / "scratch"
        scratch.mkdir(exist_ok=True)
        return ProcessToolExecutor(
            workspace=FsWorkspace(tmp_path),
            executor=ProcessExecutor(),
            store=MemorySessionStore(),
            emitter=EventEmitter(MemorySessionStore(), ()),
            recipes={},
            launcher=RecordingLauncher(),
            scratch_dir=scratch,
        )

    @staticmethod
    def _ctx(run_id: str) -> RunContext:
        return RunContext(
            run_id=RunId(run_id),
            goal="baseline scope",
            mode=PermissionMode.INTERACTIVE,
            budget=Budget(),
        )

    async def test_the_baseline_is_dropped_when_the_run_changes(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "config").write_text("[core]\n")
        executor = self._executor(tmp_path)

        first = await executor._snapshot_before_process(self._ctx("run-a"))
        assert first.protected_digests, "第一次调用没有基线，必须照常计算"

        # 模拟上一次调用结束：记住基线，然后用户自己改了 .git。
        executor._remember_protected_baseline(self._ctx("run-a"), first)
        (tmp_path / ".git" / "config").write_text("[core]\n\tborrowed = true\n")

        reused = await executor._snapshot_before_process(self._ctx("run-a"))
        assert reused.protected_digests == first.protected_digests, "同一次运行内应当复用"

        # 换成另一次运行：基线作废，必须重新计算，否则会把用户的改动算成篡改。
        fresh = await executor._snapshot_before_process(self._ctx("run-b"))
        assert fresh.protected_digests != first.protected_digests

    async def test_the_baseline_survives_repeated_calls_in_one_run(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "config").write_text("[core]\n")
        executor = self._executor(tmp_path)
        ctx = self._ctx("run-a")

        first = await executor._snapshot_before_process(ctx)
        executor._remember_protected_baseline(ctx, first)
        second = await executor._snapshot_before_process(ctx)
        assert second.protected_digests == first.protected_digests
