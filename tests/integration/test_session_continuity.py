"""A follow-up turn continues the same conversation, not a blank run.

Haven ran one goal and stopped; asking again started a fresh RunContext with no
memory. A session now carries the prior transcript forward so a follow-up sees
what the first turn did (Phase 2). Durable-run semantics are unchanged: each
turn is still its own Run with its own checkpoint and budget.
"""

from pathlib import Path

from haven.contracts.events import RunCreated
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

        # The first request had no prior conversation; the follow-up request
        # carries the first turn's answer and the new instruction.
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
        """A follow-up is new work: its step budget is not the prior turn's
        remainder."""
        turns = [
            [tool("c1", "repo.read", path="src/calc.py"), finish("tool_calls")],
            [text("done"), finish()],
            [text("follow-up done"), finish()],
        ]
        h = Harness(make_repo(tmp_path), turns)
        first = await h.service.run("Read it")
        second = await h.service.continue_run(first.run_id, "Anything else?")
        # The follow-up ran its own step, not continuing the first turn's count.
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
        """A follow-up must not graft a run's transcript onto another repo."""
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
        """The second turn's run diff is run-scoped: it must not re-report the
        first turn's edit."""
        from haven.contracts.events import DiffPreview

        repo = make_repo(tmp_path)
        turns = [
            # Turn 1 fully satisfies the gate (edit + diff + check), so it ends
            # cleanly and does not nudge into the turns meant for turn 2.
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
            # Turn 2 (continue): just diff, no new edit.
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
