"""Run-scoped standing approval for identical repo.check calls (ADR 0025).

Approving a check once covers byte-identical re-runs within the same run —
the fix/verify loop's normal shape. Everything else keeps asking: different
recipes, write tools, and the next run.
"""

from pathlib import Path

from haven.application.approvals import ApprovalResponder
from haven.domain.approval import ApprovalRequest
from haven.domain.enums import ApprovalDecision, RunStatus
from tests.integration.harness import Harness, finish, make_repo, text, tool


class CountingApprover(ApprovalResponder):
    """Approves (or rejects) everything and remembers what it was asked."""

    def __init__(self, decision: ApprovalDecision = ApprovalDecision.APPROVED) -> None:
        self._decision = decision
        self.asked: list[str] = []

    async def respond(self, request: ApprovalRequest) -> ApprovalDecision:
        self.asked.append(request.summary)
        return self._decision


async def test_identical_check_reruns_ask_only_once(tmp_path: Path) -> None:
    # repo.diff between the checks mirrors a real fix/verify loop — and is
    # required for determinism: three *consecutive* identical calls with
    # identical results is precisely the no-progress condition, so without
    # something in between the stuck detector (threshold 3) stops the run.
    # It only ever passed because the check result carries duration_ms and
    # the milliseconds usually differed; that jitter was the 1-in-8 flake
    # recorded on 2026-08-13. The interleaved call resets the counter, so
    # this now tests standing approvals rather than the clock.
    repo = make_repo(tmp_path)
    turns = [
        [tool("c1", "repo.check", recipe_id="always-pass"), finish("tool_calls")],
        [tool("d1", "repo.diff"), finish("tool_calls")],
        [tool("c2", "repo.check", recipe_id="always-pass"), finish("tool_calls")],
        [tool("d2", "repo.diff"), finish("tool_calls")],
        [tool("c3", "repo.check", recipe_id="always-pass"), finish("tool_calls")],
        [text("Ran the check three times."), finish()],
    ]
    approver = CountingApprover()
    h = Harness(repo, turns, approver=approver)
    outcome = await h.service.run("run the check repeatedly")

    assert outcome.status is RunStatus.SUCCEEDED, outcome
    check_asks = [s for s in approver.asked if "check recipe" in s]
    assert len(check_asks) == 1, f"identical re-runs must not re-ask; asked: {approver.asked}"
    # The card discloses the standing scope at the moment of consent.
    assert "identical re-runs" in check_asks[0]

    # The journal still carries one approval row per execution...
    decided = h.sink.events_of("approval.decided")
    assert len(decided) == 3, f"approval.decided events: {decided}"
    # ...and the standing grants are announced, once per skipped ask.
    all_notices = [getattr(e, "message", "") for e in h.sink.events_of("notice")]
    notices = [m for m in all_notices if "standing approval" in m]
    assert len(notices) == 2, f"notices seen: {all_notices}"


async def test_a_different_recipe_asks_again(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    turns = [
        [tool("c1", "repo.check", recipe_id="always-pass"), finish("tool_calls")],
        [tool("c2", "repo.check", recipe_id="verify-calc"), finish("tool_calls")],
        [text("Two different checks."), finish()],
    ]
    approver = CountingApprover()
    h = Harness(repo, turns, approver=approver)
    await h.service.run("run two different checks")

    check_asks = [s for s in approver.asked if "check recipe" in s]
    assert len(check_asks) == 2, "a different recipe is a different consent"


async def test_rejection_never_arms_the_grant(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    turns = [
        [tool("c1", "repo.check", recipe_id="always-pass"), finish("tool_calls")],
        [tool("c2", "repo.check", recipe_id="always-pass"), finish("tool_calls")],
        [text("Tried twice."), finish()],
    ]
    approver = CountingApprover(ApprovalDecision.REJECTED)
    h = Harness(repo, turns, approver=approver)
    await h.service.run("try the check")

    check_asks = [s for s in approver.asked if "check recipe" in s]
    assert len(check_asks) == 2, "a rejected check must ask again, not inherit a grant"
    assert not h.sink.events_of("execution.started"), "nothing was ever authorized"


async def test_write_tools_always_re_ask(tmp_path: Path) -> None:
    """Two identical-shape edits to the same file still ask twice: standing
    grants exist only for repo.check, never for anything that writes."""
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
        [tool("c3", "repo.read", path="src/calc.py"), finish("tool_calls")],
        [
            tool(
                "c4",
                "repo.edit",
                path="src/calc.py",
                old_string="return a + b",
                new_string="return a + b  # verified",
            ),
            finish("tool_calls"),
        ],
        [tool("c5", "repo.diff"), finish("tool_calls")],
        [tool("c6", "repo.check", recipe_id="verify-calc"), finish("tool_calls")],
        [text("Edited twice."), finish()],
    ]
    approver = CountingApprover()
    h = Harness(repo, turns, approver=approver)
    outcome = await h.service.run("fix and annotate add()")

    assert outcome.status is RunStatus.SUCCEEDED
    edit_asks = [s for s in approver.asked if s.startswith("edit ")]
    assert len(edit_asks) == 2
