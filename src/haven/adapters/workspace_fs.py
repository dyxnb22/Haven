"""文件系统工作区 adapter。

代理在磁盘上能够看到或触碰的一切都经过此类。它会规范化路径，在路径逃逸时失败即
拒绝，执行大小上限，将编辑绑定到 preimage，原子应用变更，并跟踪每次运行的原始内容，
使 `repo.diff` 只显示“本次运行”产生的变更。
"""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

from haven.domain.digest import sha256_bytes, sha256_text
from haven.ports.workspace import (
    EditOutcome,
    EditPreview,
    ListEntry,
    ListResult,
    PatchEffect,
    PatchOpSpec,
    PatchPreview,
    PatchRollbackError,
    PathFacts,
    ReadResult,
    RunDiff,
    SearchMatch,
    SearchResult,
    WorkspaceError,
    WorkspaceSnapshot,
)

MAX_READ_BYTES = 128 * 1024
MAX_EDIT_FILE_BYTES = 256 * 1024
MAX_SEARCH_FILE_BYTES = 1024 * 1024
MAX_SEARCH_TOTAL_BYTES = 64 * 1024
MAX_SEARCH_LINE_CHARS = 240
MAX_DIFF_BYTES = 64 * 1024
MAX_CREATE_BYTES = 256 * 1024
RIPGREP_TIMEOUT_SECONDS = 20.0

