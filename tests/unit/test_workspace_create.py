"""repo.create: new files only, atomic, and attributed to this run's diff."""

from pathlib import Path

import pytest

from haven.adapters.workspace_fs import MAX_CREATE_BYTES, FsWorkspace
from haven.domain.digest import sha256_text
from haven.ports.workspace import WorkspaceError


@pytest.fixture()
def workspace(tmp_path: Path) -> FsWorkspace:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    return FsWorkspace(tmp_path)


class TestCreate:
    async def test_creates_a_new_file(self, workspace: FsWorkspace) -> None:
        outcome = await workspace.apply_create("tests/test_calc.py", "assert True\n")
        target = workspace.root / "tests" / "test_calc.py"
        assert target.read_text() == "assert True\n"
        assert outcome.postimage_digest == sha256_text("assert True\n")
        assert outcome.preimage_digest == ""

    async def test_creates_parent_directories(self, workspace: FsWorkspace) -> None:
        await workspace.apply_create("a/b/c/deep.py", "x = 1\n")
        assert (workspace.root / "a" / "b" / "c" / "deep.py").is_file()

    async def test_preview_shows_the_whole_file_as_additions(self, workspace: FsWorkspace) -> None:
        preview = await workspace.preview_create("notes.md", "line one\nline two\n")
        assert preview.insertions == 2
        assert preview.deletions == 0
        assert "+line one" in preview.diff
        assert "/dev/null" in preview.diff

    async def test_preview_does_not_write(self, workspace: FsWorkspace) -> None:
        await workspace.preview_create("notes.md", "hello\n")
        assert not (workspace.root / "notes.md").exists()

    async def test_existing_file_is_refused(self, workspace: FsWorkspace) -> None:
        with pytest.raises(WorkspaceError) as exc:
            await workspace.preview_create("src/calc.py", "overwritten\n")
        assert exc.value.code == "invalid_arguments"
        assert "repo.edit" in str(exc.value)

    async def test_existing_directory_is_refused(self, workspace: FsWorkspace) -> None:
        with pytest.raises(WorkspaceError) as exc:
            await workspace.preview_create("src", "nope\n")
        assert exc.value.code == "invalid_arguments"

    async def test_oversized_content_refused(self, workspace: FsWorkspace) -> None:
        with pytest.raises(WorkspaceError) as exc:
            await workspace.preview_create("big.txt", "x" * (MAX_CREATE_BYTES + 1))
        assert exc.value.code == "invalid_arguments"

    async def test_created_file_appears_in_run_diff(self, workspace: FsWorkspace) -> None:
        await workspace.apply_create("tests/test_calc.py", "assert add(1, 2) == 3\n")
        run_diff = await workspace.run_diff()
        assert run_diff.files == ("tests/test_calc.py",)
        assert run_diff.insertions == 1
        assert run_diff.deletions == 0
        assert "+assert add(1, 2) == 3" in run_diff.diff

    async def test_create_then_edit_is_allowed(self, workspace: FsWorkspace) -> None:
        created = await workspace.apply_create("mod.py", "VALUE = 1\n")
        outcome = await workspace.apply_edit(
            "mod.py", "VALUE = 1", "VALUE = 2", created.postimage_digest
        )
        assert (workspace.root / "mod.py").read_text() == "VALUE = 2\n"
        # the run diff still compares against "file did not exist"
        run_diff = await workspace.run_diff()
        assert run_diff.files == ("mod.py",)
        assert "+VALUE = 2" in run_diff.diff
        assert "-VALUE = 1" not in run_diff.diff
        assert outcome.path == "mod.py"


class TestCreateSecurity:
    async def test_escape_is_denied(self, workspace: FsWorkspace) -> None:
        for path in ("../outside.py", "/tmp/evil.py", "~/evil.py"):
            with pytest.raises(WorkspaceError) as exc:
                await workspace.preview_create(path, "evil\n")
            assert exc.value.code == "denied"

    async def test_protected_paths_are_denied(self, workspace: FsWorkspace) -> None:
        for path in (".git/hooks/pre-commit", ".haven.toml", ".haven/config"):
            with pytest.raises(WorkspaceError) as exc:
                await workspace.preview_create(path, "evil\n")
            assert exc.value.code == "denied"

    async def test_nothing_is_written_when_denied(
        self, workspace: FsWorkspace, tmp_path: Path
    ) -> None:
        with pytest.raises(WorkspaceError):
            await workspace.apply_create("../outside.py", "evil\n")
        assert not (tmp_path.parent / "outside.py").exists()

    async def test_empty_path_refused(self, workspace: FsWorkspace) -> None:
        with pytest.raises(WorkspaceError):
            await workspace.preview_create(".", "evil\n")
