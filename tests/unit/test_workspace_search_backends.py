"""搜索：ripgrep 和纯 Python 回退必须结果一致，并且都要跳过 vendor/build 目录，使
搜索在真实仓库中保持可用。"""

import shutil
from pathlib import Path

import pytest

from haven.adapters.workspace_fs import IGNORED_DIRS, FsWorkspace

HAS_RG = shutil.which("rg") is not None
requires_rg = pytest.mark.skipif(not HAS_RG, reason="ripgrep not installed")


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "calc.py").write_text("def add(a, b):\n    return a - b  # BUG\n")
    (tmp_path / "src" / "util.py").write_text("# helper: BUG tracking\nVALUE = 1\n")
    (tmp_path / "README.md").write_text("# demo\nno issues here\n")

    # 绝不能搜索的 vendor 噪声
    (tmp_path / "node_modules" / "left-pad").mkdir(parents=True)
    (tmp_path / "node_modules" / "left-pad" / "index.js").write_text("// BUG in vendor code\n")
    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "site.py").write_text("# BUG in a dependency\n")
    (tmp_path / "src" / "__pycache__").mkdir()
    (tmp_path / "src" / "__pycache__" / "calc.cpython-312.pyc").write_text("BUG\n")
    return tmp_path


def paths_of(result: object) -> list[tuple[str, int]]:
    return [(m.path, m.line_number) for m in result.matches]  # type: ignore[attr-defined]


class TestIgnoredDirectories:
    async def test_walk_backend_skips_vendor_dirs(self, repo: Path) -> None:
        workspace = FsWorkspace(repo, use_ripgrep=False)
        result = await workspace.search("BUG", ".", 50)
        found = {m.path for m in result.matches}
        assert found == {"src/calc.py", "src/util.py"}

    @requires_rg
    async def test_ripgrep_backend_skips_vendor_dirs(self, repo: Path) -> None:
        workspace = FsWorkspace(repo, use_ripgrep=True)
        result = await workspace.search("BUG", ".", 50)
        found = {m.path for m in result.matches}
        assert found == {"src/calc.py", "src/util.py"}

    def test_ignore_list_covers_the_usual_suspects(self) -> None:
        assert {"node_modules", ".venv", "__pycache__", "dist", "build", "target"} <= IGNORED_DIRS


class TestBackendParity:
    @requires_rg
    async def test_backends_agree_on_a_tree_without_gitignore(self, repo: Path) -> None:
        with_rg = await FsWorkspace(repo, use_ripgrep=True).search("BUG", ".", 50)
        without_rg = await FsWorkspace(repo, use_ripgrep=False).search("BUG", ".", 50)
        assert paths_of(with_rg) == paths_of(without_rg)
        assert [m.line for m in with_rg.matches] == [m.line for m in without_rg.matches]

    @requires_rg
    async def test_backends_agree_on_regex_and_subdirectory_scope(self, repo: Path) -> None:
        for pattern, path in (("def .*", "src"), ("^VALUE", "."), ("BUG|issues", ".")):
            with_rg = await FsWorkspace(repo, use_ripgrep=True).search(pattern, path, 50)
            without_rg = await FsWorkspace(repo, use_ripgrep=False).search(pattern, path, 50)
            assert paths_of(with_rg) == paths_of(without_rg), f"{pattern!r} in {path!r}"

    @requires_rg
    async def test_backends_agree_when_there_are_no_matches(self, repo: Path) -> None:
        with_rg = await FsWorkspace(repo, use_ripgrep=True).search("zzz_absent", ".", 50)
        without_rg = await FsWorkspace(repo, use_ripgrep=False).search("zzz_absent", ".", 50)
        assert with_rg.matches == without_rg.matches == ()


class TestGitignoreAwareness:
    @requires_rg
    async def test_ripgrep_honours_gitignore(self, repo: Path) -> None:
        (repo / ".gitignore").write_text("generated/\n")
        (repo / "generated").mkdir()
        (repo / "generated" / "out.py").write_text("# BUG in generated code\n")

        result = await FsWorkspace(repo, use_ripgrep=True).search("BUG", ".", 50)
        assert all(not m.path.startswith("generated/") for m in result.matches)


class TestRipgrepHardening:
    @requires_rg
    async def test_pattern_starting_with_dash_is_not_a_flag(self, repo: Path) -> None:
        (repo / "src" / "flags.py").write_text("value = -1  # negative\n")
        result = await FsWorkspace(repo, use_ripgrep=True).search("-1", ".", 50)
        assert any(m.path == "src/flags.py" for m in result.matches)

    @requires_rg
    async def test_results_stay_inside_the_workspace(self, repo: Path) -> None:
        result = await FsWorkspace(repo, use_ripgrep=True).search(".", ".", 50)
        for match in result.matches:
            assert not match.path.startswith("/")
            assert ".." not in match.path

    @requires_rg
    async def test_result_cap_is_enforced(self, repo: Path) -> None:
        (repo / "many.txt").write_text("hit\n" * 500)
        result = await FsWorkspace(repo, use_ripgrep=True).search("hit", ".", 10)
        assert len(result.matches) == 10
        assert result.truncated

    async def test_missing_ripgrep_falls_back_silently(self, repo: Path) -> None:
        workspace = FsWorkspace(repo, use_ripgrep=True)
        workspace._ripgrep = "/nonexistent/rg"
        result = await workspace.search("BUG", ".", 50)
        assert {m.path for m in result.matches} == {"src/calc.py", "src/util.py"}


class TestFilesScannedHonesty:
    """`files_scanned` 的契约是"检查过的文件数，包括没有匹配的文件"。

    ripgrep 的计数来自它退出时打印的 `--stats`；搜索一旦触到结果上限就被提前掐断，
    那一行永远等不到。此时旧实现拿"有匹配的文件数"冒充扫描总数——而触顶正是常态
    （max_results 默认 50）。
    """

    @requires_rg
    async def test_a_truncated_ripgrep_search_reports_unknown_not_a_lower_bound(
        self, repo: Path
    ) -> None:
        workspace = FsWorkspace(repo, use_ripgrep=True)
        result = await workspace.search("BUG", ".", 2)
        assert result.truncated
        assert len(result.matches) == 2
        # 不是"有匹配的文件数"：那个数只是覆盖范围的下界。
        assert result.files_scanned is None

    @requires_rg
    async def test_a_complete_ripgrep_search_reports_the_real_count(self, repo: Path) -> None:
        workspace = FsWorkspace(repo, use_ripgrep=True)
        result = await workspace.search("BUG", ".", 50)
        assert not result.truncated
        # 仓库里 3 个可搜索文本文件，其中 2 个有匹配——计数必须包含没有匹配的那个。
        assert result.files_scanned == 3
        assert result.files_scanned != len({m.path for m in result.matches})

    async def test_the_walk_backend_reports_the_files_it_actually_examined(
        self, repo: Path
    ) -> None:
        workspace = FsWorkspace(repo, use_ripgrep=False)
        result = await workspace.search("BUG", ".", 50)
        assert result.files_scanned == 3
        # 截断时也报告真实检视数，而不是 None：它确实知道自己读到哪儿。
        truncated = await workspace.search("BUG", ".", 2)
        assert truncated.truncated
        assert truncated.files_scanned is not None
        assert truncated.files_scanned >= 1
