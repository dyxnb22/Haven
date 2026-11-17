"""单元测试：单只读子 Agent 委托执行与层级预算（ADR / 规划书 Phase P5）。"""

from __future__ import annotations

from pathlib import Path

from haven.adapters.memory_session import MemorySessionStore
from haven.application.emitter import EventEmitter, EventEnvelope
from haven.application.state import RunContext
from haven.application.tool_readers import ReadToolExecutor
from haven.contracts.events import ApplicationEvent
from haven.contracts.model import ToolCallProposal
from haven.contracts.tools import AgentDelegateArgs
from haven.domain.budget import Budget, BudgetUsage
from haven.domain.enums import PermissionMode, RunStatus, ToolStatus
from haven.domain.ids import RunId


class MemoryWorkspace:
    """用于测试的简易工作区桩。"""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path

    def path_facts(self, rel_path: str):
        p = self.root / rel_path
        exists = p.exists()
        is_f = p.is_file() if exists else False
        return type(
            "Facts",
            (),
            {
                "exists": exists,
                "is_file": is_f,
                "within_workspace": True,
                "is_protected": False,
            },
        )()

    async def read_file(self, rel_path: str, start_line: int, max_lines: int):
        p = self.root / rel_path
        text = p.read_text(encoding="utf-8")
        lines = text.splitlines()
        selected = lines[start_line - 1 : start_line - 1 + max_lines]
        return type(
            "ReadResult",
            (),
            {
                "content": "\n".join(selected),
                "total_lines": len(lines),
                "digest": "sha256:1234567890abcdef",
            },
        )()

    async def run_diff(self):
        diff_str = "--- a/file.py\n+++ b/file.py\n@@ -1 +1 @@\n-old\n+new"
        return type("DiffResult", (), {"diff": diff_str})()


class MemorySink:
    def __init__(self) -> None:
        self.events: list[ApplicationEvent] = []

    async def emit(self, envelope: EventEnvelope) -> None:
        self.events.append(envelope.event)


async def test_subagent_without_model_runtime_reports_unavailable(tmp_path: Path) -> None:
    sink = MemorySink()
    executor = ReadToolExecutor(
        MemoryWorkspace(tmp_path), EventEmitter(MemorySessionStore(), [sink])
    )
    ctx = RunContext(
        run_id=RunId("parent"),
        goal="Investigate code",
        mode=PermissionMode.READ_ONLY,
        budget=Budget(),
    )
    call = ToolCallProposal(call_id="delegate", tool_name="agent.delegate", arguments_json="{}")
    result = await executor.execute_agent_delegate(
        ctx, call, AgentDelegateArgs(role="scout", task="Find the bug"), "", None
    )
    assert result.result.status is ToolStatus.ERROR
    assert "unavailable" in result.result.message
    assert not sink.events


async def test_subagent_depth_invariant_rejection(tmp_path: Path) -> None:
    ws = MemoryWorkspace(tmp_path)
    sink = MemorySink()
    emitter = EventEmitter(MemorySessionStore(), [sink])
    executor = ReadToolExecutor(ws, emitter)  # type: ignore[arg-type]

    # 已是子运行（parent_run_id 非空）
    ctx = RunContext(
        run_id=RunId("run-child-1"),
        goal="Nested subagent",
        mode=PermissionMode.INTERACTIVE,
        parent_run_id="run-parent-0",
        budget=Budget(max_steps=10, max_tool_calls=15),
        status=RunStatus.CREATED,
    )
    call = ToolCallProposal(call_id="call-nested", tool_name="agent.delegate", arguments_json="{}")
    args = AgentDelegateArgs(role="scout", task="Attempt nested delegation")

    result = await executor.execute_agent_delegate(ctx, call, args, "ticket", None)
    assert result.result.status is ToolStatus.ERROR
    assert "depth limited to 1" in result.result.message


async def test_subagent_budget_exhaustion_rejection(tmp_path: Path) -> None:
    ws = MemoryWorkspace(tmp_path)
    sink = MemorySink()
    emitter = EventEmitter(MemorySessionStore(), [sink])
    executor = ReadToolExecutor(ws, emitter)  # type: ignore[arg-type]

    # 预算已消耗殆尽
    ctx = RunContext(
        run_id=RunId("run-parent-3"),
        goal="Exhausted budget",
        mode=PermissionMode.INTERACTIVE,
        budget=Budget(max_steps=5, max_tool_calls=5),
        status=RunStatus.CREATED,
        usage=BudgetUsage(steps=5, tool_calls=5),
    )
    call = ToolCallProposal(call_id="call-budget", tool_name="agent.delegate", arguments_json="{}")
    args = AgentDelegateArgs(role="scout", task="Task with no budget")

    result = await executor.execute_agent_delegate(ctx, call, args, "ticket", None)
    assert result.result.status is ToolStatus.ERROR
    assert "Insufficient parent budget" in result.result.message
