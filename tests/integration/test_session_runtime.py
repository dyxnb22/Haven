"""Phase 3 session runtime: steering a running agent, rewind, fork.

Steering must land only at a turn boundary (the tool channel is never
interrupted), rewind must be fail-closed compensation, and fork is a
follow-up from any checkpointed run, not just the latest.
"""

from pathlib import Path

from haven.contracts.events import EventEnvelope, Notice, RunCreated
from haven.domain.enums import RunStatus
from tests.integration.harness import Harness, finish, make_repo, text, tool


class _SteerOnToolCompleted:
    """Event sink that queues steering when a given tool call completes —
    i.e. mid-turn, exactly where a user typing during a run lands."""

    def __init__(self, call_id: str, message: str) -> None:
        self.call_id = call_id
        self.message = message
        self.service = None  # attached after Harness construction

    async def emit(self, envelope: EventEnvelope) -> None:
        event = envelope.event
        if (
            event.kind == "tool.completed"
            and getattr(event, "call_id", "") == self.call_id
            and self.service is not None
        ):
            accepted = await self.service.steer(self.message)
            assert accepted, "steering during an active run must be accepted"


class TestSteering:
    async def test_steering_lands_as_a_user_message_at_the_next_turn(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        turns = [
            [tool("c1", "repo.read", path="src/calc.py"), finish("tool_calls")],
            [tool("c2", "repo.read", path="README.md"), finish("tool_calls")],
            [text("Done reading."), finish()],
        ]
        h = Harness(repo, turns)
        # Queued while step 1's tool call is finishing (mid-turn), so it must
        # be delivered at the next boundary — request 2 — and not sooner.
        steerer = _SteerOnToolCompleted("c1", "also check the README licensing section")
        h.emitter._sinks.append(steerer)  # noqa: SLF001 - test-only wiring
        steerer.service = h.service

        outcome = await h.service.run("Look around the repository")
        assert outcome.status is RunStatus.SUCCEEDED

        first = "\n".join(m.content for m in h.model.requests_seen[0].messages)
        second = "\n".join(m.content for m in h.model.requests_seen[1].messages)
        assert "licensing section" not in first
        assert "User update: also check the README licensing section" in second

        queued = [e for e in h.sink.events_of("steer.queued")]
        assert len(queued) == 1
        delivered = [
            e
            for e in h.sink.events_of("notice")
            if isinstance(e, Notice) and "steering delivered" in e.message
        ]
        assert len(delivered) == 1

    async def test_steering_without_an_active_run_is_refused(self, tmp_path: Path) -> None:
        h = Harness(make_repo(tmp_path), [[text("hi"), finish()]])
        assert not await h.service.steer("anything")

    async def test_undelivered_steering_does_not_leak_into_the_next_run(
        self, tmp_path: Path
    ) -> None:
        """Steering queued on the final turn has no later boundary; it must
        be dropped (visible in the journal), not delivered to a future run."""
        repo = make_repo(tmp_path)
        turns = [
            [text("First run answer."), finish()],
            [text("Second run answer."), finish()],
        ]
        h = Harness(repo, turns)

        class _SteerOnFinalAnswer:
            service = h.service

            async def emit(self, envelope: EventEnvelope) -> None:
                event = envelope.event
                if event.kind == "model.completed" and "First run answer" in getattr(
                    event, "text", ""
                ):
                    # Queued during the run's very last turn: there is no
                    # later boundary, so it can never be delivered.
                    await self.service.steer("too late for this run")

        steerer = _SteerOnFinalAnswer()
        h.emitter._sinks.append(steerer)  # noqa: SLF001

        await h.service.run("First goal")
        steerer.service = None  # type: ignore[assignment]
        await h.service.run("Second goal, fresh run")

        for request in h.model.requests_seen[1:]:
            joined = "\n".join(m.content for m in request.messages)
            assert "too late for this run" not in joined


class TestRewind:
    async def test_rewind_restores_edits_and_removes_creations(self, tmp_path: Path) -> None:
        from haven.application.recovery_service import RecoveryService

        repo = make_repo(tmp_path)
        original = (repo / "src" / "calc.py").read_text()
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
                tool("c3", "repo.create", path="src/new_helper.py", content="X = 1\n"),
                finish("tool_calls"),
            ],
            [tool("c4", "repo.diff"), finish("tool_calls")],
            [tool("c5", "repo.check", recipe_id="verify-calc"), finish("tool_calls")],
            [text("Fixed."), finish()],
        ]
        h = Harness(repo, turns)
        outcome = await h.service.run("Fix add() and add a helper")
        assert outcome.status is RunStatus.SUCCEEDED

        recovery = RecoveryService(h.store, h.workspace)
        report = await recovery.rewind(outcome.run_id)
        assert report.rewound, report.blockers
        assert (repo / "src" / "calc.py").read_text() == original
        assert not (repo / "src" / "new_helper.py").exists(), (
            "a file the run created must be removed by rewind"
        )

    async def test_rewind_blocks_when_a_file_changed_after_the_run(self, tmp_path: Path) -> None:
        from haven.application.recovery_service import RecoveryService

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
            [tool("c3", "repo.diff"), finish("tool_calls")],
            [tool("c4", "repo.check", recipe_id="verify-calc"), finish("tool_calls")],
            [text("Fixed."), finish()],
        ]
        h = Harness(repo, turns)
        outcome = await h.service.run("Fix add()")

        # Someone edits the file after the run: rewind must refuse to clobber.
        (repo / "src" / "calc.py").write_text("def add(a, b):\n    return a + b + 0\n")
        recovery = RecoveryService(h.store, h.workspace)
        report = await recovery.rewind(outcome.run_id)
        assert not report.rewound
        assert any("changed since this run" in b for b in report.blockers)
        assert "a + b + 0" in (repo / "src" / "calc.py").read_text(), "nothing was overwritten"


class TestFork:
    async def test_continue_from_an_older_run_forks_the_session(self, tmp_path: Path) -> None:
        """Fork semantics: a follow-up can branch from ANY checkpointed run,
        not only the latest; the branch records its parent."""
        repo = make_repo(tmp_path)
        turns = [
            [text("First answer: ALPHA."), finish()],
            [text("Second answer: BETA."), finish()],
            [text("Branched from the first answer."), finish()],
        ]
        h = Harness(repo, turns)
        first = await h.service.run("Initial question")
        second = await h.service.continue_run(first.run_id, "Refine it")
        fork = await h.service.continue_run(first.run_id, "Take a different direction")

        assert fork.run_id not in (first.run_id, second.run_id)
        created = [
            e
            for e in h.sink.events_of("run.created")
            if isinstance(e, RunCreated) and e.run_id == fork.run_id
        ]
        assert created and created[0].parent_run_id == first.run_id

        # The fork's transcript branches from run 1: it carries ALPHA but not
        # the sibling's BETA.
        last_request = "\n".join(m.content for m in h.model.requests_seen[-1].messages)
        assert "ALPHA" in last_request
        assert "BETA" not in last_request
