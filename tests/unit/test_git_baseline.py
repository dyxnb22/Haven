"""Git 基线的解析： porcelain 输出必须被如实解释。

`capture_git_baseline` 记录"运行开始前就已存在未提交变更的文件"。这个集合错了不会
报错，只会让之后的证据悄悄建立在另一份文件状态上——所以这里逐条钉住解析规则。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from haven.adapters.git_baseline import _dirty_paths, capture_git_baseline


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for args in (
        ["init"],
        ["config", "user.email", "test@haven.dev"],
        ["config", "user.name", "Haven Test"],
    ):
        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)


def _commit(path: Path, message: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", message], cwd=path, check=True, capture_output=True)


class TestDirtyPathParsing:
    def test_an_ordinary_entry_keeps_its_path_intact(self) -> None:
        # `-z` 条目形如 `XY <路径>`
        assert _dirty_paths(" M normal.txt\0") == ("normal.txt",)

    def test_a_leading_space_is_not_eaten(self) -> None:
        """整段输出曾被 `.strip()`，于是**第一行**的 `XY ` 前缀被削掉。

        `line[3:]` 接着啃掉文件名头一个字符：`normal.txt` 记成 `ormal.txt`。第一行
        和其余行行为不一致，所以这个 bug 在只有单个脏文件时最难发现。
        """
        porcelain = " M first.txt\0 M second.txt\0"
        assert _dirty_paths(porcelain) == ("first.txt", "second.txt")

    def test_paths_with_surrounding_whitespace_survive(self) -> None:
        """`-z` 不给路径加引号，所以空白原样保留。

        按行解析时 git 会输出 `" leading.txt"`（带引号），还得自己反转义八进制字节；
        换成 `-z` 之后这条路整个消失了。
        """
        porcelain = " M  leading.txt\0 M trailing.txt \0"
        assert _dirty_paths(porcelain) == (" leading.txt", "trailing.txt ")

    def test_a_rename_records_both_paths(self) -> None:
        """重命名条目多带一个原始路径：`XY <新路径>\\0<原路径>\\0`。"""
        porcelain = "R  new.py\0old.py\0"
        assert _dirty_paths(porcelain) == ("new.py", "old.py")

    def test_a_copy_also_records_both_paths(self) -> None:
        assert _dirty_paths("C  copy.py\0source.py\0") == ("copy.py", "source.py")

    def test_untracked_and_deleted_entries_are_kept(self) -> None:
        porcelain = "?? untracked.py\0 D gone.py\0"
        assert _dirty_paths(porcelain) == ("untracked.py", "gone.py")

    def test_empty_output_yields_nothing(self) -> None:
        assert _dirty_paths("") == ()
        assert _dirty_paths("\0") == ()


class TestCaptureAgainstARealRepo:
    def test_a_clean_repo_has_no_dirty_files(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        _commit(repo, "init")

        baseline = capture_git_baseline_sync(repo)

        assert baseline.is_repo is True
        assert baseline.branch
        assert baseline.commit
        assert baseline.dirty_files == ()

    def test_dirty_files_are_reported_byte_for_byte(self, tmp_path: Path) -> None:
        """记录下来的路径必须和磁盘上的名字逐字节相同。"""
        repo = tmp_path / "repo"
        _init_repo(repo)
        (repo / "normal.txt").write_text("one\n", encoding="utf-8")
        (repo / "trailing.txt ").write_text("two\n", encoding="utf-8")
        (repo / " leading.txt").write_text("three\n", encoding="utf-8")
        _commit(repo, "init")

        (repo / "normal.txt").write_text("changed\n", encoding="utf-8")
        (repo / "trailing.txt ").write_text("changed\n", encoding="utf-8")

        baseline = capture_git_baseline_sync(repo)

        assert set(baseline.dirty_files) == {"normal.txt", "trailing.txt "}
        # 每一个都真的对应磁盘上的一个文件
        for name in baseline.dirty_files:
            assert (repo / name).is_file(), name

    def test_a_non_repo_reports_is_repo_false(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        assert capture_git_baseline_sync(plain).is_repo is False


def capture_git_baseline_sync(root: Path):
    """同步包装，供非 async 测试使用。"""
    import asyncio

    return asyncio.run(capture_git_baseline(root))
