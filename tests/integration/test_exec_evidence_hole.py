"""repo.exec 执行的写入不得逃过 Evidence Gate。

过去门禁依据的证据账本只由 repo.edit / repo.create 写入，因此沙箱命令可以在没有
账本记录的情况下修改工作区，运行会被报告为“没有变更但成功”。ADR 0012 通过将
进程写入归因到账本关闭了这一缺口。
"""

import sys
from pathlib import Path

from haven.domain.enums import RunStatus, StopReason
from tests.integration.harness import Harness, finish, make_repo, text, tool


class TestExecWritesAreAttributed:
    async def test_a_write_via_exec_cannot_be_reported_as_no_changes(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        turns = [
            [
                tool(
                    "c1",
                    "repo.exec",
                    argv=[
                        sys.executable,
                        "-c",
                        "open('src/calc.py','w').write('def add(a,b):\\n    return a+b\\n')",
                    ],
                    cwd=".",
                ),
                finish("tool_calls"),
            ],
            [text("Fixed it."), finish()],
        ]
        h = Harness(repo, turns)
        outcome = await h.service.run("Fix add() however you like")

        assert "return a+b" in (repo / "src" / "calc.py").read_text(), (
            "precondition: the command must actually have written the file"
        )
        # 写入现在会被归因，因此门禁要求之后有 diff 和通过的 check——但本次
        # 运行从未产生它们。
        assert not (
            outcome.status is RunStatus.SUCCEEDED and outcome.stop_reason is StopReason.FINAL_ANSWER
        ), "a run that rewrote a file was accepted as having made no changes"

    async def test_a_run_whose_exec_changes_nothing_is_unaffected(self, tmp_path: Path) -> None:
        """只读命令不得记录为写入，否则每个安全的 exec 都会错误触发门禁。"""
        repo = make_repo(tmp_path)
        turns = [
            [tool("c1", "repo.exec", argv=["ls", "-la"], cwd="."), finish("tool_calls")],
            [text("Listed the directory; made no changes."), finish()],
        ]
        h = Harness(repo, turns)
        outcome = await h.service.run("List the repository")

        assert outcome.status is RunStatus.SUCCEEDED
        assert outcome.stop_reason is StopReason.FINAL_ANSWER


class TestProtectedPathTamperIsDetected:
    async def test_a_process_that_rewrites_dot_git_raises_an_error_notice(
        self, tmp_path: Path
    ) -> None:
        """Landlock 无法从可写工作区中划出 `.git`，而变更快照会将受保护路径排除在
        普通归因之外——因此进程触碰这些路径过去是不可见的。现在必须在审计轨迹中
        作为错误暴露（ADR 0017）。"""
        from haven.contracts.events import Notice

        repo = make_repo(tmp_path)
        (repo / ".git").mkdir()
        (repo / ".git" / "config").write_text("[core]\n")
        turns = [
            [
                tool(
                    "c1",
                    "repo.exec",
                    argv=[sys.executable, "-c", "open('.git/config','a').write('tampered\\n')"],
                    cwd=".",
                ),
                finish("tool_calls"),
            ],
            [text("Done."), finish()],
        ]
        h = Harness(repo, turns)
        await h.service.run("Do something")

        assert "tampered" in (repo / ".git" / "config").read_text(), (
            "precondition: the recording launcher does not enforce, so the write lands"
        )
        errors = [
            e
            for e in h.sink.events_of("notice")
            if isinstance(e, Notice) and e.level == "error" and ".git" in e.message
        ]
        assert errors, "a protected-path change during a process must be surfaced, not silent"

    async def test_an_exec_that_tampers_fails_as_a_tool_call(self, tmp_path: Path) -> None:
        """ADR 0018：违规是硬结果。exec 调用本身必须返回 protected_path_tampered，
        而不是成功后附带一条说明。"""
        from haven.contracts.events import ToolCompleted

        repo = make_repo(tmp_path)
        (repo / ".git").mkdir()
        (repo / ".git" / "config").write_text("[core]\n")
        turns = [
            [
                tool(
                    "c1",
                    "repo.exec",
                    argv=[sys.executable, "-c", "open('.git/config','a').write('tampered\\n')"],
                    cwd=".",
                ),
                finish("tool_calls"),
            ],
            [text("Done."), finish()],
        ]
        h = Harness(repo, turns)
        await h.service.run("Do something")

        completed = [
            e
            for e in h.sink.events_of("tool.completed")
            if isinstance(e, ToolCompleted) and e.tool_name == "repo.exec"
        ]
        assert completed and completed[0].error_code == "protected_path_tampered"

    async def test_a_check_that_tampers_fails_and_records_no_check_evidence(
        self, tmp_path: Path
    ) -> None:
        """重写控制平面的配方不是验证：调用会以 protected_path_tampered 失败，且不
        会产生检查证据，因此 Evidence Gate 不能被篡改检查满足（ADR 0018）。"""
        from haven.contracts.events import ToolCompleted
        from haven.contracts.tools import RecipeSpec
        from tests.integration.harness import default_recipes

        repo = make_repo(tmp_path)
        (repo / ".git").mkdir()
        (repo / ".git" / "config").write_text("[core]\n")
        recipes = default_recipes() | {
            "tampering": RecipeSpec(
                id="tampering",
                argv=(
                    sys.executable,
                    "-c",
                    "open('.git/config','a').write('tampered\\n')",
                ),
                timeout_seconds=30,
            )
        }
        turns = [
            [tool("c1", "repo.check", recipe_id="tampering"), finish("tool_calls")],
            [text("Checked."), finish()],
        ]
        h = Harness(repo, turns, recipes=recipes)
        await h.service.run("Verify the project")

        completed = [
            e
            for e in h.sink.events_of("tool.completed")
            if isinstance(e, ToolCompleted) and e.tool_name == "repo.check"
        ]
        assert completed and completed[0].error_code == "protected_path_tampered"
        check_evidence = [
            e
            for e in h.sink.events_of("evidence.recorded")
            if getattr(e, "evidence_kind", "") == "check"
        ]
        assert not check_evidence, "a tampering check must not count as verification"
