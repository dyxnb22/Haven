"""由 ScriptedModel 驱动的端到端代理流程（完全离线）。"""

import asyncio
import sys
from pathlib import Path

import pytest

from haven.contracts.events import (
    EventEnvelope,
    EvidenceRecorded,
    PolicyDecided,
    RunFinished,
    ToolCompleted,
)
from haven.contracts.model import ModelEvent
from haven.domain.approval import ApprovalRequest
from haven.domain.budget import Budget
from haven.domain.enums import ApprovalDecision, EffectState, PermissionMode, RunStatus, StopReason

from .harness import BUGGY_CALC, Harness, finish, make_repo, text, tool, usage


class TestReadOnlyJourney:
    async def test_search_read_answer(self, tmp_path: Path) -> None:
        turns: list[list[ModelEvent]] = [
            [
                text("Let me look for the bug."),
                tool("c1", "repo.search", pattern="BUG", path="."),
                finish("tool_calls"),
            ],
            [
                tool("c2", "repo.read", path="src/calc.py"),
                finish("tool_calls"),
            ],
            [
                text("The bug is in src/calc.py line 2: add() subtracts instead of adding."),
                usage(),
                finish(),
            ],
        ]
        h = Harness(make_repo(tmp_path), turns)
        outcome = await h.service.run("Where is the bug in add()?")

        assert outcome.status is RunStatus.SUCCEEDED
        assert outcome.stop_reason is StopReason.FINAL_ANSWER
        assert outcome.steps == 3
        assert outcome.tool_calls == 2
        assert "line 2" in outcome.final_text

        kinds = h.sink.kinds()
        assert kinds[0] == "run.created"
        assert kinds[-1] == "run.finished"
        # 每次调用都走完整的 proposal -> policy -> execution -> completion 链
        assert kinds.count("tool.proposed") == 2
        assert kinds.count("policy.decided") == 2
        assert kinds.count("tool.completed") == 2

    async def test_trace_is_persisted(self, tmp_path: Path) -> None:
        turns: list[list[ModelEvent]] = [[text("No tools needed: answer."), finish()]]
        h = Harness(make_repo(tmp_path), turns)
        outcome = await h.service.run("Say hi")
        stored = await h.store.load_events(outcome.run_id)
        stored_kinds = [env.event.kind for env in stored]
        assert "run.created" in stored_kinds
        assert "model.completed" in stored_kinds
        assert "run.finished" in stored_kinds
        # 临时增量绝不会进入日志
        assert "assistant.delta" not in stored_kinds