#: 纯 Python 搜索每处理多少行检查一次截止时间。频率足以限制长时间遍历，
#: 又足够低，不会让读取时钟本身成为负担。
_DEADLINE_CHECK_LINES = 256

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
    """为本地目录实现 WorkspacePort。

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
        # 路径（规范化、相对路径）-> 本次运行第一次写入前的文件内容。本次
        # 运行创建的文件映射为 ""，使运行 diff 将其显示为纯新增。
        self._originals: dict[str, str] = {}
        self._ripgrep = shutil.which("rg") if use_ripgrep else None

    @property
    def root(self) -> Path:
        return self._root

    @property
    def workspace_digest(self) -> str:
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

        candidate = (self._root / raw_path).resolve()
        if candidate != self._root and self._root not in candidate.parents:
            return outside

        normalized = (
            candidate.relative_to(self._root).as_posix() if candidate != self._root else "."
        )
        is_protected = any(part in PROTECTED_COMPONENTS for part in Path(normalized).parts)

        exists = candidate.exists()
        is_file = candidate.is_file() and not candidate.is_symlink() if exists else False
        is_dir = candidate.is_dir() if exists else False
        size = candidate.stat().st_size if is_file else 0
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
        """搜索文件内容，优先使用 ripgrep，不可用时回退到 Python。

        两个后端都会跳过相同的 vendor/build 目录，以相同方式限制结果，并输出相同的
        规范化结构；因此对于没有 `.gitignore` 的目录树，它们会返回相同的匹配项（测试
        已对此断言）。Ripgrep 还会遵守 `.gitignore`，这使搜索可以用于真实仓库；纯
        Python 回退实现则使用固定的 `IGNORED_DIRS` 列表近似这一行为。
        """
        target, normalized = self._require_inside(path)
        try:
            re.compile(pattern)
        except re.error as exc:
            raise WorkspaceError("invalid_arguments", f"invalid regex: {exc}") from exc
        if not target.exists():
            raise WorkspaceError("not_found", f"no such path to search: {normalized!r}")

        if self._ripgrep is not None:
            return await self._search_ripgrep(self._ripgrep, pattern, target, max_results)
        return self._search_walk(pattern, target, max_results)

    async def _search_ripgrep(
        self, ripgrep: str, pattern: str, target: Path, max_results: int
    ) -> SearchResult:
        # 固定 argv，不经过 shell。`--regexp=` 和 `--` 防止以连字符开头的模式
        # 或路径被解析为 ripgrep 标志。
        argv = [
            ripgrep,
            "--line-number",
            "--no-heading",
            "--with-filename",
            "--color=never",
            "--sort=path",
            # 即使工作区不是 Git checkout 也遵守 .gitignore，因此忽略语义不取决于
            # `.git` 是否恰好存在。
            "--no-require-git",
            f"--max-filesize={MAX_SEARCH_FILE_BYTES}",
            *(f"--glob=!{name}" for name in sorted(IGNORED_DIRS | PROTECTED_COMPONENTS)),
            f"--regexp={pattern}",
            "--",
            str(target),
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=self._root,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
            )
            raw, err = await asyncio.wait_for(proc.communicate(), timeout=RIPGREP_TIMEOUT_SECONDS)
        except (OSError, TimeoutError):
            # 缺失或行为异常的 ripgrep 绝不能破坏工具。
            return self._search_walk(pattern, target, max_results)
        # 0 = 匹配，1 = 无匹配，2 = 部分 I/O 错误（文件不可读或路径消失），
        # 此时 stdout 仍然有效。搜索后端出问题时必须降级，绝不能终止运行。
        if proc.returncode not in (0, 1, 2):
            return self._search_walk(pattern, target, max_results)
        if proc.returncode == 2 and not raw.strip():
            return self._search_walk(pattern, target, max_results)

        matches: list[SearchMatch] = []
        seen_files: set[str] = set()
        total_bytes = 0
        truncated = False
        for line in raw.decode("utf-8", errors="replace").splitlines():
            parsed = self._parse_ripgrep_line(line)
            if parsed is None:
                continue
            rel, line_number, text = parsed
            seen_files.add(rel)
            clipped = text.strip()[:MAX_SEARCH_LINE_CHARS]
            total_bytes += len(clipped.encode("utf-8"))
            matches.append(SearchMatch(path=rel, line_number=line_number, line=clipped))
            if len(matches) >= max_results or total_bytes >= MAX_SEARCH_TOTAL_BYTES:
                truncated = True
                break
        return SearchResult(
            matches=tuple(matches), files_scanned=len(seen_files), truncated=truncated
        )

    def _parse_ripgrep_line(self, line: str) -> tuple[str, int, str] | None:
        """解析 `path:line:text`，允许路径和文本内部包含冒号。"""
        head, sep, text = line.partition(":")
        if not sep:
            return None
        number, sep, text = text.partition(":")
        if not sep or not number.isdigit():
            return None
        try:
            rel = Path(head).resolve().relative_to(self._root).as_posix()
        except ValueError:
            return None
        return rel, int(number), text

    def _search_walk(self, pattern: str, target: Path, max_results: int) -> SearchResult:
        """纯 Python 回退实现，仅在 ripgrep 不可用时使用。

        除结果数量上限外，还受到墙上时钟截止时间限制，因为模式来自模型，目前只做语法
        验证：像 `(a+)+b` 这样的回溯模式会对每个匹配对象产生指数级成本，而 `re`
        本身没有超时机制。

        下面是根据测量而非假设得出的截止时间覆盖范围：

        - 它限制的是遍历过程——大量文件和大量行——这正是现实中慢搜索的形态。
          检查发生在匹配对象之间，因此检查本身没有成本。
        - 它无法中断已经运行的单次 `re.search`。Python 的正则引擎会在整个匹配期间
          持有 GIL：这里测得一次 0.68 秒的匹配让事件循环运行了 **零** 次。这也是
          将其移到 `asyncio.to_thread` 没有帮助、并且有意没有这样做的原因——超时机制
          甚至无法触发。要真正限制单个异常匹配对象，需要可杀死的子进程；对于只在
          ripgrep 缺失时才存在的回退实现，这样做不值得（ripgrep 的引擎是线性的，
          不受该问题影响）。
        """
        compiled = re.compile(pattern)
        matches: list[SearchMatch] = []
        # 产生至少一个匹配的文件，这是 ripgrep 唯一能报告的数量。如果这里
        # 改为统计遍历过的文件，那么 ripgrep 是否安装会让同一个字段具有
        # 不同含义。
        seen_files: set[str] = set()
        total_bytes = 0
        truncated = False
        deadline = time.monotonic() + RIPGREP_TIMEOUT_SECONDS

        for file_path in self._iter_files(target):
            if truncated:
                break
            if time.monotonic() > deadline:
                truncated = True
                break
            try:
                if file_path.stat().st_size > MAX_SEARCH_FILE_BYTES:
                    continue
                data = file_path.read_bytes()
            except OSError:
                continue
            if b"\x00" in data:
                continue  # 二进制
            text = data.decode("utf-8", errors="replace")
            rel = file_path.relative_to(self._root).as_posix()
            for line_number, line in enumerate(text.splitlines(), start=1):
                if line_number % _DEADLINE_CHECK_LINES == 0 and time.monotonic() > deadline:
                    truncated = True
                    break
                if compiled.search(line):
                    clipped = line.strip()[:MAX_SEARCH_LINE_CHARS]
                    total_bytes += len(clipped.encode("utf-8"))
                    matches.append(SearchMatch(path=rel, line_number=line_number, line=clipped))
                    seen_files.add(rel)
                    if len(matches) >= max_results or total_bytes >= MAX_SEARCH_TOTAL_BYTES:
                        truncated = True
                        break
        return SearchResult(
            matches=tuple(matches), files_scanned=len(seen_files), truncated=truncated
        )

    def _iter_files(self, target: Path) -> list[Path]:
        if target.is_file():
            return [target]
        found: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(target):
            dirnames[:] = sorted(
                d for d in dirnames if d not in PROTECTED_COMPONENTS and d not in IGNORED_DIRS
            )
            for name in sorted(filenames):
                if name in PROTECTED_COMPONENTS:
                    continue
                candidate = Path(dirpath) / name
                if candidate.is_file() and not candidate.is_symlink():
                    found.append(candidate)
        return found

    async def read_file(self, path: str, start_line: int, max_lines: int) -> ReadResult:
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

        end_line = start_line - 1 + emitted
        truncated = byte_truncated or end_line < total
        return ReadResult(
            path=normalized,
            content=content,
            start_line=start_line,
            end_line=end_line,
            total_lines=total,
            truncated=truncated,
            digest=digest,
        )

    # -- 编辑 ------------------------------------------------------------------

    async def preview_edit(
        self,
        path: str,
        old: str,
        new: str,
        *,
        occurrence: int | None = None,
        replace_all: bool = False,
    ) -> EditPreview:
        target, normalized = self._require_inside(path)
        text, preimage = self._load_editable(target, normalized)
        new_text = self._apply_replacement(
            text, old, new, normalized, occurrence=occurrence, replace_all=replace_all
        )
        return self._diff_preview(normalized, text, new_text, preimage)

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
        target, normalized = self._require_inside(path)
        text, preimage = self._load_editable(target, normalized)
        if preimage != expected_preimage:
            raise WorkspaceError(
                "stale_preimage",
                f"file changed since it was read/approved: {normalized!r}",
            )
        new_text = self._apply_replacement(
            text, old, new, normalized, occurrence=occurrence, replace_all=replace_all
        )

        if normalized not in self._originals:
            self._originals[normalized] = text

        postimage = self._atomic_write(target, normalized, new_text)
        return EditOutcome(path=normalized, preimage_digest=preimage, postimage_digest=postimage)

    # -- 创建 ------------------------------------------------------------------

    async def preview_create(self, path: str, content: str) -> EditPreview:
        normalized = self._require_creatable(path, content)
        return self._diff_preview(normalized, "", content, preimage="")

    async def apply_create(self, path: str, content: str) -> EditOutcome:
        normalized = self._require_creatable(path, content)
        target = self._root / normalized
        target.parent.mkdir(parents=True, exist_ok=True)

        if normalized not in self._originals:
            # 空的原始内容会使运行 diff 将该文件显示为新增。
            self._originals[normalized] = ""

        postimage = self._atomic_write(target, normalized, content)
        return EditOutcome(path=normalized, preimage_digest="", postimage_digest=postimage)

    # -- 删除 ------------------------------------------------------------------

    async def preview_delete(self, path: str) -> EditPreview:
        target, normalized = self._require_inside(path)
        text, preimage = self._load_editable(target, normalized)
        # 删除就是从文件内容到空内容的 diff。
        return self._diff_preview(normalized, text, "", preimage=preimage)

    async def apply_delete(self, path: str, expected_preimage: str) -> EditOutcome:
        target, normalized = self._require_inside(path)
        text, preimage = self._load_editable(target, normalized)
        if preimage != expected_preimage:
            raise WorkspaceError(
                "stale_preimage", f"file changed since it was approved: {normalized!r}"
            )
        if normalized not in self._originals:
            self._originals[normalized] = text
        target.unlink()
        # 空 postimage 表示删除，与 ledger 使用相同约定。
        return EditOutcome(path=normalized, preimage_digest=preimage, postimage_digest="")

    # -- 移动 ------------------------------------------------------------------

    async def preview_move(self, src: str, dest: str) -> tuple[EditPreview, EditPreview]:
        src_target, src_norm = self._require_inside(src)
        text, preimage = self._load_editable(src_target, src_norm)
        dest_norm = self._require_creatable(dest, text)
        removal = self._diff_preview(src_norm, text, "", preimage=preimage)
        addition = self._diff_preview(dest_norm, "", text, preimage="")
        return removal, addition

    async def apply_move(
        self, src: str, dest: str, expected_preimage: str
    ) -> tuple[EditOutcome, EditOutcome]:
        src_target, src_norm = self._require_inside(src)
        text, preimage = self._load_editable(src_target, src_norm)
        if preimage != expected_preimage:
            raise WorkspaceError(
                "stale_preimage", f"file changed since it was approved: {src_norm!r}"
            )
        dest_norm = self._require_creatable(dest, text)
        dest_target = self._root / dest_norm
        dest_target.parent.mkdir(parents=True, exist_ok=True)
        if src_norm not in self._originals:
            self._originals[src_norm] = text
        if dest_norm not in self._originals:
            self._originals[dest_norm] = ""
        postimage = self._atomic_write(dest_target, dest_norm, text)
        src_target.unlink()
        removal = EditOutcome(path=src_norm, preimage_digest=preimage, postimage_digest="")
        addition = EditOutcome(path=dest_norm, preimage_digest="", postimage_digest=postimage)
        return removal, addition

    # -- 补丁（多文件、单事务）--------------------------------------------------

    async def preview_patch(
        self, ops: tuple[PatchOpSpec, ...], files_read: dict[str, str]
    ) -> PatchPreview:
        """在内存中模拟补丁并返回其确定性计划。

        模拟会按照顺序作用于延迟填充的目录树视图，因此后续操作可以看到前面操作的
        效果。计划记录每个文件的“净”副作用（move 会变成可证明的 delete 加可证明
        的 create），这样中断的补丁才能按照现有恢复规则逐文件分类。
        """
        if not ops:
            raise WorkspaceError("invalid_arguments", "a patch needs at least one operation")

        #: 规范化路径 -> 模拟中的当前文本（None = 不存在）。
        state: dict[str, str | None] = {}
        #: 规范化路径 -> 磁盘上首次看到的（文本、摘要）。
        on_disk: dict[str, tuple[str, str]] = {}
        #: 内容完全由此补丁决定的路径（创建目标或移动目标），因此不适用先读后
        #: 编辑规则。
        patch_authored: set[str] = set()

        def seed(raw: str) -> str:
            target, normalized = self._require_inside(raw)
            if normalized not in state:
                if target.is_file() and not target.is_symlink():
                    text, digest = self._load_editable(target, normalized)
                    state[normalized] = text
                    on_disk[normalized] = (text, digest)
                elif target.exists():
                    raise WorkspaceError("invalid_arguments", f"not a regular file: {normalized!r}")
                else:
                    state[normalized] = None
            return normalized

        for index, op in enumerate(ops):
            where = f"operation {index + 1} ({op.kind})"
            if op.kind == "edit":
                normalized = seed(op.path)
                current = state[normalized]
                if current is None:
                    raise WorkspaceError(
                        "not_found", f"{where}: {normalized!r} does not exist at this point"
                    )
                if normalized not in patch_authored:
                    recorded = files_read.get(normalized)
                    if recorded is None:
                        raise WorkspaceError(
                            "invalid_arguments",
                            f"{where}: read {normalized!r} with repo.read before editing it",
                        )
                    if on_disk[normalized][1] != recorded:
                        raise WorkspaceError(
                            "stale_preimage",
                            f"{where}: {normalized!r} changed since it was last read",
                        )
                state[normalized] = self._apply_replacement(
                    current,
                    op.old,
                    op.new,
                    normalized,
                    occurrence=op.occurrence,
                    replace_all=op.replace_all,
                )
            elif op.kind == "create":
                normalized = self._require_creatable(op.path, op.content)
                if state.get(normalized) is not None:
                    raise WorkspaceError(
                        "invalid_arguments",
                        f"{where}: {normalized!r} already exists at this point",
                    )
                state[normalized] = op.content
                patch_authored.add(normalized)
            elif op.kind == "delete":
                normalized = seed(op.path)
                if state[normalized] is None:
                    raise WorkspaceError(
                        "not_found", f"{where}: {normalized!r} does not exist at this point"
                    )
                state[normalized] = None
            elif op.kind == "move":
                src_norm = seed(op.src)
                moving = state[src_norm]
                if moving is None:
                    raise WorkspaceError(
                        "not_found", f"{where}: {src_norm!r} does not exist at this point"
                    )
                dest_norm = self._require_creatable(op.dest, moving)
                if state.get(dest_norm) is not None:
                    raise WorkspaceError(
                        "invalid_arguments",
                        f"{where}: destination {dest_norm!r} already exists at this point",
                    )
                state[dest_norm] = moving
                state[src_norm] = None
                patch_authored.add(dest_norm)
            else:  # pragma: no cover — contract 的判别字段禁止此情况
                raise WorkspaceError("invalid_arguments", f"{where}: unknown operation kind")

        diffs: list[str] = []
        preimages: dict[str, str] = {}
        effects: list[PatchEffect] = []
        insertions = 0
        deletions = 0
        for normalized in sorted(state):
            before: tuple[str | None, str] = on_disk.get(normalized, (None, ""))
            before_text, before_digest = before
            after_text = state[normalized]
            if before_text == after_text:
                continue  # 净空操作（例如先创建后删除）
            if before_text is not None:
                preimages[normalized] = before_digest
            preview = self._diff_preview(
                normalized, before_text or "", after_text or "", before_digest
            )
            diffs.append(preview.diff)
            insertions += preview.insertions
            deletions += preview.deletions
            if before_text is None:
                shape = "repo.create"
            elif after_text is None:
                shape = "repo.delete"
            else:
                shape = "repo.edit"
            effects.append(
                PatchEffect(
                    tool_shape=shape,
                    path=normalized,
                    preimage_digest=before_digest,
                    expected_postimage=sha256_text(after_text) if after_text is not None else "",
                )
            )
        if not effects:
            raise WorkspaceError("invalid_arguments", "the patch changes nothing")
        final_contents = {
            effect.path: text for effect in effects if (text := state[effect.path]) is not None
        }
        return PatchPreview(
            diff="".join(diffs),
            preimages=preimages,
            effects=tuple(effects),
            final_contents=final_contents,
            insertions=insertions,
            deletions=deletions,
        )

    async def apply_patch(self, plan: PatchPreview) -> tuple[EditOutcome, ...]:
        """提交计划中的补丁：验证每个固定值，暂存所有写入，然后重命名写入结果并
        删除待移除项；任何失败都会触发回滚。

        这里的顺序是有意设计的：所有内容都会在任何删除发生前落盘，因此任何崩溃点都
        不会丢失数据；每个中间状态都可以依据日志记录的逐文件预期进行分类。
        """
        # 1. 每个固定的 preimage 都必须仍然匹配，每个 create 目标都必须仍然
        # 不存在——在任何字节落盘前检查。
        for normalized, expected in plan.preimages.items():
            target = self._root / normalized
            if not target.is_file() or sha256_bytes(target.read_bytes()) != expected:
                raise WorkspaceError(
                    "stale_preimage",
                    f"file changed since the patch was approved: {normalized!r}",
                )
        for effect in plan.effects:
            if effect.tool_shape == "repo.create" and (self._root / effect.path).exists():
                raise WorkspaceError(
                    "stale_preimage",
                    f"path appeared since the patch was approved: {effect.path!r}",
                )

        writes = [e for e in plan.effects if e.tool_shape in ("repo.edit", "repo.create")]
        removals = [e for e in plan.effects if e.tool_shape == "repo.delete"]

        # 2. 将每次写入先暂存到目标旁边的临时文件。
        staged: dict[str, str] = {}
        try:
            for effect in writes:
                target = self._root / effect.path
                target.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".haven-patch-")
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(plan.final_contents[effect.path])
                    handle.flush()
                    os.fsync(handle.fileno())
                staged[effect.path] = tmp_name
        except OSError as exc:
            for tmp_name in staged.values():
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)
            raise WorkspaceError("internal", f"could not stage the patch: {exc}") from exc

        # 3. 提交，并记录足够的信息以便回滚：先写入，后删除。
        # `performed` 按最新在前记录需要撤销的内容。
        performed: list[tuple[str, str, str | None]] = []  # （action，path，原始文本）
        outcomes: list[EditOutcome] = []

        def rollback() -> None:
            for action, normalized, original in reversed(performed):
                target = self._root / normalized
                if action == "write":
                    if original is None:
                        target.unlink(missing_ok=True)
                    else:
                        self._atomic_write(target, normalized, original)
                elif action == "unlink" and original is not None:
                    self._atomic_write(target, normalized, original)

        try:
            for effect in writes:
                target = self._root / effect.path
                original = on_disk_text = None
                if effect.tool_shape == "repo.edit":
                    on_disk_text = target.read_text(encoding="utf-8")
                    original = on_disk_text
                if effect.path not in self._originals:
                    self._originals[effect.path] = on_disk_text or ""
                os.replace(staged.pop(effect.path), target)
                performed.append(("write", effect.path, original))
                postimage = sha256_bytes(target.read_bytes())
                if postimage != effect.expected_postimage:
                    raise WorkspaceError(
                        "internal", f"postimage mismatch after write: {effect.path!r}"
                    )
                outcomes.append(
                    EditOutcome(
                        path=effect.path,
                        preimage_digest=effect.preimage_digest,
                        postimage_digest=postimage,
                    )
                )
            for effect in removals:
                target = self._root / effect.path
                original = target.read_text(encoding="utf-8")
                if effect.path not in self._originals:
                    self._originals[effect.path] = original
                target.unlink()
                performed.append(("unlink", effect.path, original))
                outcomes.append(
                    EditOutcome(
                        path=effect.path,
                        preimage_digest=effect.preimage_digest,
                        postimage_digest="",
                    )
                )
        except (OSError, WorkspaceError) as exc:
            try:
                rollback()
            except (OSError, WorkspaceError) as rollback_exc:
                # 目录树现在处于无法撤销的部分状态：必须将其暴露为 unknown 副作用，
                # 不能作为干净失败处理，以便恢复逻辑阻止继续运行，由用户调和。
                raise PatchRollbackError(
                    f"patch failed ({exc}) and rollback also failed ({rollback_exc}); "
                    "the workspace is in a partial state"
                ) from exc
            raise WorkspaceError(
                "internal", f"patch failed and was rolled back cleanly: {exc}"
            ) from exc
        finally:
            for tmp_name in staged.values():
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)

        return tuple(outcomes)

    def _require_creatable(self, path: str, content: str) -> str:
        """create 只适用于真正的新文件；覆盖已有文件必须通过 repo.edit，
        以便继续绑定 preimage。"""
        facts = self.path_facts(path)
        if not facts.within_workspace:
            raise WorkspaceError("denied", f"path escapes the workspace: {path!r}")
        if facts.is_protected:
            raise WorkspaceError("denied", f"path is protected: {facts.normalized!r}")
        if facts.normalized in (".", ""):
            raise WorkspaceError("invalid_arguments", "a file path is required")
        if facts.exists:
            kind = "directory" if facts.is_dir else "file"
            raise WorkspaceError(
                "invalid_arguments",
                f"{facts.normalized!r} already exists as a {kind}; use repo.edit to change it",
            )
        if len(content.encode("utf-8")) > MAX_CREATE_BYTES:
            raise WorkspaceError(
                "invalid_arguments", f"content too large to create: {facts.normalized!r}"
            )
        return facts.normalized

    # -- 共享写入基础设施 -------------------------------------------------------

    def _atomic_write(self, target: Path, normalized: str, new_text: str) -> str:
        """通过临时文件 + fsync + rename 写入，然后重新读取以确认结果。

        成功的 write() 不是证据；postimage 摘要才是。
        """
        fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".haven-write-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(new_text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, target)
        except OSError:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
            raise

        postimage = sha256_bytes(target.read_bytes())
        if postimage != sha256_text(new_text):
            raise WorkspaceError("internal", f"postimage mismatch after write: {normalized!r}")
        return postimage

    @staticmethod
    def _diff_preview(normalized: str, before: str, after: str, preimage: str) -> EditPreview:
        diff_lines = list(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=f"a/{normalized}" if before else "/dev/null",
                tofile=f"b/{normalized}",
            )
        )
        insertions = sum(
            1 for line in diff_lines if line.startswith("+") and not line.startswith("+++")
        )
        deletions = sum(
            1 for line in diff_lines if line.startswith("-") and not line.startswith("---")
        )
        return EditPreview(
            path=normalized,
            diff="".join(diff_lines),
            preimage_digest=preimage,
            postimage_digest=sha256_text(after),
            insertions=insertions,
            deletions=deletions,
        )

    def _load_editable(self, target: Path, normalized: str) -> tuple[str, str]:
        if not target.is_file() or target.is_symlink():
            raise WorkspaceError("not_found", f"not a regular file: {normalized!r}")
        data = target.read_bytes()
        if len(data) > MAX_EDIT_FILE_BYTES:
            raise WorkspaceError(
                "invalid_arguments",
                f"file too large to edit ({len(data)} bytes): {normalized!r}",
            )
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WorkspaceError(
                "invalid_arguments", f"not a UTF-8 text file: {normalized!r}"
            ) from exc
        return text, sha256_bytes(data)

    @staticmethod
    def _apply_replacement(
        text: str,
        old: str,
        new: str,
        normalized: str,
        *,
        occurrence: int | None,
        replace_all: bool,
    ) -> str:
        """替换 `old` 的一个或全部出现位置。

        默认仍要求“必须唯一”，因为意外匹配多处是代理悄悄破坏文件最常见的方式。
        `replace_all` 和 `occurrence` 是明确退出这一约束的两种方式，因此意图始终会
        记录在已审批的参数中。
        """
        if replace_all and occurrence is not None:
            raise WorkspaceError(
                "invalid_arguments", "set either replace_all or occurrence, not both"
            )

        count = text.count(old)
        if count == 0:
            raise WorkspaceError(
                "not_found",
                f"old_string not found in {normalized!r}; re-read the file and copy the "
                "exact text including indentation",
            )

        if replace_all:
            return text.replace(old, new)

        if occurrence is not None:
            if occurrence > count:
                raise WorkspaceError(
                    "not_found",
                    f"occurrence {occurrence} requested but old_string appears only "
                    f"{count} time(s) in {normalized!r}",
                )
            index = -1
            for _ in range(occurrence):
                index = text.index(old, index + 1)
            return text[:index] + new + text[index + len(old) :]

        if count > 1:
            raise WorkspaceError(
                "ambiguous_match",
                f"old_string occurs {count} times in {normalized!r}. Include more "
                "surrounding lines to make it unique, or pass occurrence=N (1-based) "
                "to pick one, or replace_all=true to change every match",
            )
        return text.replace(old, new, 1)

    # -- 运行范围 diff ----------------------------------------------------------

    async def run_diff(self) -> RunDiff:
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

    def restore_originals(self, originals: dict[str, str]) -> None:
        self._originals = dict(originals)

    # -- 进程写入归因（ADR 0012）------------------------------------------------

    def capture_snapshot(self) -> WorkspaceSnapshot:
        """为每个普通文件计算摘要，并保留可生成 diff 的文件文本。

        摘要映射足以完成门禁：任何文本或二进制变更都会改变摘要。内容映射用于渲染
        运行 diff。受保护目录和忽略目录会被排除，因此只写入字节码缓存或沙箱临时目录
        的进程不会产生变更记录。
        """
        digests: dict[str, str] = {}
        contents: dict[str, str] = {}
        for file_path in self._iter_files(self._root):
            try:
                data = file_path.read_bytes()
            except OSError:
                continue
            normalized = file_path.relative_to(self._root).as_posix()
            digests[normalized] = sha256_bytes(data)
            if len(data) <= MAX_EDIT_FILE_BYTES:
                with contextlib.suppress(UnicodeDecodeError):
                    contents[normalized] = data.decode("utf-8")
        return WorkspaceSnapshot(
            digests=digests, contents=contents, protected_digests=self._protected_digests()
        )

    def _protected_digests(self) -> dict[str, str]:
        """为受保护路径计算摘要，使进程对其进行写入时能够被检测到，
        即使操作系统沙箱无法阻止该写入。"""
        result: dict[str, str] = {}
        for name in PROTECTED_COMPONENTS:
            target = self._root / name
            if target.is_file():
                with contextlib.suppress(OSError):
                    result[name] = sha256_bytes(target.read_bytes())
            elif target.is_dir():
                # 目录（例如 .git）：将其中的文件摘要折叠成一个，从而目录内
                # 任意变更都会改变聚合值。
                parts: list[str] = []
                for child in sorted(target.rglob("*")):
                    if child.is_file() and not child.is_symlink():
                        with contextlib.suppress(OSError):
                            rel = child.relative_to(self._root).as_posix()
                            parts.append(f"{rel}:{sha256_bytes(child.read_bytes())}")
                result[name] = sha256_text("\n".join(parts))
        return result

    def register_run_original(self, path: str, content: str) -> None:
        """为进程修改过的路径填充运行 diff 的原始内容，但仅限于该路径尚未被跟踪的
        情况——运行中更早编辑过的文件必须保留真正的运行开始内容，不能被重置为进程
        修改前的内容。"""
        if path not in self._originals:
            self._originals[path] = content
