"""repo.exec through the real channel: facts, policy, approval, execution.

The launcher is a recording fake here, so these assertions hold on any
platform; that the OS actually confines the process is asserted separately in
tests/security/test_sandbox_enforcement.py.
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
        """A classified read-only command is the one exec that skips the prompt."""
        turns = [
            [tool("c1", "repo.exec", argv=["ls"]), finish("tool_calls")],
            [text("Listed the directory."), finish()],
        ]
        h = Harness(make_repo(tmp_path), turns)
        outcome = await h.service.run("List the repository root")

        assert outcome.status is RunStatus.SUCCEEDED
        assert completed(h)[0].status == "ok"
        assert h.sink.events_of("approval.requested") == []

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
        """What the user reads is what the approval digest binds."""
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
        """The central claim: a green exec is not verification."""
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
        """Fail closed: no sandbox means no exec, not an unconfined exec."""
        turns = [
            [tool("c1", "repo.exec", argv=["ls"]), finish("tool_calls")],
            [text("Exec is unavailable here."), finish()],
        ]
        h = Harness(make_repo(tmp_path), turns, launcher=None)
        await h.service.run("List the repository root")

        assert "sandbox_unavailable" in denials(h)
        assert completed(h)[0].error_code == "denied"
        assert "execution.started" not in h.sink.kinds()
