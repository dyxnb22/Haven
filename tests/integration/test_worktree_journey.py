"""集成测试：基于 Git 投机工作树隔离的任务执行与合流（ADR 0041）。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from haven.adapters.providers.scripted import ScriptedModel
from haven.application.approvals import AutoApprover
from haven.bootstrap import build_services
from haven.domain.enums import PermissionMode, RunStatus
from tests.integration.harness import CollectingSink, finish, text, tool, usage


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@haven.dev"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Haven Test"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (repo / "README.md").write_text("# Demo\n", encoding="utf-8")
    verify_cmd = f'argv = ["{sys.executable}", "-c", "import calc; assert calc.add(2, 3) == 5"]'
    (repo / ".haven.toml").write_text(f"[recipes.verify]\n{verify_cmd}\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo


class TestWorktreeJourney:
    async def test_successful_run_reconciles_to_main_workspace(self, git_repo: Path) -> None:
        """任务成功完成时，变更从临时工作树合流回主工作区，随后工作树被完全清理。"""
        fixed_calc = "def add(a, b):\n    return a + b\n"
        model = ScriptedModel(
            turns=[
                [
                    tool("c0", "repo.read", path="calc.py"),
                    finish("tool_calls"),
                    usage(30, 10),
                ],
                [
                    tool(
                        "c1",
                        "repo.edit",
                        path="calc.py",
                        old_string="def add(a, b):\n    return a - b\n",
                        new_string=fixed_calc,
                    ),
                    finish("tool_calls"),
                    usage(50, 10),
                ],
                [
                    tool("c2", "repo.diff"),
                    finish("tool_calls"),
                    usage(20, 10),
                ],
                [
                    tool("c3", "repo.check", recipe_id="verify"),
                    finish("tool_calls"),
                    usage(20, 10),
                ],
                [
                    text("Bug fixed in worktree and verified!"),
                    finish("stop"),
                    usage(30, 10),
                ],
            ]
        )
        sink = CollectingSink()
        services = await build_services(
            git_repo,
            mode=PermissionMode.INTERACTIVE,
            approvals=AutoApprover("approve_all"),
            sinks=[sink],
            model=model,
            store_path=git_repo.parent / "wt_test1.db",
            worktree=True,
        )

        assert services.ephemeral_worktree is not None
        wt_path = services.ephemeral_worktree.worktree_path
        assert wt_path.is_dir()

        # 运行前主仓库是旧代码
        assert (git_repo / "calc.py").read_text() == "def add(a, b):\n    return a - b\n"

        try:
            outcome = await services.run_service.run("fix calc bug")
            assert outcome.status == RunStatus.SUCCEEDED

            # 运行后、合流前：工作树内已修改，主仓库仍保持未被污染
            assert (wt_path / "calc.py").read_text() == fixed_calc
            assert (git_repo / "calc.py").read_text() == "def add(a, b):\n    return a - b\n"

            # 执行合流
            reconciled = await services.reconcile_worktree_if_succeeded(outcome)
            assert "calc.py" in reconciled
            assert (git_repo / "calc.py").read_text() == fixed_calc
        finally:
            await services.close()

        # close() 确保临时工作树被完全移除，零脏文件残留
        assert not wt_path.exists()

    async def test_failed_run_leaves_main_workspace_unpolluted(self, git_repo: Path) -> None:
        """任务未成功时，主工作区不接受任何合流，零污染。"""
        model = ScriptedModel(
            turns=[
                [
                    tool("c0", "repo.read", path="calc.py"),
                    finish("tool_calls"),
                    usage(30, 10),
                ],
                [
                    tool(
                        "c1",
                        "repo.edit",
                        path="calc.py",
                        old_string="def add(a, b):\n    return a - b\n",
                        new_string="def add(a, b):\n    broken garbage\n",
                    ),
                    finish("tool_calls"),
                    usage(50, 10),
                ],
                [
                    # 模拟遇到错误停止
                    finish("error"),
                    usage(30, 10),
                ],
            ]
        )
        services = await build_services(
            git_repo,
            mode=PermissionMode.INTERACTIVE,
            approvals=AutoApprover("approve_all"),
            sinks=[],
            model=model,
            store_path=git_repo.parent / "wt_test1.db",
            worktree=True,
        )

        wt_path = services.ephemeral_worktree.worktree_path

        try:
            outcome = await services.run_service.run("attempt broken fix")
            assert outcome.status != RunStatus.SUCCEEDED

            # 尝试合流被拒绝
            reconciled = await services.reconcile_worktree_if_succeeded(outcome)
            assert reconciled == []
            # 主仓库代码完好无损
            assert (git_repo / "calc.py").read_text() == "def add(a, b):\n    return a - b\n"
        finally:
            await services.close()

        assert not wt_path.exists()

    def test_cli_worktree_requires_git_repo(self, tmp_path: Path) -> None:
        """CLI --worktree 在非 git 仓库上优雅报错并以 EXIT_USAGE 退出。"""
        from typer.testing import CliRunner

        from haven.interfaces.cli import app

        runner = CliRunner()
        non_git = tmp_path / "nongit"
        non_git.mkdir()
        result = runner.invoke(
            app,
            ["run", "do task", "-w", str(non_git), "--worktree"],
            env={"HAVEN_API_KEY": "fake"},
        )
        assert result.exit_code == 2
        assert "worktree isolation requires a git repository" in result.stdout