class TestEditJourney:
    @staticmethod
    def fix_turns() -> list[list[ModelEvent]]:
        return [
            [tool("c1", "repo.read", path="src/calc.py"), finish("tool_calls")],
            [
                tool(
                    "c2",
                    "repo.edit",
                    path="src/calc.py",
                    old_string="return a - b  # BUG: should be +",
                    new_string="return a + b",
                    summary="fix add() to use addition",
                ),
                finish("tool_calls"),
            ],
            [tool("c3", "repo.diff"), finish("tool_calls")],
            [tool("c4", "repo.check", recipe_id="verify-calc"), finish("tool_calls")],
            [
                text("Fixed add() and verified: diff applied, verify-calc exited 0."),
                finish(),
            ],
        ]

    async def test_full_fix_with_evidence(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        h = Harness(repo, self.fix_turns())
        outcome = await h.service.run("Fix the bug in add()")

        assert outcome.status is RunStatus.SUCCEEDED
        assert outcome.stop_reason is StopReason.EVIDENCE_SATISFIED
        assert "return a + b" in (repo / "src" / "calc.py").read_text()

        # 两次审批：edit 和 check
        approvals = h.sink.events_of("approval.requested")
        assert len(approvals) == 2
        decisions = h.sink.events_of("approval.decided")
        assert len(decisions) == 2

        finished = h.sink.events_of("run.finished")[0]
        assert isinstance(finished, RunFinished)
        assert finished.gate_reason == "evidence_satisfied"

    async def test_rejection_leaves_file_untouched(self, tmp_path: Path) -> None:
        from haven.application.approvals import AutoApprover

        repo = make_repo(tmp_path)
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
            [text("The user rejected the change; stopping here."), finish()],
        ]
        h = Harness(repo, turns, approver=AutoApprover("reject_all"))
        outcome = await h.service.run("Fix the bug in add()")

        # 没有写入任何内容，因此可以接受最终答案
        assert outcome.status is RunStatus.SUCCEEDED
        assert outcome.stop_reason is StopReason.FINAL_ANSWER
        assert (repo / "src" / "calc.py").read_text() == BUGGY_CALC

        completed = [
            e
            for e in h.sink.events_of("tool.completed")
            if isinstance(e, ToolCompleted) and e.call_id == "c2"
        ]
        assert completed[0].error_code == "approval_rejected"

    async def test_edit_without_prior_read_is_refused(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        turns = [
            [
                tool(
                    "c1",
                    "repo.edit",
                    path="src/calc.py",
                    old_string="return a - b  # BUG: should be +",
                    new_string="return a + b",
                ),
                finish("tool_calls"),
            ],
            [text("Could not edit without reading first."), finish()],
        ]
        h = Harness(repo, turns)
        await h.service.run("Fix the bug")
        completed = h.sink.events_of("tool.completed")
        assert isinstance(completed[0], ToolCompleted)
        assert completed[0].error_code == "invalid_arguments"
        assert (repo / "src" / "calc.py").read_text() == BUGGY_CALC

    async def test_evidence_missing_stops_run(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
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
            # 模型在没有 diff/check 证据的情况下三次声称成功
            [text("Done! I fixed it."), finish()],
            [text("Really, it is done."), finish()],
            [text("Trust me, done."), finish()],
        ]
        h = Harness(repo, turns)
        outcome = await h.service.run("Fix the bug in add()")

        assert outcome.status is RunStatus.STOPPED
        assert outcome.stop_reason is StopReason.EVIDENCE_MISSING
        # 停止前已将门禁失败反馈给模型并发送 nudge
        notices = h.sink.events_of("notice")
        assert any("evidence gate" in getattr(n, "message", "") for n in notices)

    async def test_failing_check_blocks_success(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        turns = [
            [tool("c1", "repo.read", path="src/calc.py"), finish("tool_calls")],
            [
                tool(
                    "c2",
                    "repo.edit",
                    path="src/calc.py",
                    old_string="return a - b  # BUG: should be +",
                    new_string="return a * b",  # 错误的修复
                ),
                finish("tool_calls"),
            ],
            [tool("c3", "repo.diff"), finish("tool_calls")],
            [tool("c4", "repo.check", recipe_id="verify-calc"), finish("tool_calls")],
            [text("All done."), finish()],
            [text("All done."), finish()],
            [text("All done."), finish()],
        ]
        h = Harness(repo, turns)
        outcome = await h.service.run("Fix the bug in add()")
        assert outcome.status is RunStatus.STOPPED
        assert outcome.stop_reason is StopReason.EVIDENCE_MISSING
        assert outcome.gate_reason == "check_failed"


class TestCreateJourney:
    async def test_create_a_test_file_and_verify(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
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
            [
                tool(
                    "c3",
                    "repo.create",
                    path="tests/test_add.py",
                    content=(
                        "from src.calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
                    ),
                    summary="cover the fixed behavior",
                ),
                finish("tool_calls"),
            ],
            [tool("c4", "repo.diff"), finish("tool_calls")],
            [tool("c5", "repo.check", recipe_id="verify-calc"), finish("tool_calls")],
            [text("Fixed add() and added a regression test."), finish()],
        ]
        h = Harness(repo, turns)
        outcome = await h.service.run("Fix add() and add a test")

        assert outcome.status is RunStatus.SUCCEEDED
        assert outcome.stop_reason is StopReason.EVIDENCE_SATISFIED
        assert (repo / "tests" / "test_add.py").is_file()
        # 新文件会在运行 diff 中显示为纯新增
        diffs = h.sink.events_of("diff.preview")
        assert any("tests/test_add.py" in getattr(d, "preview", "") for d in diffs)
        # 创建像其他写操作一样经过审批
        approvals = h.sink.events_of("approval.requested")
        assert any("create tests/test_add.py" in getattr(a, "summary", "") for a in approvals)

    async def test_create_over_existing_file_is_refused(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        original = (repo / "src" / "calc.py").read_text()
        turns = [
            [
                tool("c1", "repo.create", path="src/calc.py", content="wiped\n"),
                finish("tool_calls"),
            ],
            [text("That file already exists; I would need repo.edit."), finish()],
        ]
        h = Harness(repo, turns)
        await h.service.run("Rewrite calc.py from scratch")

        assert (repo / "src" / "calc.py").read_text() == original
        completed = h.sink.events_of("tool.completed")
        assert isinstance(completed[0], ToolCompleted)
        assert completed[0].error_code == "invalid_arguments"
        # 甚至没有到达审批阶段
        assert h.sink.events_of("approval.requested") == []

    async def test_create_outside_workspace_is_denied(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        turns = [
            [
                tool("c1", "repo.create", path="../escaped.py", content="evil\n"),
                finish("tool_calls"),
            ],
            [text("Denied."), finish()],
        ]
        h = Harness(repo, turns)
        await h.service.run("Write a file next to the repo")

        assert not (tmp_path / "escaped.py").exists()
        denies = [
            e
            for e in h.sink.events_of("policy.decided")
            if isinstance(e, PolicyDecided) and e.decision == "deny"
        ]
        assert denies and denies[0].reason_code == "outside_workspace"

    async def test_create_denied_in_read_only_mode(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        turns = [
            [
                tool("c1", "repo.create", path="notes.md", content="hello\n"),
                finish("tool_calls"),
            ],
            [text("Read-only mode."), finish()],
        ]
        h = Harness(repo, turns, mode=PermissionMode.READ_ONLY)
        await h.service.run("Leave me a note")

        assert not (repo / "notes.md").exists()
        denies = [
            e
            for e in h.sink.events_of("policy.decided")
            if isinstance(e, PolicyDecided) and e.decision == "deny"
        ]
        assert denies and denies[0].reason_code == "read_only_mode"

    async def test_rejected_create_writes_nothing(self, tmp_path: Path) -> None:
        from haven.application.approvals import AutoApprover

        repo = make_repo(tmp_path)
        turns = [
            [
                tool("c1", "repo.create", path="notes.md", content="hello\n"),
                finish("tool_calls"),
            ],
            [text("You rejected it."), finish()],
        ]
        h = Harness(repo, turns, approver=AutoApprover("reject_all"))
        await h.service.run("Leave me a note")

        assert not (repo / "notes.md").exists()
        completed = h.sink.events_of("tool.completed")
        assert isinstance(completed[0], ToolCompleted)
        assert completed[0].error_code == "approval_rejected"


class TestEditScopeJourney:
    async def test_replace_all_rename(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        (repo / "src" / "names.py").write_text(
            "def helper():\n    return 1\n\n\nx = helper()\ny = helper()\n"
        )
        turns = [
            [tool("c1", "repo.read", path="src/names.py"), finish("tool_calls")],
            [
                tool(
                    "c2",
                    "repo.edit",
                    path="src/names.py",
                    old_string="helper",
                    new_string="compute",
                    replace_all=True,
                    summary="rename helper to compute",
                ),
                finish("tool_calls"),
            ],
            [tool("c3", "repo.diff"), finish("tool_calls")],
            [tool("c4", "repo.check", recipe_id="always-pass"), finish("tool_calls")],
            [text("Renamed helper to compute everywhere."), finish()],
        ]
        h = Harness(repo, turns)
        outcome = await h.service.run("Rename helper to compute")

        assert outcome.status is RunStatus.SUCCEEDED
        content = (repo / "src" / "names.py").read_text()
        assert "helper" not in content
        assert content.count("compute") == 3
        approvals = h.sink.events_of("approval.requested")
        assert any("[all occurrences]" in getattr(a, "summary", "") for a in approvals)

    async def test_ambiguous_edit_is_recoverable(self, tmp_path: Path) -> None:
        """失败消息必须足够可操作，使下一轮能够据此处理。"""
        repo = make_repo(tmp_path)
        turns = [
            [tool("c1", "repo.read", path="src/calc.py"), finish("tool_calls")],
            [
                tool(
                    "c2", "repo.edit", path="src/calc.py", old_string="return a - b", new_string="x"
                ),
                finish("tool_calls"),
            ],
            [
                tool(
                    "c3",
                    "repo.edit",
                    path="src/calc.py",
                    old_string="return a - b",
                    new_string="return a + b",
                    occurrence=1,
                ),
                finish("tool_calls"),
            ],
            [tool("c4", "repo.diff"), finish("tool_calls")],
            [tool("c5", "repo.check", recipe_id="verify-calc"), finish("tool_calls")],
            [text("Disambiguated with occurrence=1 and verified."), finish()],
        ]
        h = Harness(repo, turns)
        outcome = await h.service.run("Fix add()")

        assert outcome.status is RunStatus.SUCCEEDED
        completed = [e for e in h.sink.events_of("tool.completed") if isinstance(e, ToolCompleted)]
        assert completed[1].error_code == "ambiguous_match"
        assert (repo / "src" / "calc.py").read_text().count("return a + b") == 1


class TestPlanTool:
    async def test_plan_is_recorded_and_resent_every_turn(self, tmp_path: Path) -> None:
        turns = [
            [
                tool(
                    "c1",
                    "task.plan",
                    steps=[
                        {"title": "locate the bug", "status": "in_progress"},
                        {"title": "fix it", "status": "pending"},
                    ],
                ),
                finish("tool_calls"),
            ],
            [tool("c2", "repo.read", path="src/calc.py"), finish("tool_calls")],
            [text("The bug is on line 2."), finish()],
        ]
        h = Harness(make_repo(tmp_path), turns)
        outcome = await h.service.run("Find the bug")
        assert outcome.status is RunStatus.SUCCEEDED

        updates = h.sink.events_of("plan.updated")
        assert len(updates) == 1
        # 计划设置后，每个请求都会携带它
        later_requests = h.model.requests_seen[1:]
        assert later_requests
        for request in later_requests:
            assert any("locate the bug" in m.content for m in request.messages)

    async def test_plan_appears_as_untrusted_context_segment(self, tmp_path: Path) -> None:
        turns = [
            [
                tool("c1", "task.plan", steps=[{"title": "step one", "status": "pending"}]),
                finish("tool_calls"),
            ],
            [text("done"), finish()],
        ]
        h = Harness(make_repo(tmp_path), turns)
        await h.service.run("Do something")

        built = h.sink.events_of("context.built")
        last = built[-1]
        plan_segments = [s for s in last.segments if s.source == "task_plan"]
        assert len(plan_segments) == 1
        assert plan_segments[0].trust == "untrusted"

    async def test_plan_needs_no_approval_even_in_read_only(self, tmp_path: Path) -> None:
        turns = [
            [
                tool("c1", "task.plan", steps=[{"title": "review only", "status": "pending"}]),
                finish("tool_calls"),
            ],
            [text("Reviewed."), finish()],
        ]
        h = Harness(make_repo(tmp_path), turns, mode=PermissionMode.READ_ONLY)
        outcome = await h.service.run("Review the repo")
        assert outcome.status is RunStatus.SUCCEEDED
        assert h.sink.events_of("approval.requested") == []
        assert h.sink.events_of("plan.updated")

    async def test_plan_is_checkpointed(self, tmp_path: Path) -> None:
        turns = [
            [
                tool("c1", "task.plan", steps=[{"title": "persisted step", "status": "done"}]),
                finish("tool_calls"),
            ],
            [text("done"), finish()],
        ]
        h = Harness(make_repo(tmp_path), turns)
        outcome = await h.service.run("Do something")

        checkpoint = await h.store.load_checkpoint(outcome.run_id)
        assert checkpoint is not None
        assert [s.title for s in checkpoint.plan] == ["persisted step"]
        assert checkpoint.plan[0].status == "done"

    async def test_invalid_plan_is_rejected(self, tmp_path: Path) -> None:
        turns = [
            [tool("c1", "task.plan", steps=[]), finish("tool_calls")],
            [text("Plan needs at least one step."), finish()],
        ]
        h = Harness(make_repo(tmp_path), turns)
        await h.service.run("Do something")
        completed = h.sink.events_of("tool.completed")
        assert isinstance(completed[0], ToolCompleted)
        assert completed[0].error_code == "invalid_arguments"


class TestDeterministicReview:
    @staticmethod
    def _turns_writing(line: str) -> list[list[ModelEvent]]:
        return [
            [tool("c1", "repo.read", path="src/calc.py"), finish("tool_calls")],
            [
                tool(
                    "c2",
                    "repo.edit",
                    path="src/calc.py",
                    old_string="return a - b  # BUG: should be +",
                    new_string=f"return a + b\n{line}",
                ),
                finish("tool_calls"),
            ],
            [tool("c3", "repo.diff"), finish("tool_calls")],
            [tool("c4", "repo.check", recipe_id="verify-calc"), finish("tool_calls")],
            [text("All done."), finish()],
            [text("Really done."), finish()],
            [text("Definitely done."), finish()],
        ]

    async def test_added_secret_blocks_success(self, tmp_path: Path) -> None:
        h = Harness(make_repo(tmp_path), self._turns_writing('TOKEN = "AKIAIOSFODNN7EXAMPLE"'))
        outcome = await h.service.run("Fix add()")

        assert outcome.status is RunStatus.STOPPED
        assert outcome.stop_reason is StopReason.EVIDENCE_MISSING
        assert outcome.gate_reason == "review_failed"
        notices = h.sink.events_of("notice")
        assert any("AWS access key" in getattr(n, "message", "") for n in notices)

    async def test_debug_leftover_blocks_success(self, tmp_path: Path) -> None:
        h = Harness(make_repo(tmp_path), self._turns_writing("    breakpoint()"))
        outcome = await h.service.run("Fix add()")
        assert outcome.gate_reason == "review_failed"

    async def test_clean_change_still_succeeds(self, tmp_path: Path) -> None:
        h = Harness(make_repo(tmp_path), self._turns_writing("# fixed"))
        outcome = await h.service.run("Fix add()")
        assert outcome.status is RunStatus.SUCCEEDED
        assert outcome.gate_reason == "evidence_satisfied"

    async def test_review_does_not_apply_to_read_only_runs(self, tmp_path: Path) -> None:
        """没有写入就没有需要审查的内容；普通回答仍然可以成功。"""
        repo = make_repo(tmp_path)
        (repo / "secrets.txt").write_text('password = "hunter2hunter2"\n')
        turns = [
            [tool("c1", "repo.read", path="secrets.txt"), finish("tool_calls")],
            [text("That file contains a hardcoded credential."), finish()],
        ]
        h = Harness(repo, turns)
        outcome = await h.service.run("Audit the repo")
        assert outcome.status is RunStatus.SUCCEEDED
        assert outcome.stop_reason is StopReason.FINAL_ANSWER


class TestUnwinnableEvidenceGate:
    """一次实时失败的回归：没有配方 + 发生写入 = 无法获胜。"""

    @staticmethod
    def _edit_turns() -> list[list[ModelEvent]]:
        return [
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
            [text("Fixed it."), finish()],
            [text("Fixed it."), finish()],
            [text("Fixed it."), finish()],
            [text("Fixed it."), finish()],
        ]

    async def test_stops_immediately_with_an_accurate_reason(self, tmp_path: Path) -> None:
        h = Harness(make_repo(tmp_path), self._edit_turns())
        h.service._recipes = {}  # type: ignore[attr-defined]

        outcome = await h.service.run("Fix add()")
        assert outcome.status is RunStatus.STOPPED
        assert outcome.stop_reason is StopReason.VERIFICATION_UNAVAILABLE
        assert outcome.gate_reason == "verification_unavailable"

    async def test_does_not_burn_the_budget_nudging(self, tmp_path: Path) -> None:
        h = Harness(make_repo(tmp_path), self._edit_turns())
        h.service._recipes = {}  # type: ignore[attr-defined]

        outcome = await h.service.run("Fix add()")
        # 读取、编辑和回答共 3 步，然后停止，而不是再发送两次 nudge 最终耗尽预算。
        assert outcome.steps == 3

    async def test_prompt_does_not_promise_a_check_that_does_not_exist(
        self, tmp_path: Path
    ) -> None:
        h = Harness(make_repo(tmp_path), [[text("nothing to do"), finish()]])
        h.service._recipes = {}  # type: ignore[attr-defined]

        await h.service.run("Just answer")
        prompt = h.model.requests_seen[0].messages[0].content
        assert "NO check recipes are registered" in prompt
        assert "you MUST call repo.diff and then repo.check" not in prompt


class TestPolicyEnforcement:
    async def test_read_only_mode_denies_edit(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
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
            [text("Cannot edit in read-only mode."), finish()],
        ]
        h = Harness(repo, turns, mode=PermissionMode.READ_ONLY)
        outcome = await h.service.run("Fix the bug")

        assert (repo / "src" / "calc.py").read_text() == BUGGY_CALC
        assert outcome.status is RunStatus.SUCCEEDED  # 最终答案，没有写入
        denies = [
            e
            for e in h.sink.events_of("policy.decided")
            if isinstance(e, PolicyDecided) and e.decision == "deny"
        ]
        assert len(denies) == 1
        assert denies[0].reason_code == "read_only_mode"
        # 被拒绝的操作甚至不会请求审批
        assert h.sink.events_of("approval.requested") == []

    async def test_path_escape_denied_and_content_not_leaked(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        (tmp_path / "secret.txt").write_text("TOP-SECRET-CONTENT")
        turns = [
            [tool("c1", "repo.read", path="../secret.txt"), finish("tool_calls")],
            [text("I could not read that path."), finish()],
        ]
        h = Harness(repo, turns)
        await h.service.run("Read ../secret.txt")

        completed = h.sink.events_of("tool.completed")
        assert isinstance(completed[0], ToolCompleted)
        assert completed[0].error_code == "denied"
        # 秘密从未进入模型 transcript
        for request in h.model.requests_seen:
            for message in request.messages:
                assert "TOP-SECRET-CONTENT" not in message.content

    async def test_unknown_tool_denied(self, tmp_path: Path) -> None:
        turns: list[list[ModelEvent]] = [
            [tool("c1", "shell.exec", command="rm -rf /"), finish("tool_calls")],
            [text("Tool unavailable."), finish()],
        ]
        h = Harness(make_repo(tmp_path), turns)
        await h.service.run("Run a shell command")
        completed = h.sink.events_of("tool.completed")
        assert isinstance(completed[0], ToolCompleted)
        assert completed[0].error_code == "unknown_tool"

    async def test_unregistered_recipe_denied(self, tmp_path: Path) -> None:
        turns: list[list[ModelEvent]] = [
            [tool("c1", "repo.check", recipe_id="curl-evil.sh"), finish("tool_calls")],
            [text("Recipe not registered."), finish()],
        ]
        h = Harness(make_repo(tmp_path), turns)
        await h.service.run("Run checks")
        denies = [
            e
            for e in h.sink.events_of("policy.decided")
            if isinstance(e, PolicyDecided) and e.decision == "deny"
        ]
        assert denies and denies[0].reason_code == "unregistered_recipe"


class TestApprovalBinding:
    async def test_stale_approval_fails_closed(self, tmp_path: Path) -> None:
        """文件在人类审批和执行之间发生变化（TOCTOU）。"""
        repo = make_repo(tmp_path)

        class TamperingApprover:
            async def respond(self, request: ApprovalRequest) -> ApprovalDecision:
                # 用户“思考”期间，文件在我们这边发生变化
                (repo / "src" / "calc.py").write_text("everything changed\n")
                return ApprovalDecision.APPROVED

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
            [text("The file changed; I stopped."), finish()],
        ]
        h = Harness(repo, turns, approver=TamperingApprover())
        await h.service.run("Fix the bug")

        completed = [
            e
            for e in h.sink.events_of("tool.completed")
            if isinstance(e, ToolCompleted) and e.call_id == "c2"
        ]
        assert completed[0].error_code == "stale_preimage"
        # 被篡改的内容不会被过时的 edit 触碰
        assert (repo / "src" / "calc.py").read_text() == "everything changed\n"

    async def test_an_oversized_file_reports_the_real_reason_not_a_stale_preimage(
        self, tmp_path: Path
    ) -> None:
        """超过 MAX_EDIT_FILE_BYTES 的文件必须得到"过大"这个准确原因。

        `path_facts` 对超大文件不算摘要（digest 为 None），而 `repo.read` 没有这个
        上限、记录的 digest 是真的。若无条件比较，模型每次编辑都得到
        `stale_preimage`，而重读根本修不好——它会一路重试到被卡死检测停掉。
        """
        repo = make_repo(tmp_path)
        big = repo / "src" / "big.py"
        big.write_text("x = 1\n" + ("# pad\n" * 60_000), encoding="utf-8")
        assert big.stat().st_size > 256 * 1024

        turns: list[list[ModelEvent]] = [
            [tool("c1", "repo.read", path="src/big.py"), finish("tool_calls")],
            [
                tool("c2", "repo.edit", path="src/big.py", old_string="x = 1", new_string="x = 2"),
                finish("tool_calls"),
            ],
            [text("The file is too large to edit."), finish()],
        ]
        h = Harness(repo, turns)
        await h.service.run("Edit the big file")

        completed = [
            e
            for e in h.sink.events_of("tool.completed")
            if isinstance(e, ToolCompleted) and e.call_id == "c2"
        ]
        assert completed[0].error_code != "stale_preimage"
        # 文件未被触碰：错误发生在执行之前
        assert "x = 1" in big.read_text()


class TestBudgetsAndStops:
    async def test_step_budget_stops_run(self, tmp_path: Path) -> None:
        turns: list[list[ModelEvent]] = [
            [tool("c1", "repo.list", path="."), finish("tool_calls")],
        ]
        h = Harness(make_repo(tmp_path), turns, budget=Budget(max_steps=1), repeat_last=True)
        outcome = await h.service.run("Explore forever")
        assert outcome.status is RunStatus.STOPPED
        assert outcome.stop_reason is StopReason.STEP_BUDGET_EXHAUSTED

        token_limited = Harness(
            make_repo(tmp_path / "tokens"),
            [[text("would otherwise succeed"), usage(100, 20, estimated=True), finish()]],
            budget=Budget(max_input_tokens=50),
        )
        token_outcome = await token_limited.service.run("Use too many tokens")
        assert token_outcome.status is RunStatus.STOPPED
        assert token_outcome.stop_reason is StopReason.TOKEN_BUDGET_EXHAUSTED
        assert token_outcome.usage_estimated is True

    async def test_step_exhaustion_still_lets_the_model_say_what_it_found(
        self, tmp_path: Path
    ) -> None:
        """步数用完后，已经读过的内容不该随运行一起被丢掉。

        直接停会把"步数用完了"当作全部交付物。收尾调用不给工具，所以模型只能
        把已得到的说出来——成功的定义没有变松，只是一个 stopped 运行能带上内容。
        """
        turns: list[list[ModelEvent]] = [
            [tool("c1", "repo.list", path="."), usage(30, 10), finish("tool_calls")],
            [
                text("I found the defect but ran out of steps before fixing it."),
                usage(5, 40),
                finish(),
            ],
        ]
        h = Harness(make_repo(tmp_path), turns, budget=Budget(max_steps=1))
        outcome = await h.service.run("Explore forever")
        assert outcome.status is RunStatus.STOPPED
        assert outcome.stop_reason is StopReason.STEP_BUDGET_EXHAUSTED
        assert "found the defect" in outcome.final_text
        # 收尾调用不计步骤：预算说不能再动手，就没有再动手。
        assert outcome.steps == 1
        # 收尾请求不带工具，所以模型没有可提议的东西。
        assert h.model.requests_seen[-1].tools == ()
        # 但 token 要照常计费：它确实发出了一次模型调用，少报就等于瞒报成本。
        # （这一行曾缺失——收尾调用走了不经主循环的路径，于是整整一次调用
        # 的用量从 RunFinished 里消失了。）
        # 用脚本模型给出确定的用量，因此可以精确断言两次调用都被计入：
        # 工具轮 30/10 + 收尾轮 5/40。少了收尾那轮就只会看到 30/10。
        assert (outcome.input_tokens, outcome.output_tokens) == (35, 50)

    async def test_resource_exhaustion_does_not_spend_one_more_call(self, tmp_path: Path) -> None:
        """token/成本耗尽是"别再花"的信号，不能再调一次模型去写总结。"""
        turns: list[list[ModelEvent]] = [
            [text("would otherwise succeed"), usage(100, 20, estimated=True), finish()],
            [text("a summary that must never be requested"), finish()],
        ]
        h = Harness(make_repo(tmp_path), turns, budget=Budget(max_input_tokens=50))
        outcome = await h.service.run("Use too many tokens")
        assert outcome.status is RunStatus.STOPPED
        assert outcome.stop_reason is StopReason.TOKEN_BUDGET_EXHAUSTED
        assert "must never be requested" not in outcome.final_text

    async def test_a_run_that_already_spoke_keeps_its_own_words(self, tmp_path: Path) -> None:
        """模型已经说了话，就不该再用一次调用覆盖它。"""
        turns: list[list[ModelEvent]] = [
            [tool("c1", "repo.list", path="."), finish("tool_calls")],
            [text("my own conclusion"), finish()],
        ]
        h = Harness(make_repo(tmp_path), turns, budget=Budget(max_steps=1), repeat_last=True)
        outcome = await h.service.run("Explore forever")
        assert outcome.stop_reason is StopReason.STEP_BUDGET_EXHAUSTED
        assert "my own conclusion" in outcome.final_text
        calls_before = len(h.model.requests_seen)
        assert calls_before >= 1

    async def test_stuck_loop_detected(self, tmp_path: Path) -> None:
        turns: list[list[ModelEvent]] = [
            [tool("c1", "repo.search", pattern="nothing_here", path="."), finish("tool_calls")],
        ]
        h = Harness(make_repo(tmp_path), turns, repeat_last=True)
        outcome = await h.service.run("Search in circles")
        assert outcome.status is RunStatus.STOPPED
        assert outcome.stop_reason is StopReason.NO_PROGRESS
        assert outcome.steps < 12  # 检测器会在步骤预算耗尽前触发

    async def test_an_alternating_pattern_is_not_detected_as_stuck(self, tmp_path: Path) -> None:
        """在循环层固定检测器的实测边界。42 次实时运行的追踪研究发现，不收敛是多样
        的无效工作，而不是重复；因此下面的运行会耗尽预算，却不会触发卡死检查
        （`docs/notes/rejected/0002`）。"""
        alternating: list[list[ModelEvent]] = []
        for i in range(8):
            path = "." if i % 2 == 0 else "src"
            alternating.append([tool(f"c{i}", "repo.list", path=path), finish("tool_calls")])
        h = Harness(make_repo(tmp_path), alternating, budget=Budget(max_steps=6))
        outcome = await h.service.run("Alternate forever")

        assert outcome.stop_reason is StopReason.STEP_BUDGET_EXHAUSTED
        assert not any(
            "stuck loop" in getattr(e, "message", "") for e in h.sink.events_of("notice")
        )

    async def test_provider_error_fails_run(self, tmp_path: Path) -> None:
        turns: list[list[ModelEvent]] = [
            [tool("c1", "repo.list", path="."), finish("tool_calls")],
            # 脚本在下一次调用时耗尽 -> ProviderError
        ]
        h = Harness(make_repo(tmp_path), turns)
        outcome = await h.service.run("Trigger a provider failure")
        assert outcome.status is RunStatus.FAILED
        assert outcome.stop_reason is StopReason.PROVIDER_ERROR


class TestCancellation:
    async def test_cancel_mid_run_finalizes_state(self, tmp_path: Path) -> None:
        from collections.abc import AsyncIterator

        from haven.contracts.model import ModelRequest

        class SlowModel:
            model_name = "slow"

            def generate_stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
                return self._gen()

            @staticmethod
            async def _gen() -> AsyncIterator[ModelEvent]:
                yield text("thinking very slowly")
                await asyncio.sleep(60)
                yield finish()

        repo = make_repo(tmp_path)
        h = Harness(repo, [])
        h.service._model = SlowModel()  # type: ignore[attr-defined]

        task = asyncio.create_task(h.service.run("Slow task"))
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        finished = h.sink.events_of("run.finished")
        assert len(finished) == 1
        assert isinstance(finished[0], RunFinished)
        assert finished[0].status == "cancelled"
        # 只断言"确实计量了"和"取消真的打断了 60 秒的模型休眠"，不断言下限：
        # 前者用 > 0 抓住"忘记填 duration"，后者用远小于休眠时长抓住"没取消成"。
        # 曾经写的是 >= 250，在 CI 上以 247 挂过一次——那是调度精度，不是行为。
        assert 0 < finished[0].duration_ms < 60_000
        run = await h.store.get_run(finished[0].run_id)
        assert run is not None
        assert run.status is RunStatus.CANCELLED

        timed = Harness(
            make_repo(tmp_path / "timed"), [], budget=Budget(max_wall_time_seconds=0.05)
        )
        timed.service._model = SlowModel()  # type: ignore[attr-defined]
        timed_outcome = await timed.service.run("Respect wall time")
        assert timed_outcome.status is RunStatus.STOPPED
        assert timed_outcome.stop_reason is StopReason.WALL_TIME_EXHAUSTED

    async def test_cancel_during_a_process_reports_an_unknown_effect(self, tmp_path: Path) -> None:
        h = Harness(
            make_repo(tmp_path),
            [
                [
                    tool(
                        "c1",
                        "repo.exec",
                        argv=[sys.executable, "-c", "import time; time.sleep(60)"],
                    ),
                    finish("tool_calls"),
                ]
            ],
        )
        task = asyncio.create_task(h.service.run("Cancel an executing process"))

        records = []
        for _ in range(200):
            active = h.service.active_run_id
            if active is not None:
                records = await h.store.load_executions(active)
                if records:
                    break
            await asyncio.sleep(0.01)
        assert records and records[0].effect_state is EffectState.STARTED

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        finished = h.sink.events_of("run.finished")
        assert len(finished) == 1
        assert isinstance(finished[0], RunFinished)
        assert finished[0].status == "effect_unknown"
        executions = await h.store.load_executions(finished[0].run_id)
        assert executions[0].effect_state is EffectState.EFFECT_UNKNOWN

    async def test_cancel_while_publishing_edit_evidence_is_effect_unknown(
        self, tmp_path: Path
    ) -> None:
        """落盘完成不等于整条执行已确认；证据广播期间取消必须保守收口。"""
        repo = make_repo(tmp_path)
        h = Harness(
            repo,
            [
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
            ],
        )
        evidence_started = asyncio.Event()

        class PauseEditEvidence:
            async def emit(self, envelope: EventEnvelope) -> None:
                if isinstance(envelope.event, EvidenceRecorded):
                    evidence_started.set()
                    await asyncio.Event().wait()

        h.emitter.add_sink(PauseEditEvidence())
        task = asyncio.create_task(h.service.run("Cancel while recording an edit"))
        await asyncio.wait_for(evidence_started.wait(), timeout=2)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert "return a + b" in (repo / "src" / "calc.py").read_text()
        finished = h.sink.events_of("run.finished")
        assert len(finished) == 1 and isinstance(finished[0], RunFinished)
        assert finished[0].status == "effect_unknown"
        executions = await h.store.load_executions(finished[0].run_id)
        assert executions[0].effect_state is EffectState.EFFECT_UNKNOWN


class TestNoToolCallEscapesTheLoop:
    """流水线的不变量：工具调用绝不向代理循环抛异常。

    `ToolPipeline.execute` 原先只捕获 `WorkspaceError`，于是一次普通的 I/O 错误
    就能穿过 execute 逃出 `run()`，整次运行崩溃——而模块文档字符串承诺的恰恰是
    "一条错误路径不会终止整个运行"。
    """

    async def test_an_unexpected_read_failure_becomes_a_structured_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = make_repo(tmp_path)
        h = Harness(
            repo,
            [
                [tool("c1", "repo.read", path="src/calc.py"), finish("tool_calls")],
                [text("The read failed; I will say so."), finish()],
            ],
        )

        async def boom(*args: object, **kwargs: object) -> None:
            raise OSError("Input/output error")

        monkeypatch.setattr(h.workspace, "read_file", boom)
        outcome = await h.service.run("Read the calculator")

        assert outcome.status is RunStatus.SUCCEEDED, "一次读取失败不该终止整次运行"
        tool_msg = next(m.content for m in h.model.requests_seen[-1].messages if m.role == "tool")
        assert '"error_code": "internal"' in tool_msg
        # 带上异常类型：轨迹里必须能看出这是什么，而不是一句泛泛的失败。
        assert "OSError" in tool_msg

    async def test_an_unexpected_facts_failure_becomes_a_structured_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """事实收集阶段同样受这条不变量管束。

        `_facts_path_read` 直接调 `path_facts` 而没有自己的 try；一次 I/O 错误
        在那里逃出去，同样会终止整次运行。读取工具的处理器不再调 `path_facts`，
        因此这里的替换只会命中事实收集那一次。
        """
        repo = make_repo(tmp_path)
        h = Harness(
            repo,
            [
                [tool("c1", "repo.read", path="src/calc.py"), finish("tool_calls")],
                [text("The lookup failed; I will say so."), finish()],
            ],
        )

        def boom(path: str) -> object:
            raise OSError("facts collection blew up")

        monkeypatch.setattr(h.workspace, "path_facts", boom)
        outcome = await h.service.run("Read the calculator")

        assert outcome.status is RunStatus.SUCCEEDED
        tool_msg = next(m.content for m in h.model.requests_seen[-1].messages if m.role == "tool")
        assert '"error_code": "internal"' in tool_msg
        assert "OSError" in tool_msg

    async def test_an_unexpected_failure_after_a_write_is_effect_unknown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """写入途中炸开时，"无副作用"是不能假设的。

        产生副作用的工具族在重新抛出前已把这次调用记成 EFFECT_UNKNOWN；流水线必须
        照实上报，让运行停下来等人，而不是带着无法证明的磁盘状态继续往下写。
        """
        repo = make_repo(tmp_path)
        h = Harness(
            repo,
            [
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
                [text("Edited."), finish()],
            ],
        )
        original_apply = h.workspace.apply_edit

        async def apply_then_blow_up(*args: object, **kwargs: object) -> object:
            await original_apply(*args, **kwargs)
            # 写入已经落盘，之后的事情才炸开——这正是"副作用可能已发生"的形状。
            raise RuntimeError("evidence registration blew up")

        monkeypatch.setattr(h.workspace, "apply_edit", apply_then_blow_up)

        outcome = await h.service.run("Fix add()")

        assert outcome.status is RunStatus.EFFECT_UNKNOWN
        assert outcome.stop_reason is StopReason.EFFECT_UNKNOWN
        executions = await h.store.load_executions(outcome.run_id)
        assert executions and all(
            record.effect_state is EffectState.EFFECT_UNKNOWN for record in executions
        )
