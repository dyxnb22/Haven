"""End-to-end agent journeys driven by the ScriptedModel (fully offline)."""

import asyncio
from pathlib import Path

import pytest

from haven.contracts.events import PolicyDecided, RunFinished, ToolCompleted
from haven.contracts.model import ModelEvent
from haven.domain.approval import ApprovalRequest
from haven.domain.budget import Budget
from haven.domain.enums import ApprovalDecision, PermissionMode, RunStatus, StopReason

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
        # a full proposal -> policy -> execution -> completion chain per call
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
        # transient deltas never hit the journal
        assert "assistant.delta" not in stored_kinds


class TestEditJourney:
    TURNS: list[list[ModelEvent]] = []

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

        # two approvals: the edit and the check
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

        # nothing was written, so a final answer is acceptable
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
            # model claims success three times without diff/check evidence
            [text("Done! I fixed it."), finish()],
            [text("Really, it is done."), finish()],
            [text("Trust me, done."), finish()],
        ]
        h = Harness(repo, turns)
        outcome = await h.service.run("Fix the bug in add()")

        assert outcome.status is RunStatus.STOPPED
        assert outcome.stop_reason is StopReason.EVIDENCE_MISSING
        # the model was nudged with the gate failure before stopping
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
                    new_string="return a * b",  # wrong fix
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
        # the new file shows up in the run diff as a pure addition
        diffs = h.sink.events_of("diff.preview")
        assert any("tests/test_add.py" in getattr(d, "preview", "") for d in diffs)
        # creation went through approval like any other write
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
        # never even reached approval
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
        """The failure message must be actionable enough for the next turn."""
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
        # every request after the plan was set carries it
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
        """No writes means nothing to review; a plain answer is still fine."""
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
        assert outcome.status is RunStatus.SUCCEEDED  # final answer, no writes
        denies = [
            e
            for e in h.sink.events_of("policy.decided")
            if isinstance(e, PolicyDecided) and e.decision == "deny"
        ]
        assert len(denies) == 1
        assert denies[0].reason_code == "read_only_mode"
        # no approval was even requested for a denied action
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
        # the secret never entered the model transcript
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
        """The file changes between human approval and execution (TOCTOU)."""
        repo = make_repo(tmp_path)

        class TamperingApprover:
            async def respond(self, request: ApprovalRequest) -> ApprovalDecision:
                # while the human "thinks", the file changes under us
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
        # the tampered content is untouched by the stale edit
        assert (repo / "src" / "calc.py").read_text() == "everything changed\n"


class TestBudgetsAndStops:
    async def test_step_budget_stops_run(self, tmp_path: Path) -> None:
        turns: list[list[ModelEvent]] = [
            [tool("c1", "repo.list", path="."), finish("tool_calls")],
        ]
        h = Harness(make_repo(tmp_path), turns, budget=Budget(max_steps=1), repeat_last=True)
        outcome = await h.service.run("Explore forever")
        assert outcome.status is RunStatus.STOPPED
        assert outcome.stop_reason is StopReason.STEP_BUDGET_EXHAUSTED

    async def test_stuck_loop_detected(self, tmp_path: Path) -> None:
        turns: list[list[ModelEvent]] = [
            [tool("c1", "repo.search", pattern="nothing_here", path="."), finish("tool_calls")],
        ]
        h = Harness(make_repo(tmp_path), turns, repeat_last=True)
        outcome = await h.service.run("Search in circles")
        assert outcome.status is RunStatus.STOPPED
        assert outcome.stop_reason is StopReason.NO_PROGRESS
        assert outcome.steps < 12  # detector fires before the step budget

    async def test_provider_error_fails_run(self, tmp_path: Path) -> None:
        turns: list[list[ModelEvent]] = [
            [tool("c1", "repo.list", path="."), finish("tool_calls")],
            # script exhausted on the next call -> ProviderError
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
        h.service._model = SlowModel()  # type: ignore[attr-defined]  # noqa: SLF001

        task = asyncio.create_task(h.service.run("Slow task"))
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        finished = h.sink.events_of("run.finished")
        assert len(finished) == 1
        assert isinstance(finished[0], RunFinished)
        assert finished[0].status == "cancelled"
        run = await h.store.get_run(finished[0].run_id)
        assert run is not None
        assert run.status is RunStatus.CANCELLED
