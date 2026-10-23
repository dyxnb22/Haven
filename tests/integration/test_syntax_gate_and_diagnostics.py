"""端到端集成测试：AST 语法预检门禁与结构化诊断提取（ADR 0040）。"""

from pathlib import Path

from haven.contracts.events import ToolCompleted
from haven.contracts.model import ModelEvent
from haven.domain.enums import RunStatus, StopReason, ToolErrorCode, ToolStatus
from tests.integration.harness import Harness, finish, make_repo, text, tool, usage


class TestSyntaxGateAndDiagnosticsJourney:
    async def test_syntax_error_rejected_before_commit_and_model_recovers(
        self, tmp_path: Path
    ) -> None:
        turns: list[list[ModelEvent]] = [
            # 1. 读文件
            [
                tool("c1", "repo.read", path="src/calc.py"),
                finish("tool_calls"),
            ],
            # 2. 模型提出带语法错误的编辑（未闭合括号）
            [
                tool(
                    "c2",
                    "repo.edit",
                    path="src/calc.py",
                    old_string="return a - b  # BUG: should be +",
                    new_string="return add_helper(\n",
                ),
                finish("tool_calls"),
            ],
            # 3. 收到语法报错后，模型提出正确的语法修复
            [
                tool(
                    "c3",
                    "repo.edit",
                    path="src/calc.py",
                    old_string="return a - b  # BUG: should be +",
                    new_string="return a + b",
                ),
                finish("tool_calls"),
            ],
            # 4. 产生 diff 证据
            [
                tool("c4", "repo.diff"),
                finish("tool_calls"),
            ],
            # 5. 运行检查确认
            [
                tool("c5", "repo.check", recipe_id="verify-calc"),
                finish("tool_calls"),
            ],
            [
                text("Fixed and verified!"),
                usage(),
                finish(),
            ],
        ]

        h = Harness(make_repo(tmp_path), turns)
        outcome = await h.service.run("Fix the bug in add()")

        assert outcome.status is RunStatus.SUCCEEDED
        assert outcome.stop_reason is StopReason.EVIDENCE_SATISFIED

        completed = [e for e in h.sink.events_of("tool.completed") if isinstance(e, ToolCompleted)]

        # c2 必须以 syntax_error 失败
        c2_ev = next(e for e in completed if e.call_id == "c2")
        assert c2_ev.status == ToolStatus.ERROR
        assert c2_ev.error_code == ToolErrorCode.SYNTAX_ERROR
        assert "Syntax validation failed" in c2_ev.summary

        # c3 必须成功应用
        c3_ev = next(e for e in completed if e.call_id == "c3")
        assert c3_ev.status == ToolStatus.OK

    async def test_check_failure_surfaces_structured_diagnostics(self, tmp_path: Path) -> None:
        import sys

        from haven.contracts.tools import RecipeSpec

        cmd = (
            "print('FAILED tests/test_calc.py::test_broken - AssertionError: mismatch'); "
            "import sys; sys.exit(1)"
        )
        recipes = {
            "test-diag": RecipeSpec(
                id="test-diag",
                argv=(sys.executable, "-c", cmd),
                timeout_seconds=10,
            )
        }
        turns: list[list[ModelEvent]] = [
            # 1. 运行产生失败诊断的检查
            [
                tool("c1", "repo.check", recipe_id="test-diag"),
                finish("tool_calls"),
            ],
            [
                text("The test failed, I can see the diagnostic failure."),
                usage(),
                finish(),
            ],
        ]

        h = Harness(make_repo(tmp_path), turns, recipes=recipes)
        outcome = await h.service.run("Run checks")

        assert outcome.status is RunStatus.SUCCEEDED
        completed = [e for e in h.sink.events_of("tool.completed") if isinstance(e, ToolCompleted)]

        c1_ev = next(e for e in completed if e.call_id == "c1")
        assert c1_ev.status == ToolStatus.OK
        assert '"exit_code": 1' in c1_ev.summary
        assert "diagnostics" in c1_ev.summary
        assert "test_broken" in c1_ev.summary
