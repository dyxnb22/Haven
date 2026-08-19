"""文件系统工作区 adapter。

代理在磁盘上能够看到或触碰的一切都经过此类。它会规范化路径，在路径逃逸时失败即
拒绝，执行大小上限，将编辑绑定到 preimage，原子应用变更，并跟踪每次运行的原始内容，
使 `repo.diff` 只显示“本次运行”产生的变更。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from haven.adapters.workspace_editor import MAX_CREATE_BYTES, MAX_EDIT_FILE_BYTES, WorkspaceEditor
from haven.adapters.workspace_search import search_workspace
from haven.adapters.workspace_snapshot import capture_workspace_snapshot
from haven.domain.digest import sha256_bytes, sha256_text
from haven.ports.workspace import (
    EditOutcome,
    EditPreview,
    ListEntry,
    ListResult,
    PatchOpSpec,
    PatchPreview,
    PathFacts,
    ReadResult,
    RunDiff,
    SearchResult,
    WorkspaceError,
    WorkspaceSnapshot,
)

__all__ = [
    "FsWorkspace",
    "IGNORED_DIRS",
    "MAX_CREATE_BYTES",
    "MAX_EDIT_FILE_BYTES",
    "os",
]

MAX_READ_BYTES = 128 * 1024
MAX_SEARCH_FILE_BYTES = 1024 * 1024
MAX_SEARCH_TOTAL_BYTES = 64 * 1024
MAX_SEARCH_LINE_CHARS = 240
RIPGREP_TIMEOUT_SECONDS = 20.0

#: 工具永远不能触碰的路径组件（代理不能重写自身配置、Git 历史或审计表面）。
PROTECTED_COMPONENTS = frozenset({".git", ".haven", ".haven.toml"})

#: 永远不值得搜索的 vendor 和构建目录。Ripgrep 也会遵守 `.gitignore`；在
#: 真实仓库中，正是这个列表阻止纯 Python 回退实现遍历 `node_modules`。
IGNORED_DIRS = frozenset(
    {
        ".direnv",
        ".gradle",
        ".idea",
        ".mypy_cache",
        ".next",
        ".nuxt",
        ".parcel-cache",
        ".pytest_cache",
        ".ruff_cache",
        ".svelte-kit",
        ".terraform",
        ".tox",
        ".venv",
        ".vscode",
        "__pycache__",
        ".haven-scratch",
        "bower_components",
        "build",
        "coverage",
        "dist",
        "htmlcov",
        "node_modules",
        "target",
        "venv",
        "vendor",
    }
)


class FsWorkspace:
    """真实文件系统工作区实现，统一执行路径检查、摘要和保护路径规则。

    每条写入路径都必须遵守以下不变量（下面的分节标题用于归类实现）：

    - 每条路径都会被规范化并限制在工作区内：逃逸以及 PROTECTED_COMPONENTS
      （.git/.haven/.haven.toml）会在任何 I/O 之前失败即拒绝；
    - 变更会先生成预览（统一 diff + preimage 摘要），应用时再次验证该 preimage——
      审批绑定的正是预览中展示的内容（否则返回 `stale_preimage`）；
    - 写入是原子的（临时文件 + fsync + rename），并会从磁盘重新读取 postimage：
      一次成功的 write() 调用不是证据；
    - `apply_patch` 会先暂存所有文件，再以先写入后删除的顺序提交，并记录回滚日志；
      无法回滚的失败会抛出 PatchRollbackError，使流水线能够将副作用标记为 unknown；
    - 首次触碰每个文件时都会归档其原始内容（`_originals`），这正是运行级 diff 和
      `haven rewind` 的基础。
    """

    def __init__(self, root: Path, *, use_ripgrep: bool = True) -> None:
        resolved = root.resolve()
        if not resolved.is_dir():
            raise WorkspaceError("not_found", f"workspace root does not exist: {root}")
        self._root = resolved
        self._workspace_digest = sha256_text(str(resolved))
        self._editor = WorkspaceEditor(resolved, self._require_inside, self.path_facts)
        self._ripgrep = shutil.which("rg") if use_ripgrep else None

    @property
    def root(self) -> Path:
        """返回已解析的工作区根目录。"""
        return self._root

    @property
    def workspace_digest(self) -> str:
        """返回用于绑定运行身份的工作区摘要。"""
        return self._workspace_digest

    # -- 路径处理 --------------------------------------------------------------

    def path_facts(self, raw_path: str) -> PathFacts:
        """规范化模型提出的路径并收集已验证的事实。"""
        outside = PathFacts(
            raw=raw_path,
            normalized="",
            within_workspace=False,
            is_protected=False,
            exists=False,
            is_file=False,
            is_dir=False,
            size_bytes=0,
            digest=None,
        )
        if not raw_path or raw_path.startswith(("/", "~")) or "\x00" in raw_path:
            return outside

        try:
            candidate = (self._root / raw_path).resolve()
        except (OSError, RuntimeError):
            # 损坏或循环的符号链接绝不能绕开路径授权，也不应让工具崩溃。
            return outside
        if candidate != self._root and self._root not in candidate.parents:
            return outside

        normalized = (
            candidate.relative_to(self._root).as_posix() if candidate != self._root else "."
        )
        is_protected = any(part in PROTECTED_COMPONENTS for part in Path(normalized).parts)

        try:
            exists = candidate.exists()
            is_file = candidate.is_file() and not candidate.is_symlink() if exists else False
            is_dir = candidate.is_dir() if exists else False
            size = candidate.stat().st_size if is_file else 0
        except OSError:
            return outside
        digest: str | None = None
        if is_file and size <= MAX_EDIT_FILE_BYTES:
            digest = sha256_bytes(candidate.read_bytes())

        return PathFacts(
            raw=raw_path,
            normalized=normalized,
            within_workspace=True,
            is_protected=is_protected,
            exists=exists,
            is_file=is_file,
            is_dir=is_dir,
            size_bytes=size,
            digest=digest,
        )

    def _require_inside(self, raw_path: str) -> tuple[Path, str]:
        facts = self.path_facts(raw_path)
        if not facts.within_workspace:
            raise WorkspaceError("denied", f"path escapes the workspace: {raw_path!r}")
        if facts.is_protected:
            raise WorkspaceError("denied", f"path is protected: {facts.normalized!r}")
        return self._root / facts.normalized, facts.normalized

    # -- 只读工具 --------------------------------------------------------------

    async def list_dir(self, path: str, max_entries: int) -> ListResult:
        """列出工作区内目录，跳过保护组件并按名称稳定排序。"""
        target, normalized = self._require_inside(path)
        if not target.is_dir():
            raise WorkspaceError("not_found", f"not a directory: {normalized!r}")

        entries: list[ListEntry] = []
        truncated = False
        children = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name))
        for child in children:
            if child.name in PROTECTED_COMPONENTS:
                continue
            if len(entries) >= max_entries:
                truncated = True
                break
            is_dir = child.is_dir()
            size = child.stat().st_size if child.is_file() else 0
            entries.append(ListEntry(name=child.name, is_dir=is_dir, size_bytes=size))
        return ListResult(path=normalized, entries=tuple(entries), truncated=truncated)

    async def search(self, pattern: str, path: str, max_results: int) -> SearchResult:
        """搜索工作区内容；路径授权留在门面，后端选择和遍历委托给搜索组件。"""
        target, normalized = self._require_inside(path)
        return await search_workspace(
            root=self._root,
            ripgrep=self._ripgrep,
            pattern=pattern,
            target=target,
            normalized=normalized,
            max_results=max_results,
            timeout_seconds=RIPGREP_TIMEOUT_SECONDS,
            ignored_dirs=IGNORED_DIRS,
            protected_components=PROTECTED_COMPONENTS,
        )

    async def read_file(self, path: str, start_line: int, max_lines: int) -> ReadResult:
        """读取 UTF-8 普通文件的有界行窗口，并返回完整文件摘要。"""
        target, normalized = self._require_inside(path)
        if not target.is_file() or target.is_symlink():
            raise WorkspaceError("not_found", f"not a regular file: {normalized!r}")

        data = target.read_bytes()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WorkspaceError(
                "invalid_arguments", f"not a UTF-8 text file: {normalized!r}"
            ) from exc

        digest = sha256_bytes(data)
        lines = text.splitlines(keepends=True)
        total = len(lines)
        window = lines[start_line - 1 : start_line - 1 + max_lines]

        content = ""
        emitted = 0
        byte_budget = MAX_READ_BYTES
        byte_truncated = False
        for line in window:
            encoded = len(line.encode("utf-8"))
            if encoded > byte_budget:
                byte_truncated = True
                break
            content += line
            byte_budget -= encoded
            emitted += 1

        actual_start = min(start_line, total + 1) if total else 1
        end_line = actual_start - 1 + emitted
        truncated = byte_truncated or end_line < total
        return ReadResult(
            path=normalized,
            content=content,
            start_line=actual_start,
            end_line=end_line,
            total_lines=total,
            truncated=truncated,
            digest=digest,
        )

    # -- 编辑 ------------------------------------------------------------------

    # -- 编辑门面 --------------------------------------------------------------

    async def preview_edit(
        self,
        path: str,
        old: str,
        new: str,
        *,
        occurrence: int | None = None,
        replace_all: bool = False,
    ) -> EditPreview:
        """将编辑请求转发给带前像校验的 WorkspaceEditor。"""
        return await self._editor.preview_edit(
            path, old, new, occurrence=occurrence, replace_all=replace_all
        )

    async def apply_edit(
        self,
        path: str,
        old: str,
        new: str,
        expected_preimage: str,
        *,
        occurrence: int | None = None,
        replace_all: bool = False,
    ) -> EditOutcome:
        """将已审批编辑转发给 WorkspaceEditor 执行。"""
        return await self._editor.apply_edit(
            path,
            old,
            new,
            expected_preimage,
            occurrence=occurrence,
            replace_all=replace_all,
        )

    async def preview_create(self, path: str, content: str) -> EditPreview:
        """转发新文件创建预览。"""
        return await self._editor.preview_create(path, content)

    async def apply_create(self, path: str, content: str) -> EditOutcome:
        """转发新文件创建并保留运行级原始内容。"""
        return await self._editor.apply_create(path, content)

    async def preview_delete(self, path: str) -> EditPreview:
        """转发文件删除预览。"""
        return await self._editor.preview_delete(path)

    async def apply_delete(self, path: str, expected_preimage: str) -> EditOutcome:
        """转发带前像校验的文件删除。"""
        return await self._editor.apply_delete(path, expected_preimage)

    async def preview_move(self, src: str, dest: str) -> tuple[EditPreview, EditPreview]:
        """转发源删除和目标创建组成的移动预览。"""
        return await self._editor.preview_move(src, dest)

    async def apply_move(
        self, src: str, dest: str, expected_preimage: str
    ) -> tuple[EditOutcome, EditOutcome]:
        """转发带前像校验的文件移动。"""
        return await self._editor.apply_move(src, dest, expected_preimage)

    async def preview_patch(
        self, ops: tuple[PatchOpSpec, ...], files_read: dict[str, str]
    ) -> PatchPreview:
        """转发多文件补丁规划，所有路径仍由本门面统一授权。"""
        return await self._editor.preview_patch(ops, files_read)

    async def apply_patch(self, plan: PatchPreview) -> tuple[EditOutcome, ...]:
        """转发原子补丁提交并保留回滚错误语义。"""
        return await self._editor.apply_patch(plan)

    async def run_diff(self) -> RunDiff:
        """返回运行级累计差异。"""
        return await self._editor.run_diff()

    def original_contents(self) -> dict[str, str | None]:
        """返回工作区编辑器保存的运行前内容。"""
        return self._editor.original_contents()

    def restore_originals(self, originals: dict[str, str | None]) -> None:
        """恢复运行级原始内容索引，不直接执行文件恢复。"""
        self._editor.restore_originals(originals)

    def capture_snapshot(self) -> WorkspaceSnapshot:
        """捕获普通文件内容和受保护路径摘要，用于进程写入归因。"""
        return capture_workspace_snapshot(
            root=self._root,
            max_text_bytes=MAX_EDIT_FILE_BYTES,
            ignored_dirs=IGNORED_DIRS,
            protected_components=PROTECTED_COMPONENTS,
        )

    def register_run_original(self, path: str, content: str | None) -> None:
        """登记进程外部写入路径的运行前内容，供差异和撤销使用。"""
        self._editor.register_run_original(path, content)
