"""Security regression: every workspace escape must fail closed."""

import os
from pathlib import Path

import pytest

from haven.adapters.workspace_fs import FsWorkspace
from haven.ports.workspace import WorkspaceError


@pytest.fixture()
def workspace(tmp_path: Path) -> FsWorkspace:
    (tmp_path / "repo").mkdir()
    (tmp_path / "repo" / "src").mkdir()
    (tmp_path / "repo" / "src" / "app.py").write_text("print('hello')\n")
    (tmp_path / "repo" / ".git").mkdir()
    (tmp_path / "repo" / ".git" / "config").write_text("[core]\n")
    (tmp_path / "repo" / ".haven.toml").write_text("[budget]\n")
    (tmp_path / "outside.txt").write_text("secret outside workspace\n")
    return FsWorkspace(tmp_path / "repo")


class TestPathEscapes:
    def test_absolute_path_rejected(self, workspace: FsWorkspace) -> None:
        facts = workspace.path_facts("/etc/passwd")
        assert not facts.within_workspace

    def test_home_path_rejected(self, workspace: FsWorkspace) -> None:
        facts = workspace.path_facts("~/.ssh/id_rsa")
        assert not facts.within_workspace

    def test_parent_traversal_rejected(self, workspace: FsWorkspace) -> None:
        facts = workspace.path_facts("../outside.txt")
        assert not facts.within_workspace

    def test_nested_traversal_rejected(self, workspace: FsWorkspace) -> None:
        facts = workspace.path_facts("src/../../outside.txt")
        assert not facts.within_workspace

    def test_null_byte_rejected(self, workspace: FsWorkspace) -> None:
        facts = workspace.path_facts("src/app.py\x00.txt")
        assert not facts.within_workspace

    async def test_read_outside_raises(self, workspace: FsWorkspace) -> None:
        with pytest.raises(WorkspaceError) as exc:
            await workspace.read_file("../outside.txt", 1, 100)
        assert exc.value.code == "denied"

    async def test_symlink_escape_rejected(self, workspace: FsWorkspace, tmp_path: Path) -> None:
        os.symlink(tmp_path / "outside.txt", workspace.root / "sneaky_link")
        facts = workspace.path_facts("sneaky_link")
        assert not facts.within_workspace
        with pytest.raises(WorkspaceError):
            await workspace.read_file("sneaky_link", 1, 100)

    async def test_symlink_inside_workspace_not_editable(self, workspace: FsWorkspace) -> None:
        os.symlink(workspace.root / "src" / "app.py", workspace.root / "alias.py")
        # resolves inside the workspace, so reads work through the real path
        facts = workspace.path_facts("alias.py")
        assert facts.within_workspace
        assert facts.normalized == "src/app.py"


class TestProtectedPaths:
    def test_git_dir_protected(self, workspace: FsWorkspace) -> None:
        facts = workspace.path_facts(".git/config")
        assert facts.is_protected

    def test_haven_toml_protected(self, workspace: FsWorkspace) -> None:
        facts = workspace.path_facts(".haven.toml")
        assert facts.is_protected

    async def test_read_protected_raises(self, workspace: FsWorkspace) -> None:
        with pytest.raises(WorkspaceError) as exc:
            await workspace.read_file(".git/config", 1, 100)
        assert exc.value.code == "denied"

    async def test_edit_protected_raises(self, workspace: FsWorkspace) -> None:
        with pytest.raises(WorkspaceError) as exc:
            await workspace.apply_edit(".haven.toml", "[budget]", "[hacked]", "any")
        assert exc.value.code == "denied"

    async def test_list_hides_protected_entries(self, workspace: FsWorkspace) -> None:
        result = await workspace.list_dir(".", 100)
        names = [e.name for e in result.entries]
        assert ".git" not in names
        assert ".haven.toml" not in names
