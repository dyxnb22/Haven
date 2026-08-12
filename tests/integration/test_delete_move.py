"""repo.delete and repo.move go through the full approval + evidence channel.

Both are effect tools: they require approval, pin the file's content so a
concurrent change fails closed, and record their change to the evidence ledger
so the Evidence Gate holds them to the same standard as an edit (Phase 3b).
"""

from pathlib import Path

from haven.application.approvals import AutoApprover
from haven.contracts.events import ApprovalRequested, ToolCompleted
from haven.domain.approval import ApprovalRequest
from haven.domain.enums import ApprovalDecision, RunStatus
from tests.integration.harness import Harness, finish, make_repo, text, tool


def completed(h: Harness) -> list[ToolCompleted]:
    return [e for e in h.sink.events_of("tool.completed") if isinstance(e, ToolCompleted)]


class TestDelete:
    async def test_approved_delete_removes_the_file(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        assert (repo / "README.md").exists()
        turns = [
            [tool("c1", "repo.delete", path="README.md"), finish("tool_calls")],
            [text("Deleted the readme."), finish()],
        ]
        h = Harness(repo, turns)
        await h.service.run("Remove the readme")

        assert not (repo / "README.md").exists()
        assert completed(h)[0].status == "ok"

    async def test_rejected_delete_keeps_the_file(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        turns = [
            [tool("c1", "repo.delete", path="README.md"), finish("tool_calls")],
            [text("Kept it."), finish()],
        ]
        h = Harness(repo, turns, approver=AutoApprover("reject_all"))
        await h.service.run("Remove the readme")

        assert (repo / "README.md").exists()
        assert completed(h)[0].error_code == "approval_rejected"

    async def test_deleting_a_protected_path_is_denied(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        (repo / ".git").mkdir(exist_ok=True)
        (repo / ".git" / "config").write_text("[core]\n")
        turns = [
            [tool("c1", "repo.delete", path=".git/config"), finish("tool_calls")],
            [text("Refused."), finish()],
        ]
        h = Harness(repo, turns)
        await h.service.run("Delete git config")

        assert (repo / ".git" / "config").exists()
        assert completed(h)[0].error_code == "denied"

    async def test_delete_preview_shows_the_path(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        turns = [
            [tool("c1", "repo.delete", path="README.md"), finish("tool_calls")],
            [text("done"), finish()],
        ]
        h = Harness(repo, turns)
        await h.service.run("Remove the readme")
        requested = h.sink.events_of("approval.requested")[0]
        assert isinstance(requested, ApprovalRequested)
        assert "delete README.md" in requested.summary


class TestMove:
    async def test_approved_move_renames_the_file(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        turns = [
            [tool("c1", "repo.move", src="README.md", dest="docs/README.md"), finish("tool_calls")],
            [text("Moved it."), finish()],
        ]
        h = Harness(repo, turns)
        await h.service.run("Move the readme into docs/")

        assert not (repo / "README.md").exists()
        assert (repo / "docs" / "README.md").exists()
        assert completed(h)[0].status == "ok"

    async def test_move_onto_an_existing_file_is_refused(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        (repo / "OTHER.md").write_text("taken\n")
        turns = [
            [tool("c1", "repo.move", src="README.md", dest="OTHER.md"), finish("tool_calls")],
            [text("Could not move."), finish()],
        ]
        h = Harness(repo, turns)
        await h.service.run("Move readme onto other")

        assert (repo / "README.md").exists()
        assert (repo / "OTHER.md").read_text() == "taken\n"
        assert completed(h)[0].status == "error"

    async def test_move_out_of_the_workspace_is_denied(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        turns = [
            [tool("c1", "repo.move", src="README.md", dest="../escaped.md"), finish("tool_calls")],
            [text("Refused."), finish()],
        ]
        h = Harness(repo, turns)
        await h.service.run("Move readme outside")

        assert (repo / "README.md").exists()
        assert not (tmp_path / "escaped.md").exists()
        assert completed(h)[0].error_code == "denied"


class TestContentIsPinnedAtApproval:
    """A file that changes between approval and execution must not be deleted or
    moved on its stale content — the TOCTOU guarantee the tool descriptions make.
    """

    async def test_delete_refuses_a_file_that_changed_after_approval(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)

        class MutatingApprover(AutoApprover):
            def __init__(self) -> None:
                super().__init__("approve_all")

            async def respond(self, request: ApprovalRequest) -> ApprovalDecision:
                # Change the file after it was proposed but before it executes.
                (repo / "README.md").write_text("changed out from under the run\n")
                return await super().respond(request)

        turns = [
            [tool("c1", "repo.delete", path="README.md"), finish("tool_calls")],
            [text("stale"), finish()],
        ]
        h = Harness(repo, turns, approver=MutatingApprover())
        await h.service.run("Delete the readme")

        assert (repo / "README.md").exists(), "a changed file must not be deleted on stale content"
        assert completed(h)[0].error_code == "stale_preimage"


class TestDeleteNeedsEvidence:
    async def test_a_delete_then_bare_success_claim_is_rejected(self, tmp_path: Path) -> None:
        """A deletion is a change, so the gate demands a diff and passing check."""
        repo = make_repo(tmp_path)
        turns = [
            [tool("c1", "repo.delete", path="README.md"), finish("tool_calls")],
            [text("All done, no need to verify."), finish()],
        ]
        h = Harness(repo, turns, repeat_last=True)
        outcome = await h.service.run("Remove the readme and finish")
        assert outcome.status is not RunStatus.SUCCEEDED
