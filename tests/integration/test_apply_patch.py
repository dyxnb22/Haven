"""repo.apply_patch through the full channel: one approval, one transaction.

A multi-file change used to be N sequential edits — N approvals, N chances to
go stale, no whole-change review. The patch tool carries the entire diff into
a single digest-bound approval and commits atomically (ADR 0019).
"""

from pathlib import Path

from haven.application.approvals import ApprovalResponder
from haven.contracts.events import ApprovalRequested, ToolCompleted
from haven.domain.approval import ApprovalRequest
from haven.domain.enums import ApprovalDecision, PermissionMode, RunStatus
from tests.integration.harness import Harness, finish, make_repo, text, tool


def patch_ops(*ops: dict) -> list[dict]:
    return list(ops)


class TestPatchJourney:
    async def test_multi_file_patch_lands_with_one_approval(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        turns = [
            [tool("c1", "repo.read", path="src/calc.py"), finish("tool_calls")],
            [
                tool(
                    "c2",
                    "repo.apply_patch",
                    operations=patch_ops(
                        {
                            "kind": "edit",
                            "path": "src/calc.py",
                            "old_string": "return a - b  # BUG: should be +",
                            "new_string": "return a + b",
                        },
                        {"kind": "create", "path": "src/util.py", "content": "HELPER = True\n"},
                        {"kind": "move", "src": "README.md", "dest": "docs.md"},
                    ),
                    summary="fix add, add helper, rename readme",
                ),
                finish("tool_calls"),
            ],
            [tool("c3", "repo.diff"), finish("tool_calls")],
            [tool("c4", "repo.check", recipe_id="verify-calc"), finish("tool_calls")],
            [text("Patched."), finish()],
        ]
        h = Harness(repo, turns)
        outcome = await h.service.run("Fix add and reorganize")

        assert outcome.status is RunStatus.SUCCEEDED
        assert "return a + b" in (repo / "src" / "calc.py").read_text()
        assert (repo / "src" / "util.py").read_text() == "HELPER = True\n"
        assert (repo / "docs.md").is_file() and not (repo / "README.md").exists()

        approvals = [
            e for e in h.sink.events_of("approval.requested") if isinstance(e, ApprovalRequested)
        ]
        patch_approvals = [a for a in approvals if a.tool_name == "repo.apply_patch"]
        assert len(patch_approvals) == 1, "the whole patch is one approval"
        # The card shows the entire reviewable diff.
        assert "return a + b" in patch_approvals[0].preview
        assert "HELPER" in patch_approvals[0].preview

    async def test_the_result_reports_net_file_effects(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        turns = [
            [tool("c1", "repo.read", path="src/calc.py"), finish("tool_calls")],
            [
                tool(
                    "c2",
                    "repo.apply_patch",
                    operations=patch_ops(
                        {
                            "kind": "edit",
                            "path": "src/calc.py",
                            "old_string": "return a - b  # BUG: should be +",
                            "new_string": "return a + b",
                        },
                    ),
                ),
                finish("tool_calls"),
            ],
            [text("Done (unverified)."), finish()],
        ]
        h = Harness(repo, turns)
        await h.service.run("Fix add")

        completed = [
            e
            for e in h.sink.events_of("tool.completed")
            if isinstance(e, ToolCompleted) and e.tool_name == "repo.apply_patch"
        ]
        assert completed and completed[0].status == "ok"


class TestPatchRefusals:
    async def test_editing_an_unread_file_is_refused(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        turns = [
            [
                tool(
                    "c1",
                    "repo.apply_patch",
                    operations=patch_ops(
                        {
                            "kind": "edit",
                            "path": "src/calc.py",
                            "old_string": "return a - b  # BUG: should be +",
                            "new_string": "return a + b",
                        },
                    ),
                ),
                finish("tool_calls"),
            ],
            [text("Could not."), finish()],
        ]
        h = Harness(repo, turns)
        await h.service.run("Fix add")

        completed = [
            e
            for e in h.sink.events_of("tool.completed")
            if isinstance(e, ToolCompleted) and e.tool_name == "repo.apply_patch"
        ]
        assert completed and completed[0].error_code == "invalid_arguments"
        assert "return a + b" not in (repo / "src" / "calc.py").read_text()

    async def test_a_protected_path_anywhere_denies_the_whole_patch(self, tmp_path: Path) -> None:
        from haven.contracts.events import PolicyDecided

        repo = make_repo(tmp_path)
        turns = [
            [tool("c1", "repo.read", path="src/calc.py"), finish("tool_calls")],
            [
                tool(
                    "c2",
                    "repo.apply_patch",
                    operations=patch_ops(
                        {
                            "kind": "edit",
                            "path": "src/calc.py",
                            "old_string": "return a - b  # BUG: should be +",
                            "new_string": "return a + b",
                        },
                        {"kind": "create", "path": ".haven.toml", "content": "[owned]\n"},
                    ),
                ),
                finish("tool_calls"),
            ],
            [text("Denied, stopping."), finish()],
        ]
        h = Harness(repo, turns)
        await h.service.run("Fix add and take over the config")

        denies = [
            e
            for e in h.sink.events_of("policy.decided")
            if isinstance(e, PolicyDecided) and e.decision == "deny"
        ]
        assert any(d.reason_code == "protected_path" for d in denies)
        assert "return a + b" not in (repo / "src" / "calc.py").read_text(), (
            "one bad op must sink the whole patch, not just itself"
        )

    async def test_read_only_mode_denies_the_patch(self, tmp_path: Path) -> None:
        from haven.contracts.events import PolicyDecided

        repo = make_repo(tmp_path)
        turns = [
            [
                tool(
                    "c1",
                    "repo.apply_patch",
                    operations=patch_ops(
                        {"kind": "create", "path": "note.txt", "content": "hi\n"},
                    ),
                ),
                finish("tool_calls"),
            ],
            [text("Read-only; cannot write."), finish()],
        ]
        h = Harness(repo, turns, mode=PermissionMode.READ_ONLY)
        await h.service.run("Write a note")

        denies = [
            e
            for e in h.sink.events_of("policy.decided")
            if isinstance(e, PolicyDecided) and e.decision == "deny"
        ]
        assert any(d.reason_code == "read_only_mode" for d in denies)
        assert not (repo / "note.txt").exists()


class _TamperingApprover(ApprovalResponder):
    """Approves everything, but mutates a file first — the TOCTOU window."""

    def __init__(self, repo: Path, target: str) -> None:
        self._repo = repo
        self._target = target

    async def respond(self, request: ApprovalRequest) -> ApprovalDecision:
        if request.tool_name == "repo.apply_patch":
            path = self._repo / self._target
            path.write_text(path.read_text() + "# changed while you decided\n")
        return ApprovalDecision.APPROVED


class TestPatchToctou:
    async def test_a_file_changed_during_approval_fails_the_whole_patch_closed(
        self, tmp_path: Path
    ) -> None:
        repo = make_repo(tmp_path)
        turns = [
            [tool("c1", "repo.read", path="src/calc.py"), finish("tool_calls")],
            [
                tool(
                    "c2",
                    "repo.apply_patch",
                    operations=patch_ops(
                        {
                            "kind": "edit",
                            "path": "src/calc.py",
                            "old_string": "return a - b  # BUG: should be +",
                            "new_string": "return a + b",
                        },
                        {"kind": "create", "path": "src/util.py", "content": "HELPER = True\n"},
                    ),
                ),
                finish("tool_calls"),
            ],
            [text("Stale, stopping."), finish()],
        ]
        h = Harness(repo, turns, approver=_TamperingApprover(repo, "src/calc.py"))
        await h.service.run("Fix add")

        completed = [
            e
            for e in h.sink.events_of("tool.completed")
            if isinstance(e, ToolCompleted) and e.tool_name == "repo.apply_patch"
        ]
        assert completed and completed[0].error_code == "stale_preimage"
        assert not (repo / "src" / "util.py").exists(), "nothing may land from a stale patch"
