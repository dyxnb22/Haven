"""FsWorkspace.preview_patch / apply_patch: the multi-file transaction.

The preview simulates the whole patch in memory (later operations see earlier
effects) and plans *net* per-file effects; apply stages every write, then
commits writes-before-removals with a journaled rollback, so a failure leaves
either the original tree (clean rollback) or a per-file-classifiable partial
state (PatchRollbackError).
"""

import os
from pathlib import Path

import pytest

from haven.adapters import workspace_fs
from haven.adapters.workspace_fs import FsWorkspace
from haven.domain.digest import sha256_text
from haven.ports.workspace import PatchOpSpec, PatchRollbackError, WorkspaceError


def make_ws(tmp_path: Path) -> FsWorkspace:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.py").write_text("def a():\n    return 1\n")
    (repo / "src" / "b.py").write_text("def b():\n    return 2\n")
    return FsWorkspace(repo)


def reads_for(ws: FsWorkspace, *paths: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in paths:
        facts = ws.path_facts(path)
        assert facts.digest is not None
        out[path] = facts.digest
    return out


class TestPreviewSimulation:
    async def test_multi_file_plan_collects_all_preimages_and_one_diff(
        self, tmp_path: Path
    ) -> None:
        ws = make_ws(tmp_path)
        plan = await ws.preview_patch(
            (
                PatchOpSpec(kind="edit", path="src/a.py", old="return 1", new="return 10"),
                PatchOpSpec(kind="edit", path="src/b.py", old="return 2", new="return 20"),
                PatchOpSpec(kind="create", path="src/c.py", content="def c():\n    return 3\n"),
            ),
            reads_for(ws, "src/a.py", "src/b.py"),
        )
        assert set(plan.preimages) == {"src/a.py", "src/b.py"}
        assert [e.path for e in plan.effects] == ["src/a.py", "src/b.py", "src/c.py"]
        assert "return 10" in plan.diff and "return 20" in plan.diff and "def c" in plan.diff
        assert plan.final_contents["src/c.py"].startswith("def c")

    async def test_later_operations_see_earlier_effects(self, tmp_path: Path) -> None:
        ws = make_ws(tmp_path)
        plan = await ws.preview_patch(
            (
                PatchOpSpec(kind="edit", path="src/a.py", old="return 1", new="return 42"),
                PatchOpSpec(kind="edit", path="src/a.py", old="return 42", new="return 43"),
            ),
            reads_for(ws, "src/a.py"),
        )
        assert plan.final_contents["src/a.py"] == "def a():\n    return 43\n"
        assert len(plan.effects) == 1, "two edits of one file are one net effect"

    async def test_a_move_plans_as_a_provable_delete_plus_create(self, tmp_path: Path) -> None:
        ws = make_ws(tmp_path)
        plan = await ws.preview_patch(
            (PatchOpSpec(kind="move", src="src/a.py", dest="src/renamed.py"),),
            {},
        )
        shapes = {e.path: e.tool_shape for e in plan.effects}
        assert shapes == {"src/a.py": "repo.delete", "src/renamed.py": "repo.create"}
        # Each end carries its own proof: the delete a preimage, the create an
        # expected postimage — no ambiguous mid-move window exists for a patch.
        by_path = {e.path: e for e in plan.effects}
        assert by_path["src/a.py"].preimage_digest
        assert by_path["src/renamed.py"].expected_postimage == sha256_text(
            "def a():\n    return 1\n"
        )

    async def test_editing_a_file_created_by_the_patch_needs_no_read(self, tmp_path: Path) -> None:
        ws = make_ws(tmp_path)
        plan = await ws.preview_patch(
            (
                PatchOpSpec(kind="create", path="src/new.py", content="X = 1\n"),
                PatchOpSpec(kind="edit", path="src/new.py", old="X = 1", new="X = 2"),
            ),
            {},
        )
        assert plan.final_contents["src/new.py"] == "X = 2\n"

    async def test_editing_an_unread_existing_file_is_refused(self, tmp_path: Path) -> None:
        ws = make_ws(tmp_path)
        with pytest.raises(WorkspaceError) as err:
            await ws.preview_patch(
                (PatchOpSpec(kind="edit", path="src/a.py", old="return 1", new="return 2"),),
                {},
            )
        assert err.value.code == "invalid_arguments"
        assert "repo.read" in str(err.value)

    async def test_a_stale_read_is_refused(self, tmp_path: Path) -> None:
        ws = make_ws(tmp_path)
        with pytest.raises(WorkspaceError) as err:
            await ws.preview_patch(
                (PatchOpSpec(kind="edit", path="src/a.py", old="return 1", new="return 2"),),
                {"src/a.py": "d-not-the-current-digest"},
            )
        assert err.value.code == "stale_preimage"

    async def test_editing_a_file_deleted_earlier_in_the_patch_is_refused(
        self, tmp_path: Path
    ) -> None:
        ws = make_ws(tmp_path)
        with pytest.raises(WorkspaceError) as err:
            await ws.preview_patch(
                (
                    PatchOpSpec(kind="delete", path="src/a.py"),
                    PatchOpSpec(kind="edit", path="src/a.py", old="x", new="y"),
                ),
                reads_for(ws, "src/a.py"),
            )
        assert err.value.code == "not_found"

    async def test_creating_over_an_existing_file_is_refused(self, tmp_path: Path) -> None:
        ws = make_ws(tmp_path)
        with pytest.raises(WorkspaceError) as err:
            await ws.preview_patch(
                (PatchOpSpec(kind="create", path="src/a.py", content="new\n"),),
                {},
            )
        assert err.value.code == "invalid_arguments"

    async def test_a_patch_that_changes_nothing_is_refused(self, tmp_path: Path) -> None:
        ws = make_ws(tmp_path)
        with pytest.raises(WorkspaceError) as err:
            await ws.preview_patch(
                (
                    PatchOpSpec(kind="create", path="src/tmp.py", content="x\n"),
                    PatchOpSpec(kind="delete", path="src/tmp.py"),
                ),
                {},
            )
        assert "changes nothing" in str(err.value)


class TestApply:
    async def test_applies_all_files_and_verifies_postimages(self, tmp_path: Path) -> None:
        ws = make_ws(tmp_path)
        plan = await ws.preview_patch(
            (
                PatchOpSpec(kind="edit", path="src/a.py", old="return 1", new="return 10"),
                PatchOpSpec(kind="create", path="src/c.py", content="C = 1\n"),
                PatchOpSpec(kind="delete", path="src/b.py"),
            ),
            reads_for(ws, "src/a.py", "src/b.py"),
        )
        outcomes = await ws.apply_patch(plan)

        root = ws.root
        assert (root / "src" / "a.py").read_text() == "def a():\n    return 10\n"
        assert (root / "src" / "c.py").read_text() == "C = 1\n"
        assert not (root / "src" / "b.py").exists()
        by_path = {o.path: o for o in outcomes}
        assert by_path["src/a.py"].postimage_digest == sha256_text("def a():\n    return 10\n")
        assert by_path["src/b.py"].postimage_digest == ""

    async def test_stale_preimage_applies_nothing(self, tmp_path: Path) -> None:
        ws = make_ws(tmp_path)
        plan = await ws.preview_patch(
            (
                PatchOpSpec(kind="edit", path="src/a.py", old="return 1", new="return 10"),
                PatchOpSpec(kind="create", path="src/c.py", content="C = 1\n"),
            ),
            reads_for(ws, "src/a.py"),
        )
        (ws.root / "src" / "a.py").write_text("def a():\n    return 999\n")
        with pytest.raises(WorkspaceError) as err:
            await ws.apply_patch(plan)
        assert err.value.code == "stale_preimage"
        assert not (ws.root / "src" / "c.py").exists(), "nothing may land on a stale patch"

    async def test_an_appeared_create_target_applies_nothing(self, tmp_path: Path) -> None:
        ws = make_ws(tmp_path)
        plan = await ws.preview_patch(
            (
                PatchOpSpec(kind="edit", path="src/a.py", old="return 1", new="return 10"),
                PatchOpSpec(kind="create", path="src/c.py", content="C = 1\n"),
            ),
            reads_for(ws, "src/a.py"),
        )
        (ws.root / "src" / "c.py").write_text("someone else got here first\n")
        with pytest.raises(WorkspaceError) as err:
            await ws.apply_patch(plan)
        assert err.value.code == "stale_preimage"
        assert (ws.root / "src" / "a.py").read_text() == "def a():\n    return 1\n"

    async def test_a_mid_commit_failure_rolls_back_every_applied_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ws = make_ws(tmp_path)
        plan = await ws.preview_patch(
            (
                PatchOpSpec(kind="edit", path="src/a.py", old="return 1", new="return 10"),
                PatchOpSpec(kind="edit", path="src/b.py", old="return 2", new="return 20"),
            ),
            reads_for(ws, "src/a.py", "src/b.py"),
        )

        real_replace = os.replace
        calls = {"n": 0}

        def failing_replace(src: object, dst: object) -> None:
            calls["n"] += 1
            if calls["n"] == 2:  # the second file's commit
                raise OSError("disk full")
            real_replace(src, dst)  # type: ignore[arg-type]

        monkeypatch.setattr(workspace_fs.os, "replace", failing_replace)
        with pytest.raises(WorkspaceError) as err:
            await ws.apply_patch(plan)
        assert "rolled back" in str(err.value)
        assert (ws.root / "src" / "a.py").read_text() == "def a():\n    return 1\n", (
            "the first file's committed write must be undone"
        )
        assert (ws.root / "src" / "b.py").read_text() == "def b():\n    return 2\n"

    async def test_a_failed_rollback_surfaces_as_a_partial_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the commit AND the compensation both fail, the caller must see
        PatchRollbackError (-> unknown effect), never a clean failure."""
        ws = make_ws(tmp_path)
        plan = await ws.preview_patch(
            (
                PatchOpSpec(kind="edit", path="src/a.py", old="return 1", new="return 10"),
                PatchOpSpec(kind="edit", path="src/b.py", old="return 2", new="return 20"),
            ),
            reads_for(ws, "src/a.py", "src/b.py"),
        )

        real_replace = os.replace
        calls = {"n": 0}

        def failing_replace(src: object, dst: object) -> None:
            calls["n"] += 1
            if calls["n"] >= 2:  # second commit fails; rollback's write fails too
                raise OSError("disk full")
            real_replace(src, dst)  # type: ignore[arg-type]

        monkeypatch.setattr(workspace_fs.os, "replace", failing_replace)
        with pytest.raises(PatchRollbackError):
            await ws.apply_patch(plan)
