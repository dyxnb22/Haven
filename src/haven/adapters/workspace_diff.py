"""一次运行范围内的原始内容跟踪与统一差异生成。"""

from __future__ import annotations

import difflib
from pathlib import Path

from haven.ports.workspace import RunDiff

MAX_DIFF_BYTES = 64 * 1024


class WorkspaceRunDiff:
    def __init__(self, root: Path) -> None:
        self._root = root
        # None 表示路径在本次运行开始时不存在；空字符串表示确实存在的空文件。
        self._originals: dict[str, str | None] = {}

    def remember(self, path: str, content: str | None) -> None:
        """只记录路径第一次被触碰时的原始内容，避免重置运行基线。"""
        self._originals.setdefault(path, content)

    async def render(self) -> RunDiff:
        """读取当前文件并生成相对原始内容的有界统一差异。"""
        chunks: list[str] = []
        files: list[str] = []
        insertions = 0
        deletions = 0
        truncated = False
        emitted_bytes = 0

        for normalized in sorted(self._originals):
            original = self._originals[normalized]
            target = self._root / normalized
            current: str | None = None
            # 运行后路径可能被外部进程换成符号链接。diff 是审计输出，绝不能
            # 因为渲染报告而读取工作区之外的目标。
            if target.is_file() and not target.is_symlink():
                try:
                    current = target.read_bytes().decode("utf-8", errors="replace")
                except OSError:
                    current = None
            if current == original:
                continue
            diff_lines = list(
                difflib.unified_diff(
                    (original or "").splitlines(keepends=True),
                    (current or "").splitlines(keepends=True),
                    fromfile=f"a/{normalized}" if original is not None else "/dev/null",
                    tofile=f"b/{normalized}" if current is not None else "/dev/null",
                )
            )
            if not diff_lines:
                # 空文件的创建/删除没有文本行，但文件存在性仍然是净变化。
                diff_lines = [
                    f"--- {'a/' + normalized if original is not None else '/dev/null'}\n",
                    f"+++ {'b/' + normalized if current is not None else '/dev/null'}\n",
                ]
            files.append(normalized)
            insertions += sum(
                1 for line in diff_lines if line.startswith("+") and not line.startswith("+++")
            )
            deletions += sum(
                1 for line in diff_lines if line.startswith("-") and not line.startswith("---")
            )
            chunk = "".join(diff_lines)
            remaining = MAX_DIFF_BYTES - emitted_bytes
            encoded = chunk.encode("utf-8")
            if len(encoded) > remaining:
                chunk = _utf8_prefix(encoded, max(remaining, 0))
                truncated = True
            emitted_bytes += len(chunk.encode("utf-8"))
            if chunk:
                chunks.append(chunk)

        return RunDiff(
            diff="".join(chunks),
            files=tuple(files),
            insertions=insertions,
            deletions=deletions,
            truncated=truncated,
        )

    def original_contents(self) -> dict[str, str | None]:
        """返回当前保存的运行前内容副本。"""
        return dict(self._originals)

    def restore(self, originals: dict[str, str | None]) -> None:
        """用给定快照替换内部基线，供检查点恢复使用。"""
        self._originals = dict(originals)


def _utf8_prefix(encoded: bytes, limit: int) -> str:
    """返回不超过字节上限且不会切断 UTF-8 码点的前缀。"""
    return encoded[:limit].decode("utf-8", errors="ignore")
