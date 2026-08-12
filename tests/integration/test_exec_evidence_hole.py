"""Can a write performed by repo.exec escape the Evidence Gate?

The gate keys off the evidence ledger, and only repo.edit / repo.create write
to it. If a sandboxed command can modify the workspace without any ledger
entry, a run can change files and still be reported as a success that made no
changes — which would hollow out the project's central claim.
"""

import sys
from pathlib import Path

import pytest

from haven.domain.enums import RunStatus, StopReason
from tests.integration.harness import Harness, finish, make_repo, text, tool


class TestExecWritesAreAttributed:
    @pytest.mark.xfail(
        strict=True,
        reason=(
            "Known hole: the sandbox permits workspace writes, but only repo.edit "
            "and repo.create write to the evidence ledger, so a file changed by "
            "repo.exec is invisible to the gate. Tracked as P0; strict=True so "
            "this flips to a failure the moment it is fixed."
        ),
    )
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
        assert not (
            outcome.status is RunStatus.SUCCEEDED and outcome.stop_reason is StopReason.FINAL_ANSWER
        ), "a run that rewrote a file was accepted as having made no changes"
