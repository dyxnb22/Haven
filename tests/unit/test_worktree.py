"""临时 Git 投机工作树单元测试（ADR 0041）。"""

from pathlib import Path

import pytest

from haven.adapters.worktree import (
    cleanup_ephemeral_worktree,
    create_ephemeral_worktree,
    is_git_repo,
    reconcile_worktree_to_main,
)
from haven.ports.workspace import WorkspaceError


class TestWorktreeLifecycle:
    async def test_non_git_repo_rejected(self, tmp_path: Path) -> None:
        non_git = tmp_path / "non_git"
        non_git.mkdir()
        assert not await is_git_repo(non_git)
        with pytest.raises(WorkspaceError) as exc:
            await create_ephemeral_worktree(non_git, "run-1")
        assert exc.value.code == "invalid_arguments"

    async def test_create_and_cleanup_worktree(self, git_repo_clean: Path) -> None:
        assert await is_git_repo(git_repo_clean)
        wt = await create_ephemeral_worktree(git_repo_clean, "run-test-1")
        assert wt.worktree_path.is_dir()
        assert (wt.worktree_path / "calc.py").is_file()

        # 在工作树中修改文件
        (wt.worktree_path / "calc.py").write_text("def add(a, b):\n    return a + b + 1\n")

        # 主仓库未受污染
        assert (git_repo_clean / "calc.py").read_text() == "def add(a, b):\n    return a + b\n"

        # 清理工作树
        await cleanup_ephemeral_worktree(wt)
        assert not wt.worktree_path.exists()
        assert (git_repo_clean / "calc.py").read_text() == "def add(a, b):\n    return a + b\n"

    async def test_reconcile_to_main_on_success(self, git_repo_clean: Path) -> None:
        wt = await create_ephemeral_worktree(git_repo_clean, "run-test-2")
        try:
            # 1. 修改现有文件
            (wt.worktree_path / "calc.py").write_text("def add(a, b):\n    return 42\n")
            # 2. 创建新文件
            (wt.worktree_path / "new_module.py").write_text("MSG = 'hello'\n")
            # 3. 删除文件
            (wt.worktree_path / "README.md").unlink()

            reconciled = await reconcile_worktree_to_main(wt)
            assert "calc.py" in reconciled
            assert "new_module.py" in reconciled
            assert "README.md" in reconciled

            # 验证主仓库状态已同步
            assert (git_repo_clean / "calc.py").read_text() == "def add(a, b):\n    return 42\n"
            assert (git_repo_clean / "new_module.py").read_text() == "MSG = 'hello'\n"
            assert not (git_repo_clean / "README.md").exists()
        finally:
            await cleanup_ephemeral_worktree(wt)

    async def test_create_worktree_inherits_dirty_uncommitted_changes(
        self, git_repo_clean: Path
    ) -> None:
        # 在主仓库中制造一个未提交的修改
        (git_repo_clean / "calc.py").write_text("def add(a, b):\n    return a + b + 999\n")

        wt = await create_ephemeral_worktree(git_repo_clean, "run-test-dirty")
        try:
            # 验证工作树中继承了主仓库未提交的草稿改动
            assert (
                wt.worktree_path / "calc.py"
            ).read_text() == "def add(a, b):\n    return a + b + 999\n"
        finally:
            await cleanup_ephemeral_worktree(wt)

    async def test_a_failed_apply_is_reported_not_swallowed(
        self, git_repo_clean: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """草稿搬不进工作树时必须报错，而不是悄悄返回一个没对齐的工作树。

        整个这一段的目的就是"让推测环境与开发者的草稿对齐"；悄悄返回一个没对齐的
        工作树，之后所有的编辑前像和证据都会建立在另一份文件状态上，而没有任何迹象
        表明它不是你眼前那份。

        正常流程里 `git apply` 几乎不会失败（工作树与 diff 同源），所以这里直接伪造
        一个非零退出——要断言的是"失败了会说出来"这个映射，不是 git 的行为。
        """
        import asyncio as _asyncio

        import haven.adapters.worktree as worktree_module

        class _FailedApply:
            returncode = 1

            async def communicate(self, input: bytes = b"") -> tuple[bytes, bytes]:
                return b"", b"error: patch does not apply"

        real = _asyncio.create_subprocess_exec

        async def fake_exec(program: str, *args: str, **kwargs: object):  # type: ignore[no-untyped-def]
            if program == "git" and args and args[0] == "apply":
                return _FailedApply()
            return await real(program, *args, **kwargs)  # type: ignore[arg-type]

        (git_repo_clean / "calc.py").write_text("def add(a, b):\n    return a + b + 999\n")
        monkeypatch.setattr(worktree_module.asyncio, "create_subprocess_exec", fake_exec)

        with pytest.raises(WorkspaceError) as exc:
            await create_ephemeral_worktree(git_repo_clean, "run-apply-fails")
        assert exc.value.code == "internal"
        assert "uncommitted changes" in str(exc.value)
        # 失败的工作树不能留在原地
        assert not (git_repo_clean / ".haven" / "worktrees" / "run-apply-fails").exists()
