"""一次模型轮次中的多个工具调用。

适配器组装数组，循环遍历数组，因此按设计它本来就应当工作——但“Haven 处理并行
工具调用”是一项主张，未经测试的主张会悄悄回归。
"""

from pathlib import Path

from haven.application.approvals import AutoApprover
from haven.contracts.events import ExecutionStarted, ToolCompleted
from haven.domain.enums import RunStatus
from tests.integration.harness import Harness, finish, make_repo, text, tool


def completed(h: Harness) -> list[ToolCompleted]:
    return [e for e in h.sink.events_of("tool.completed") if isinstance(e, ToolCompleted)]


class TestSeveralCallsInOneTurn:
    async def test_all_calls_run_in_the_order_proposed(self, tmp_path: Path) -> None:
        turns = [
            [
                tool("c1", "repo.read", path="src/calc.py"),
                tool("c2", "repo.list", path="."),
                tool("c3", "repo.search", pattern="BUG", path="src"),
                finish("tool_calls"),
            ],
            [text("Read, listed, and searched in one turn."), finish()],
        ]
        h = Harness(make_repo(tmp_path), turns)
        outcome = await h.service.run("Inspect the repository")

        assert outcome.status is RunStatus.SUCCEEDED
        assert [event.tool_name for event in completed(h)] == [
            "repo.read",
            "repo.list",
            "repo.search",
        ]

    async def test_each_call_is_charged_against_the_tool_budget(self, tmp_path: Path) -> None:
        turns = [
            [
                tool("c1", "repo.read", path="src/calc.py"),
                tool("c2", "repo.list", path="."),
                finish("tool_calls"),
            ],
            [text("Done."), finish()],
        ]
        h = Harness(make_repo(tmp_path), turns)
        outcome = await h.service.run("Inspect the repository")

        assert outcome.tool_calls == 2

    async def test_each_side_effecting_call_gets_its_own_approval(self, tmp_path: Path) -> None:
        """一次审批绝不能授权第二个操作。"""
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
                tool("c3", "repo.check", recipe_id="always-pass"),
                finish("tool_calls"),
            ],
            [tool("c4", "repo.diff"), finish("tool_calls")],
            [text("Edited and checked."), finish()],
        ]
        h = Harness(make_repo(tmp_path), turns)
        await h.service.run("Fix add() and verify")

        approvals = h.sink.events_of("approval.requested")
        assert len(approvals) == 2

    async def test_a_rejected_first_call_does_not_authorize_the_second(
        self, tmp_path: Path
    ) -> None:
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
                tool("c3", "repo.check", recipe_id="always-pass"),
                finish("tool_calls"),
            ],
            [text("Both were declined."), finish()],
        ]
        h = Harness(make_repo(tmp_path), turns, approver=AutoApprover("reject_all"))
        await h.service.run("Fix add() and verify")

        rejected = [e for e in completed(h) if e.error_code == "approval_rejected"]
        assert len(rejected) == 2
        # 自动允许的 read 会执行；两个被拒绝的操作都不能执行。
        executed = {
            event.tool_name
            for event in h.sink.events_of("execution.started")
            if isinstance(event, ExecutionStarted)
        }
        assert executed == {"repo.read"}

    async def test_a_failing_call_does_not_abort_its_siblings(self, tmp_path: Path) -> None:
        turns = [
            [
                tool("c1", "repo.read", path="does/not/exist.py"),
                tool("c2", "repo.read", path="src/calc.py"),
                finish("tool_calls"),
            ],
            [text("One failed, one succeeded."), finish()],
        ]
        h = Harness(make_repo(tmp_path), turns)
        await h.service.run("Read two files")

        results = completed(h)
        assert results[0].error_code == "not_found"
        assert results[1].status == "ok"
