"""文件系统工作区适配器的行为测试。"""

import io
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from haven.adapters import workspace_fs
from haven.adapters.workspace_fs import (
    MAX_EDIT_FILE_BYTES,
    MAX_READ_BYTES,
    FsWorkspace,
    _scan_file,
)
from haven.domain.digest import sha256_bytes, sha256_text
from haven.ports.workspace import OriginalContent, WorkspaceError


class TestCaptureSnapshot:
    def test_lists_every_regular_file_by_digest(self, workspace: FsWorkspace, repo: Path) -> None:
        snapshot = workspace.capture_snapshot()
        assert snapshot.digests["src/calc.py"] == sha256_bytes(
            (repo / "src" / "calc.py").read_bytes()
        )
        assert "README.md" in snapshot.digests

    def test_text_files_keep_their_contents_for_diffing(self, workspace: FsWorkspace) -> None:
        snapshot = workspace.capture_snapshot()
        assert "return a - b" in snapshot.contents["src/calc.py"]

    def test_protected_and_ignored_paths_are_excluded(
        self, workspace: FsWorkspace, repo: Path
    ) -> None:
        (repo / ".git").mkdir()
        (repo / ".git" / "config").write_text("[core]\n")
        (repo / "__pycache__").mkdir()
        (repo / "__pycache__" / "x.pyc").write_bytes(b"\x00\x01")
        snapshot = workspace.capture_snapshot()
        assert not any(path.startswith(".git") for path in snapshot.digests)
        assert not any("__pycache__" in path for path in snapshot.digests)

    def test_binary_files_are_digested_but_not_kept_as_text(
        self, workspace: FsWorkspace, repo: Path
    ) -> None:
        (repo / "blob.bin").write_bytes(b"\x00\x01\x02\xff")
        snapshot = workspace.capture_snapshot()
        assert "blob.bin" in snapshot.digests
        assert "blob.bin" not in snapshot.contents

    def test_a_change_moves_the_digest(self, workspace: FsWorkspace, repo: Path) -> None:
        before = workspace.capture_snapshot()
        (repo / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        after = workspace.capture_snapshot()
        assert before.digests["src/calc.py"] != after.digests["src/calc.py"]

    def test_register_run_original_seeds_the_diff_only_if_absent(
        self, workspace: FsWorkspace, repo: Path
    ) -> None:
        workspace.register_run_original("src/calc.py", OriginalContent.captured("ORIGINAL"))
        workspace.register_run_original("src/calc.py", OriginalContent.captured("SECOND"))
        assert workspace.original_contents()["src/calc.py"].text == "ORIGINAL"


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "calc.py").write_text(
        "def add(a, b):\n    return a - b  # BUG\n\n\ndef sub(a, b):\n    return a - b\n"
    )
    (tmp_path / "README.md").write_text("# Demo\nA tiny calculator.\n")
    return tmp_path


@pytest.fixture()
def workspace(repo: Path) -> FsWorkspace:
    return FsWorkspace(repo)


class TestRead:
    async def test_read_full_file(self, workspace: FsWorkspace) -> None:
        result = await workspace.read_file("src/calc.py", 1, 100)
        assert "return a - b  # BUG" in result.content
        assert result.total_lines == 6
        assert not result.truncated
        assert result.digest

    async def test_read_window(self, workspace: FsWorkspace) -> None:
        result = await workspace.read_file("src/calc.py", 2, 1)
        assert result.content == "    return a - b  # BUG\n"
        assert result.truncated  # 还有更多行

    async def test_read_missing_file(self, workspace: FsWorkspace) -> None:
        with pytest.raises(WorkspaceError) as exc:
            await workspace.read_file("nope.py", 1, 10)
        assert exc.value.code == "not_found"

    async def test_read_binary_rejected(self, workspace: FsWorkspace) -> None:
        (workspace.root / "blob.bin").write_bytes(b"\x00\x01\x02\xff")
        with pytest.raises(WorkspaceError) as exc:
            await workspace.read_file("blob.bin", 1, 10)
        assert exc.value.code == "invalid_arguments"


