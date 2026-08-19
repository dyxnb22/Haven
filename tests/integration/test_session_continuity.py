"""后续轮次继续同一段对话，而不是启动空白运行。

Haven 运行一个目标后停止；再次提问过去会启动没有记忆的新 RunContext。现在会话
会向前传递之前的对话记录，因此后续请求能看到第一轮做了什么（Phase 2）。持久
运行语义不变：每一轮仍是拥有自身检查点和预算的独立 Run。
"""

import sys
from pathlib import Path

from haven.contracts.events import RunCreated, ToolCompleted
from haven.domain.enums import RunStatus
from tests.integration.harness import Harness, finish, make_repo, text, tool


class TestFollowUpInheritsContext:
    async def test_the_follow_up_sees_the_first_turn(self, tmp_path: Path) -> None:
        turns = [
            [text("FIRST-ANSWER about calc.py"), finish()],
            [text("SECOND-ANSWER building on the first"), finish()],
        ]
        h = Harness(make_repo(tmp_path), turns)
        first = await h.service.run("Explain calc.py")
        second = await h.service.continue_run(first.run_id, "Now suggest a fix")

        assert second.run_id != first.run_id
        assert second.status is RunStatus.SUCCEEDED

        # 第一个请求没有既有对话；后续请求携带第一轮的答案和新指令。
        first_msgs = "\n".join(m.content for m in h.model.requests_seen[0].messages)
        follow_msgs = "\n".join(m.content for m in h.model.requests_seen[-1].messages)
        assert "FIRST-ANSWER" not in first_msgs
        assert "FIRST-ANSWER" in follow_msgs
        assert "Now suggest a fix" in follow_msgs

    async def test_the_follow_up_run_records_its_parent(self, tmp_path: Path) -> None:
        turns = [[text("one"), finish()], [text("two"), finish()]]
        h = Harness(make_repo(tmp_path), turns)
        first = await h.service.run("First")
        second = await h.service.continue_run(first.run_id, "Second")

        created = [
            e
            for e in h.sink.events_of("run.created")
            if isinstance(e, RunCreated) and e.run_id == second.run_id
        ]
        assert created and created[0].parent_run_id == first.run_id

    async def test_a_fresh_budget_per_turn(self, tmp_path: Path) -> None:
        """后续请求是新的工作：它的步骤预算不是上一轮剩余的预算。"""
        turns = [
            [tool("c1", "repo.read", path="src/calc.py"), finish("tool_calls")],
            [text("done"), finish()],
            [text("follow-up done"), finish()],
        ]
        h = Harness(make_repo(tmp_path), turns)
        first = await h.service.run("Read it")
        second = await h.service.continue_run(first.run_id, "Anything else?")
        # 后续轮次使用自己的步骤计数，而不是延续第一轮的计数。
        assert second.steps == 1

    async def test_continuing_a_missing_run_is_refused(self, tmp_path: Path) -> None:
        h = Harness(make_repo(tmp_path), [[text("x"), finish()]])
        try:
            await h.service.continue_run("run-does-not-exist", "hello")
        except ValueError as exc:
            assert "no checkpoint" in str(exc).lower()
        else:
            raise AssertionError("continuing a run with no checkpoint should raise")

    async def test_continuing_a_different_workspace_is_refused(self, tmp_path: Path) -> None:
        """后续请求不得将一次运行的对话记录嫁接到另一个仓库。"""
        import pytest

        h = Harness(make_repo(tmp_path), [[text("one"), finish()], [text("two"), finish()]])
        first = await h.service.run("First")
        checkpoint = await h.store.load_checkpoint(first.run_id)
        assert checkpoint is not None
        await h.store.save_checkpoint(
            checkpoint.model_copy(update={"workspace_digest": "a-different-workspace"})
        )
        with pytest.raises(ValueError, match="workspace identity"):
            await h.service.continue_run(first.run_id, "Second")

    async def test_follow_up_diff_excludes_the_first_turns_changes(self, tmp_path: Path) -> None:
        """第二轮的运行差异只属于本轮：不得重新报告第一轮的编辑。"""
        from haven.contracts.events import DiffPreview

        repo = make_repo(tmp_path)
        turns = [
            # 第 1 轮完整满足门禁（edit + diff + check），因此干净结束，不会把
            # nudge 发到原本属于第 2 轮的 turn 中。
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
            [tool("c3", "repo.diff"), finish("tool_calls")],
            [tool("c4", "repo.check", recipe_id="always-pass"), finish("tool_calls")],
            [text("Fixed in turn one."), finish()],
            # 第 2 轮（continue）：只有 diff，没有新的 edit。
            [tool("c5", "repo.diff"), finish("tool_calls")],
            [text("Nothing changed this turn."), finish()],
        ]
        h = Harness(repo, turns)
        first = await h.service.run("Fix add()")
        h.sink.envelopes.clear()
        await h.service.continue_run(first.run_id, "Did you change anything else?")

        diffs = [e for e in h.sink.events_of("diff.preview") if isinstance(e, DiffPreview)]
        assert diffs, "the follow-up ran repo.diff"
        assert diffs[-1].files_changed == 0, "follow-up diff leaked the first turn's edit"


class TestProcessToolsAcrossTurns:
    """repo.check 和 repo.exec 在持续运行中必须保持一致：使用相同配方、重新创建临时
    目录，且第一轮状态不得泄漏。Tier 3 审计正是要求固定这一回归。"""

    async def test_check_runs_green_again_on_the_follow_up_turn(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        turns = [
            [tool("c1", "repo.check", recipe_id="always-pass"), finish("tool_calls")],
            [text("Checked in turn one."), finish()],
            [tool("c2", "repo.check", recipe_id="always-pass"), finish("tool_calls")],
            [text("Checked again in turn two."), finish()],
        ]
        h = Harness(repo, turns)
        first = await h.service.run("Verify the project")
        h.sink.envelopes.clear()
        second = await h.service.continue_run(first.run_id, "Verify once more")

        assert second.status is RunStatus.SUCCEEDED
        checks = [
            e
            for e in h.sink.events_of("tool.completed")
            if isinstance(e, ToolCompleted) and e.tool_name == "repo.check"
        ]
        assert checks and checks[0].status == "ok" and not checks[0].error_code

    async def test_exec_runs_again_on_the_follow_up_turn(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        argv = [sys.executable, "-c", "print('hello from exec')"]
        turns = [
            [tool("c1", "repo.exec", argv=argv, cwd="."), finish("tool_calls")],
            [text("Ran it."), finish()],
            [tool("c2", "repo.exec", argv=argv, cwd="."), finish("tool_calls")],
            [text("Ran it again."), finish()],
        ]
        h = Harness(repo, turns)
        first_scratch = h.service._scratch_dir  # noqa: SLF001 - 生命周期不变量
        first = await h.service.run("Run the command")
        assert not first_scratch.exists()
        h.sink.envelopes.clear()
        second = await h.service.continue_run(first.run_id, "Run it once more")
        second_scratch = h.service._scratch_dir  # noqa: SLF001 - 生命周期不变量

        assert second.status is RunStatus.SUCCEEDED
        assert second_scratch != first_scratch
        assert not second_scratch.exists()
        execs = [
            e
            for e in h.sink.events_of("tool.completed")
            if isinstance(e, ToolCompleted) and e.tool_name == "repo.exec"
        ]
        assert execs and execs[0].status == "ok" and not execs[0].error_code
