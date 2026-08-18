"""一次运行范围内的原始内容跟踪与统一差异生成。"""

from __future__ import annotations

import difflib
from pathlib import Path

from haven.ports.workspace import RunDiff

MAX_DIFF_BYTES = 64 * 1024


class WorkspaceRunDiff:
    def __init__(self, root: Path) -> None:
        self._root = root
        self._originals: dict[str, str] = {}

    def remember(self, path: str, content: str) -> None:
        self._originals.setdefault(path, content)

    async def render(self) -> RunDiff:
        chunks: list[str] = []
        files: list[str] = []
        insertions = 0
        deletions = 0
        truncated = False
        total = 0

        for normalized in sorted(self._originals):
            original = self._originals[normalized]
            target = self._root / normalized
            current = ""
            if target.is_file():
                try:
                    current = target.read_bytes().decode("utf-8", errors="replace")
                except OSError:
                    current = ""
            if current == original:
                continue
            diff_lines = list(
                difflib.unified_diff(
                    original.splitlines(keepends=True),
                    current.splitlines(keepends=True),
                    fromfile=f"a/{normalized}",
                    tofile=f"b/{normalized}",
                )
            )
            files.append(normalized)
            insertions += sum(
                1 for line in diff_lines if line.startswith("+") and not line.startswith("+++")
            )
            deletions += sum(
                1 for line in diff_lines if line.startswith("-") and not line.startswith("---")
            )
            chunk = "".join(diff_lines)
            if total + len(chunk) > MAX_DIFF_BYTES:
                chunk = chunk[: MAX_DIFF_BYTES - total]
                truncated = True
            total += len(chunk)
            chunks.append(chunk)
            if truncated:
                break

        return RunDiff(
            diff="".join(chunks),
            files=tuple(files),
            insertions=insertions,
            deletions=deletions,
            truncated=truncated,
        )

    def original_contents(self) -> dict[str, str]:
        return dict(self._originals)

    def restore(self, originals: dict[str, str]) -> None:
        self._originals = dict(originals)