class TestSearch:
    async def test_search_finds_matches(self, workspace: FsWorkspace) -> None:
        result = await workspace.search("BUG", ".", 50)
        assert len(result.matches) == 1
        assert result.matches[0].path == "src/calc.py"
        assert result.matches[0].line_number == 2

    async def test_search_result_cap(self, workspace: FsWorkspace) -> None:
        (workspace.root / "many.txt").write_text("hit\n" * 500)
        result = await workspace.search("hit", ".", 10)
        assert len(result.matches) == 10
        assert result.truncated

    async def test_invalid_regex_rejected(self, workspace: FsWorkspace) -> None:
        with pytest.raises(WorkspaceError) as exc:
            await workspace.search("([unclosed", ".", 10)
        assert exc.value.code == "invalid_arguments"

    async def test_binary_files_skipped(self, workspace: FsWorkspace) -> None:
        (workspace.root / "blob.bin").write_bytes(b"BUG\x00BUG")
        result = await workspace.search("BUG", ".", 50)
        assert all(m.path != "blob.bin" for m in result.matches)

    async def test_a_slow_search_degrades_on_its_deadline(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """模型的模式只做语法检查，因此搜索可能被构造得任意缓慢。遍历受墙上时间
        限制，而不只是受结果上限限制；超时会报告截断，而不是一直运行到完成。"""
        from haven.adapters import workspace_fs

        root = tmp_path / "repo"
        root.mkdir()
        for index in range(400):
            (root / f"f{index}.txt").write_text("needle\n" * 100)
        ws = FsWorkspace(root, use_ripgrep=False)
        # 已经超时：第一次截止时间检查就必须停止遍历。
        monkeypatch.setattr(workspace_fs, "RIPGREP_TIMEOUT_SECONDS", -1.0)

        started = time.monotonic()
        result = await ws.search("needle", ".", 10_000)
        elapsed = time.monotonic() - started

        assert result.truncated, "a search past its deadline must report truncation"
        assert elapsed < 5.0, f"the walk should have stopped promptly, took {elapsed:.1f}s"

    async def test_the_deadline_does_not_truncate_an_ordinary_search(
        self, workspace: FsWorkspace
    ) -> None:
        """时间限制不能将正常搜索变成部分结果。"""
        result = await workspace.search("BUG", ".", 50)
        assert not result.truncated
        assert len(result.matches) == 1


class TestList:
    async def test_list_directories_first(self, workspace: FsWorkspace) -> None:
        result = await workspace.list_dir(".", 100)
        names = [e.name for e in result.entries]
        assert names == ["src", "README.md"]

    async def test_list_cap(self, workspace: FsWorkspace) -> None:
        for i in range(30):
            (workspace.root / f"file{i:02}.txt").write_text("x")
        result = await workspace.list_dir(".", 5)
        assert len(result.entries) == 5
        assert result.truncated


class TestEdit:
    async def test_preview_then_apply(self, workspace: FsWorkspace) -> None:
        preview = await workspace.preview_edit("src/calc.py", "return a - b  # BUG", "return a + b")
        assert "+    return a + b" in preview.diff
        assert preview.insertions == 1
        assert preview.deletions == 1

        outcome = await workspace.apply_edit(
            "src/calc.py", "return a - b  # BUG", "return a + b", preview.preimage_digest
        )
        assert outcome.postimage_digest == preview.postimage_digest
        assert "return a + b" in (workspace.root / "src" / "calc.py").read_text()

    async def test_stale_preimage_fails_closed(self, workspace: FsWorkspace) -> None:
        preview = await workspace.preview_edit("src/calc.py", "return a - b  # BUG", "return a + b")
        # 其他人在审批和执行之间修改了文件
        (workspace.root / "src" / "calc.py").write_text("everything changed\n")
        with pytest.raises(WorkspaceError) as exc:
            await workspace.apply_edit(
                "src/calc.py", "return a - b  # BUG", "return a + b", preview.preimage_digest
            )
        assert exc.value.code == "stale_preimage"

    async def test_ambiguous_match_rejected(self, workspace: FsWorkspace) -> None:
        with pytest.raises(WorkspaceError) as exc:
            await workspace.preview_edit("src/calc.py", "return a - b", "return a + b")
        assert exc.value.code == "ambiguous_match"

    async def test_ambiguous_error_tells_the_model_how_to_recover(
        self, workspace: FsWorkspace
    ) -> None:
        with pytest.raises(WorkspaceError) as exc:
            await workspace.preview_edit("src/calc.py", "return a - b", "x")
        message = str(exc.value)
        assert "occurs 2 times" in message
        assert "occurrence=N" in message
        assert "replace_all=true" in message

    async def test_replace_all_changes_every_occurrence(self, workspace: FsWorkspace) -> None:
        preview = await workspace.preview_edit("src/calc.py", "a - b", "a + b", replace_all=True)
        await workspace.apply_edit(
            "src/calc.py", "a - b", "a + b", preview.preimage_digest, replace_all=True
        )
        content = (workspace.root / "src" / "calc.py").read_text()
        assert content.count("a + b") == 2
        assert "a - b" not in content

    async def test_occurrence_selects_one_match(self, workspace: FsWorkspace) -> None:
        preview = await workspace.preview_edit(
            "src/calc.py", "return a - b", "return a + b", occurrence=1
        )
        await workspace.apply_edit(
            "src/calc.py", "return a - b", "return a + b", preview.preimage_digest, occurrence=1
        )
        content = (workspace.root / "src" / "calc.py").read_text()
        # 只有第一个（有 bug 的 add）被修改；sub() 仍然执行减法
        assert content.count("return a + b") == 1
        assert content.count("return a - b") == 1
        assert content.index("return a + b") < content.index("return a - b")

    async def test_occurrence_two_selects_the_second_match(self, workspace: FsWorkspace) -> None:
        preview = await workspace.preview_edit(
            "src/calc.py", "return a - b", "return b - a", occurrence=2
        )
        await workspace.apply_edit(
            "src/calc.py", "return a - b", "return b - a", preview.preimage_digest, occurrence=2
        )
        content = (workspace.root / "src" / "calc.py").read_text()
        assert content.index("return a - b") < content.index("return b - a")

    async def test_occurrence_out_of_range_rejected(self, workspace: FsWorkspace) -> None:
        with pytest.raises(WorkspaceError) as exc:
            await workspace.preview_edit("src/calc.py", "return a - b", "x", occurrence=9)
        assert exc.value.code == "not_found"
        assert "only 2 time(s)" in str(exc.value)

    async def test_occurrence_and_replace_all_are_mutually_exclusive(
        self, workspace: FsWorkspace
    ) -> None:
        with pytest.raises(WorkspaceError) as exc:
            await workspace.preview_edit(
                "src/calc.py", "return a - b", "x", occurrence=1, replace_all=True
            )
        assert exc.value.code == "invalid_arguments"

    async def test_replace_all_still_binds_to_the_preimage(self, workspace: FsWorkspace) -> None:
        preview = await workspace.preview_edit("src/calc.py", "a - b", "a + b", replace_all=True)
        (workspace.root / "src" / "calc.py").write_text("changed under us\n")
        with pytest.raises(WorkspaceError) as exc:
            await workspace.apply_edit(
                "src/calc.py", "a - b", "a + b", preview.preimage_digest, replace_all=True
            )
        assert exc.value.code == "stale_preimage"

    async def test_missing_old_string_rejected(self, workspace: FsWorkspace) -> None:
        with pytest.raises(WorkspaceError) as exc:
            await workspace.preview_edit("src/calc.py", "no such text", "x")
        assert exc.value.code == "not_found"

    async def test_oversized_file_rejected(self, workspace: FsWorkspace) -> None:
        (workspace.root / "big.txt").write_text("x" * (MAX_EDIT_FILE_BYTES + 1))
        with pytest.raises(WorkspaceError) as exc:
            await workspace.preview_edit("big.txt", "x", "y")
        assert exc.value.code == "invalid_arguments"


class TestRunDiff:
    async def test_diff_only_covers_this_run(self, workspace: FsWorkspace) -> None:
        # 运行开始编辑前就存在的用户改动
        (workspace.root / "README.md").write_text("# Demo\nuser edited this before the run\n")

        preview = await workspace.preview_edit("src/calc.py", "return a - b  # BUG", "return a + b")
        await workspace.apply_edit(
            "src/calc.py", "return a - b  # BUG", "return a + b", preview.preimage_digest
        )

        run_diff = await workspace.run_diff()
        assert run_diff.files == ("src/calc.py",)
        assert "README.md" not in run_diff.diff

    async def test_multiple_edits_one_file_single_diff(self, workspace: FsWorkspace) -> None:
        p1 = await workspace.preview_edit("src/calc.py", "return a - b  # BUG", "return a + b")
        await workspace.apply_edit(
            "src/calc.py", "return a - b  # BUG", "return a + b", p1.preimage_digest
        )
        p2 = await workspace.preview_edit("src/calc.py", "def sub", "def subtract")
        await workspace.apply_edit("src/calc.py", "def sub", "def subtract", p2.preimage_digest)

        run_diff = await workspace.run_diff()
        assert run_diff.files == ("src/calc.py",)
        # diff 对比的是第一次编辑前的原始内容
        assert "-    return a - b  # BUG" in run_diff.diff
        assert "+def subtract(a, b):" in run_diff.diff

    async def test_postimage_digest_matches_content(self, workspace: FsWorkspace) -> None:
        preview = await workspace.preview_edit("src/calc.py", "# BUG", "# FIXED")
        outcome = await workspace.apply_edit(
            "src/calc.py", "# BUG", "# FIXED", preview.preimage_digest
        )
        current = (workspace.root / "src" / "calc.py").read_text()
        assert outcome.postimage_digest == sha256_text(current)

    async def test_edit_preserves_executable_mode(self, workspace: FsWorkspace) -> None:
        target = workspace.root / "src" / "calc.py"
        target.chmod(0o755)
        preview = await workspace.preview_edit("src/calc.py", "# BUG", "# FIXED")
        await workspace.apply_edit("src/calc.py", "# BUG", "# FIXED", preview.preimage_digest)
        assert target.stat().st_mode & 0o777 == 0o755

    async def test_diff_does_not_follow_a_replacement_symlink(
        self, workspace: FsWorkspace, tmp_path: Path
    ) -> None:
        preview = await workspace.preview_edit("src/calc.py", "# BUG", "# FIXED")
        await workspace.apply_edit("src/calc.py", "# BUG", "# FIXED", preview.preimage_digest)
        secret = tmp_path.parent / "outside-secret.txt"
        secret.write_text("DO-NOT-EXFILTRATE\n")
        target = workspace.root / "src" / "calc.py"
        target.unlink()
        target.symlink_to(secret)

        run_diff = await workspace.run_diff()
        assert "DO-NOT-EXFILTRATE" not in run_diff.diff


class TestStreamingLineSplit:
    """流式切行必须与 `str.splitlines(keepends=True)` 逐字节一致。

    行号是编辑前像和差异的一部分；悄悄漂移会让审计记录对不上文件。这里用 adversarial
    输入把等价性钉住，尤其是块边界——一个块的末尾可能是不完整的行，也可能是一个
    还不知道后面跟不跟 `\n` 的 `\r`。
    """

    TEXT = (
        "plain\n"
        "crlf\r\n"
        "lone cr\r"
        "form feed\f"
        "vertical tab\v"
        "file sep\x1c"
        "group sep\x1d"
        "record sep\x1e"
        "next line\x85"
        "line sep\u2028"
        "para sep\u2029"
        "no trailing boundary"
    )

    @pytest.mark.parametrize("chunk", [1, 2, 3, 5, 7, 13, 64, 4096])
    def test_matches_splitlines_at_every_chunk_size(self, chunk: int) -> None:
        expected = self.TEXT.splitlines(keepends=True)
        for size in range(1, chunk + 1):
            handle = io.BytesIO(self.TEXT.encode("utf-8"))
            got: list[str] = []
            with patch("haven.adapters.workspace_fs.READ_CHUNK_BYTES", size):
                _scan_file(handle, lambda number, line, sink=got: sink.append(line))
            assert got == expected, f"chunk size {size} diverged"

    def test_an_empty_file_has_no_lines(self) -> None:
        got: list[str] = []
        _scan_file(io.BytesIO(b""), lambda number, line: got.append(line))
        assert got == []

    def test_a_file_ending_in_crlf_is_not_double_counted(self) -> None:
        """`\r\n` 是一个边界，不是两个。"""
        got: list[str] = []
        _scan_file(io.BytesIO(b"a\r\nb\r\n"), lambda number, line: got.append(line))
        assert got == ["a\r\n", "b\r\n"]

    def test_a_cr_split_across_the_chunk_boundary_stays_one_boundary(self) -> None:
        """块尾的 `\r` 不能提前断开：下一个块开头是 `\n` 时它们是同一个边界。"""
        got: list[str] = []
        with patch("haven.adapters.workspace_fs.READ_CHUNK_BYTES", 1):
            _scan_file(io.BytesIO(b"a\r\nb"), lambda number, line: got.append(line))
        assert got == ["a\r\n", "b"]

    def test_a_multibyte_character_split_across_chunks_still_decodes(self) -> None:
        got: list[str] = []
        with patch("haven.adapters.workspace_fs.READ_CHUNK_BYTES", 1):
            _scan_file(io.BytesIO("你好\n世界\n".encode()), lambda n, line: got.append(line))
        assert got == ["你好\n", "世界\n"]

    def test_a_truncated_multibyte_sequence_is_rejected(self) -> None:
        with pytest.raises(UnicodeDecodeError):
            _scan_file(io.BytesIO(b"ok\n\xe4\xbd"), lambda number, line: None)

    def test_the_digest_matches_a_whole_file_hash(self) -> None:
        data = self.TEXT.encode("utf-8")
        digest = _scan_file(io.BytesIO(data), lambda number, line: None)
        assert digest == sha256_bytes(data)


class TestReadIsBounded:
    """`repo.read` 是大文件唯一还能拿到真实摘要的入口，因此它必须有界而不只是能用。"""

    async def test_a_large_file_reads_without_loading_it_all(
        self, workspace: FsWorkspace, repo: Path
    ) -> None:
        """全篇都要扫（摘要和总行数需要它），但内存只随块和单行增长。"""
        target = repo / "src" / "huge.py"
        target.write_text("x = 1\n" * 200_000, encoding="utf-8")
        result = await workspace.read_file("src/huge.py", 1, 10)
        assert result.total_lines == 200_000
        assert result.content == "x = 1\n" * 10
        assert result.truncated
        assert result.digest == sha256_bytes(target.read_bytes())

    async def test_a_huge_single_line_is_not_loaded_whole(
        self, workspace: FsWorkspace, repo: Path
    ) -> None:
        """一行 20MB：内容仍然受 MAX_READ_BYTES 约束，不会被整个搬进内存。"""
        target = repo / "src" / "min.js"
        target.write_text("a" * (20 * 1024 * 1024) + "\n", encoding="utf-8")
        result = await workspace.read_file("src/min.js", 1, 5)
        assert result.total_lines == 1
        assert len(result.content.encode("utf-8")) <= MAX_READ_BYTES
        assert result.truncated

    async def test_the_scan_runs_off_the_event_loop_thread(
        self, workspace: FsWorkspace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """扫描在工作线程里进行：一个巨大的文件不该把事件循环按住数秒。

        用线程标识而不是计时来断言——同样的活，同一线程就是没卸载。
        """
        seen: list[int] = []
        real_scan = workspace_fs._scan_file

        def spy(handle: io.BufferedIOBase, on_line: object) -> str:
            seen.append(threading.get_ident())
            return real_scan(handle, on_line)  # type: ignore[arg-type]

        monkeypatch.setattr(workspace_fs, "_scan_file", spy)
        await workspace.read_file("src/calc.py", 1, 10)
        assert seen, "扫描应该发生过"
        assert seen[0] != threading.get_ident()
