"""A write performed by repo.exec must not escape the Evidence Gate.

The gate used to key off an evidence ledger that only repo.edit / repo.create
wrote to, so a sandboxed command could modify the workspace with no ledger
entry and the run would be reported as a success that made no changes. ADR 0012
closed this by attributing process writes to the ledger.
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
        # The write is now attributed, so the gate demands a diff and a passing
        # check after it — which this run never produced.
        assert not (
            outcome.status is RunStatus.SUCCEEDED and outcome.stop_reason is StopReason.FINAL_ANSWER
        ), "a run that rewrote a file was accepted as having made no changes"

    async def test_a_run_whose_exec_changes_nothing_is_unaffected(self, tmp_path: Path) -> None:
        """Read-only commands must not be recorded as writes, or every safe
        exec would falsely trip the gate."""
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
        """Landlock cannot carve `.git` out of a writable workspace, and the
        change snapshot excludes protected paths from normal attribution — so a
        process touching them was previously invisible. It must now surface as
        an error in the audit trail (ADR 0017)."""
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
