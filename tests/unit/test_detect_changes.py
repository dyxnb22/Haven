"""`detect_changes` 必须区分"运行前不存在"、"运行前是空文件"和"内容未能保留"。

第三种状态此前的实现是空串，于是运行差异把一个二进制文件谎称成"从空文件改成
现在这样"，回滚更会据此把它写成零字节。这里把三种状态各自钉住。
"""

from pathlib import Path

import pytest

from haven.adapters.workspace_diff import WorkspaceRunDiff
from haven.adapters.workspace_snapshot import capture_workspace_snapshot
from haven.application.tool_processes import detect_changes
from haven.ports.workspace import OriginalContent

MAX_TEXT_BYTES = 256 * 1024


def _snapshot(root: Path) -> object:
    return capture_workspace_snapshot(
        root=root,
        max_text_bytes=MAX_TEXT_BYTES,
        ignored_dirs=frozenset(),
        protected_components=frozenset(),
    )


def _change_for(root: Path, mutate) -> object:
    """在 mutate() 前后各拍一张快照，返回 path 的那一项变化。"""
    before = _snapshot(root)
    mutate()
    after = _snapshot(root)
    changes = detect_changes(before, after)  # type: ignore[arg-type]
    assert len(changes) == 1, changes
    return changes[0]


class TestBeforeContentStates:
    def test_a_changed_text_file_keeps_its_before_text(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("one\n", encoding="utf-8")
        change = _change_for(tmp_path, lambda: (tmp_path / "a.py").write_text("two\n"))
        assert not change.before_content.absent
        assert not change.before_content.unavailable
        assert change.before_content.text == "one\n"

    def test_a_new_file_is_absent_not_empty(self, tmp_path: Path) -> None:
        """新建文件：`absent` 为真。这与"当时确实是空文件"是两件事。"""
        change = _change_for(tmp_path, lambda: (tmp_path / "new.py").write_text("x\n"))
        assert change.before_content.absent
        assert not change.before_content.unavailable
        assert change.before_content.text == ""

    def test_a_genuinely_empty_file_is_captured_as_empty(self, tmp_path: Path) -> None:
        """运行前确实是空文件：内容拿到了，就是空串。

        这条是上一条的对立面——没有它，`absent` 与"空文件"的区分就没有意义。
        """
        (tmp_path / "empty.py").write_text("", encoding="utf-8")
        change = _change_for(tmp_path, lambda: (tmp_path / "empty.py").write_text("now\n"))
        assert not change.before_content.absent
        assert not change.before_content.unavailable
        assert change.before_content.text == ""

    def test_a_binary_file_is_uncaptured_not_empty(self, tmp_path: Path) -> None:
        """核心缺陷：内容不可用必须说"不知道"，不能编一个空串。

        二进制文件进不了 `contents`（不是合法 UTF-8），但 `digests` 覆盖它，
        所以变化照样检测得到——只有"运行前是什么"不知道。
        """
        (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02\xff\xfe")
        change = _change_for(tmp_path, lambda: (tmp_path / "blob.bin").write_bytes(b"\x00\x09"))
        assert change.before_content.unavailable
        assert not change.before_content.absent
        assert change.before_content.text == ""
        # 摘要仍然记录在案：变化本身是确凿的，不知道的只是旧内容。
        assert change.preimage_digest
        assert change.postimage_digest
        assert change.preimage_digest != change.postimage_digest

    def test_an_oversized_file_is_uncaptured_not_empty(self, tmp_path: Path) -> None:
        """超出 `MAX_EDIT_FILE_BYTES` 的文本文件同样没有保留内容。"""
        (tmp_path / "big.md").write_text("x" * (MAX_TEXT_BYTES + 1), encoding="utf-8")
        change = _change_for(
            tmp_path, lambda: (tmp_path / "big.md").write_text("rewritten by a process\n")
        )
        assert change.before_content.unavailable
        assert change.before_content.text == ""

    def test_a_deleted_file_reports_the_absent_pre_state_correctly(self, tmp_path: Path) -> None:
        """被删掉的文件：运行前内容拿得到（captured），运行后不存在。"""
        (tmp_path / "gone.py").write_text("bye\n", encoding="utf-8")
        change = _change_for(tmp_path, lambda: (tmp_path / "gone.py").unlink())
        assert not change.before_content.absent
        assert not change.before_content.unavailable
        assert change.before_content.text == "bye\n"
        assert change.postimage_digest == ""


class TestRunDiffRendersTheThreeStates:
    @pytest.mark.asyncio
    async def test_uncaptured_content_is_listed_but_not_fabricated(self, tmp_path: Path) -> None:
        """文件仍然出现在 diff 里——它确实变了——但不假装知道增删行数。"""
        run_diff = WorkspaceRunDiff(tmp_path)
        run_diff.remember("blob.bin", OriginalContent.uncaptured())
        (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02")
        rendered = await run_diff.render()

        assert "blob.bin" in rendered.files
        assert rendered.insertions == 0, "没有运行前内容，就不能把现行行算成新增"
        assert rendered.deletions == 0
        assert "not captured" in rendered.diff

    @pytest.mark.asyncio
    async def test_an_absent_file_created_empty_is_a_change(self, tmp_path: Path) -> None:
        """新建的空文件是一次真实变化，不能因为 current 与 text 都是 "" 而漏掉。"""
        run_diff = WorkspaceRunDiff(tmp_path)
        run_diff.remember("empty.txt", OriginalContent.missing())
        (tmp_path / "empty.txt").write_text("", encoding="utf-8")
        rendered = await run_diff.render()

        assert rendered.files == ("empty.txt",)
        assert "/dev/null" in rendered.diff

    @pytest.mark.asyncio
    async def test_a_captured_empty_file_that_did_not_change_is_skipped(
        self, tmp_path: Path
    ) -> None:
        """确实是空文件且没变：不进 diff。"""
        run_diff = WorkspaceRunDiff(tmp_path)
        run_diff.remember("empty.txt", OriginalContent.captured(""))
        (tmp_path / "empty.txt").write_text("", encoding="utf-8")
        assert (await run_diff.render()).files == ()

    @pytest.mark.asyncio
    async def test_a_captured_file_counts_real_insertions(self, tmp_path: Path) -> None:
        run_diff = WorkspaceRunDiff(tmp_path)
        run_diff.remember("a.py", OriginalContent.captured("one\n"))
        (tmp_path / "a.py").write_text("one\ntwo\n")
        rendered = await run_diff.render()
        assert rendered.files == ("a.py",)
        assert (rendered.insertions, rendered.deletions) == (1, 0)

    @pytest.mark.asyncio
    async def test_uncaptured_content_survives_a_restore_round_trip(self, tmp_path: Path) -> None:
        """检查点恢复后，"内容未能保留"不能退化成"当时是空文件"。"""
        run_diff = WorkspaceRunDiff(tmp_path)
        run_diff.remember("blob.bin", OriginalContent.uncaptured())
        restored = WorkspaceRunDiff(tmp_path)
        restored.restore(run_diff.original_contents())
        (tmp_path / "blob.bin").write_bytes(b"\x00\x01")
        rendered = await restored.render()
        assert "not captured" in rendered.diff
        assert rendered.insertions == 0
