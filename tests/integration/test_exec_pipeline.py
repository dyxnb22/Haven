"""repo.exec 经过真实通道：事实、策略、审批、执行。

这里使用记录型替身启动器，因此这些断言在任何平台都成立；操作系统确实限制进程
则在 tests/security/test_sandbox_enforcement.py 中单独断言。
"""

import sys
from pathlib import Path

from haven.application.approvals import AutoApprover
from haven.contracts.events import ApprovalRequested, PolicyDecided, ToolCompleted
from haven.domain.enums import RunStatus, StopReason
from tests.integration.harness import Harness, finish, make_repo, text, tool


def completed(h: Harness) -> list[ToolCompleted]:
    return [e for e in h.sink.events_of("tool.completed") if isinstance(e, ToolCompleted)]


def denials(h: Harness) -> list[str]:
    return [
        e.reason_code
        for e in h.sink.events_of("policy.decided")
        if isinstance(e, PolicyDecided) and e.decision == "deny"
    ]


class TestApprovalFlow:
    async def test_safe_read_command_runs_without_approval(self, tmp_path: Path) -> None:
        """被分类为只读的命令是唯一可以跳过提示的 exec。"""
        turns = [
            [tool("c1", "repo.exec", argv=["ls"]), finish("tool_calls")],
            [text("Listed the directory."), finish()],
        ]
        h = Harness(make_repo(tmp_path), turns)
        outcome = await h.service.run("List the repository root")

        assert outcome.status is RunStatus.SUCCEEDED
        assert completed(h)[0].status == "ok"
        assert h.sink.events_of("approval.requested") == []

    async def test_a_read_reaching_outside_the_workspace_asks_first(self, tmp_path: Path) -> None:
        """对工作区文件保持静默的同一个 `cat`，指向工作区外时必须先提示——否则未
        审批文件的内容会进入对话记录，进而到达模型提供商（在 Linux 上，这种形式
        会读取 /proc/<parent>/environ，绕过子进程已清理的环境）。"""
        turns = [
            [tool("c1", "repo.exec", argv=["cat", "/etc/hosts"]), finish("tool_calls")],
            [text("Read it."), finish()],
        ]
        h = Harness(make_repo(tmp_path), turns, approver=AutoApprover("reject_all"))
        await h.service.run("Show me the hosts file")

        assert h.sink.events_of("approval.requested"), "an escaping read must be approved"
        assert completed(h)[0].error_code == "approval_rejected"
        assert "execution.started" not in h.sink.kinds()

    async def test_other_command_requests_approval_before_running(self, tmp_path: Path) -> None:
        turns = [
            [tool("c1", "repo.exec", argv=[sys.executable, "-V"]), finish("tool_calls")],
            [text("Checked the interpreter version."), finish()],
        ]
        h = Harness(make_repo(tmp_path), turns)
        await h.service.run("Print the Python version")

        kinds = h.sink.kinds()
        assert kinds.index("approval.requested") < kinds.index("execution.started")

    async def test_rejected_approval_never_starts_a_process(self, tmp_path: Path) -> None:
        turns = [
            [tool("c1", "repo.exec", argv=[sys.executable, "-V"]), finish("tool_calls")],
            [text("The user declined."), finish()],
        ]
        h = Harness(make_repo(tmp_path), turns, approver=AutoApprover("reject_all"))
        await h.service.run("Print the Python version")

        assert completed(h)[0].error_code == "approval_rejected"
        assert "execution.started" not in h.sink.kinds()

    async def test_preview_shows_the_command_and_the_sandbox(self, tmp_path: Path) -> None:
        """用户看到的内容就是审批摘要绑定的内容。"""
        turns = [
            [tool("c1", "repo.exec", argv=["make", "build"]), finish("tool_calls")],
            [text("Built."), finish()],
        ]
        h = Harness(make_repo(tmp_path), turns)
        await h.service.run("Build the project")

        requested = h.sink.events_of("approval.requested")[0]
        assert isinstance(requested, ApprovalRequested)
        assert "$ make build" in requested.preview
        assert "sandbox:" in requested.preview

    async def test_shell_passthrough_preview_carries_a_warning(self, tmp_path: Path) -> None:
        turns = [
            [tool("c1", "repo.exec", argv=["bash", "-c", "echo hi"]), finish("tool_calls")],
            [text("Ran it."), finish()],
        ]
        h = Harness(make_repo(tmp_path), turns)
        await h.service.run("Echo something through a shell")

        requested = h.sink.events_of("approval.requested")[0]
        assert isinstance(requested, ApprovalRequested)
        assert "WARNING" in requested.preview
        assert requested.risk == "high"


class TestResults:
    async def test_nonzero_exit_is_a_structured_result(self, tmp_path: Path) -> None:
        turns = [
            [
                tool("c1", "repo.exec", argv=[sys.executable, "-c", "raise SystemExit(2)"]),
                finish("tool_calls"),
            ],
            [text("The command exited 2."), finish()],
        ]
        h = Harness(make_repo(tmp_path), turns)
        outcome = await h.service.run("Run a failing command")

        assert outcome.status is RunStatus.SUCCEEDED
        result = completed(h)[0]
        assert result.status == "ok"
        assert result.error_code == ""

    async def test_timeout_maps_to_the_timeout_error_code(self, tmp_path: Path) -> None:
        turns = [
            [
                tool(
                    "c1",
                    "repo.exec",
                    argv=[sys.executable, "-c", "import time; time.sleep(30)"],
                    timeout_seconds=1,
                ),
                finish("tool_calls"),
            ],
            [text("It timed out."), finish()],
        ]
        h = Harness(make_repo(tmp_path), turns)
        await h.service.run("Run something slow")

        assert completed(h)[0].error_code == "timeout"

    async def test_exec_cannot_satisfy_the_evidence_gate(self, tmp_path: Path) -> None:
        """核心主张：绿色的 exec 不是验证。"""
        turns = [
            [tool("c1", "repo.read", path="src/calc.py"), finish("tool_calls")],
            [
                tool(
                    "c2",
                    "repo.edit",
                    path="src/calc.py",
                    old_string="return a - b  # BUG: should be +",
                    new_string="return a + b",
                ),
                finish("tool_calls"),
            ],
            [tool("c3", "repo.exec", argv=["ls"]), finish("tool_calls")],
            [text("Fixed and verified by running a command."), finish()],
        ]
        h = Harness(make_repo(tmp_path), turns, repeat_last=True)
        outcome = await h.service.run("Fix the add() bug")

        assert outcome.stop_reason is StopReason.EVIDENCE_MISSING


class TestNoBackend:
    async def test_denied_when_no_launcher_is_configured(self, tmp_path: Path) -> None:
        """失败即关闭：没有沙箱就没有 exec，而不是执行不受限制的 exec。"""
        turns = [
            [tool("c1", "repo.exec", argv=["ls"]), finish("tool_calls")],
            [text("Exec is unavailable here."), finish()],
        ]
        h = Harness(make_repo(tmp_path), turns, launcher=None)
        await h.service.run("List the repository root")

        assert "sandbox_unavailable" in denials(h)
        assert completed(h)[0].error_code == "denied"
        assert "execution.started" not in h.sink.kinds()
