"""文件编辑、事务 patch 和运行级 diff 的内部组件。"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

from haven.adapters.workspace_diff import WorkspaceRunDiff
from haven.adapters.workspace_patch import (
    MAX_CREATE_BYTES as MAX_CREATE_BYTES,
)
from haven.adapters.workspace_patch import (
    MAX_EDIT_FILE_BYTES as MAX_EDIT_FILE_BYTES,
)
from haven.adapters.workspace_patch import (
    PathFactsLookup as PathFactsLookup,
)
from haven.adapters.workspace_patch import (
    RequireInside as RequireInside,
)
from haven.adapters.workspace_patch import (
    WorkspacePatchEngine,
    apply_replacement,
    atomic_write,
    diff_preview,
    load_editable,
    validate_create_path,
)
from haven.domain.digest import sha256_bytes, sha256_text
from haven.domain.syntax_gate import (
    SyntaxDiagnostic,
    format_syntax_error,
    validate_syntax_async,
)
from haven.ports.workspace import (
    EditOutcome,
    EditPreview,
    EffectUnknownError,
    OriginalContent,
    PatchOpSpec,
    PatchPreview,
    PathFacts,
    RunDiff,
    WorkspaceError,
)


class WorkspaceEditor:
    """在工作区内执行带预览、摘要和可恢复原始内容的文件变更。

    在已授权路径上实施可预览、preimage 绑定和可回滚的编辑。
    """

    def __init__(
        self, root: Path, require_inside: RequireInside, path_facts: PathFactsLookup
    ) -> None:
        self._root = root
        self._require_inside = require_inside
        self._path_facts = path_facts
        self._run_diff = WorkspaceRunDiff(root)
        self._patch_engine = WorkspacePatchEngine(
            root=root,
            require_inside=require_inside,
            path_facts=path_facts,
            run_diff=self._run_diff,
        )

    async def preview_edit(
        self,
        path: str,
        old: str,
        new: str,
        *,
        occurrence: int | None = None,
        replace_all: bool = False,
        skip_syntax_check: bool = False,
    ) -> EditPreview:
        """读取文件并计算单次文本替换的预览，不写入磁盘。"""
        target, normalized = self._require_inside(path)
        text, preimage = await self._load_editable(target, normalized)
        new_text = self._apply_replacement(
            text, old, new, normalized, occurrence=occurrence, replace_all=replace_all
        )
        if not skip_syntax_check:
            syntax_diag = await self._check_syntax(normalized, new_text)
            if not syntax_diag.is_valid:
                raise WorkspaceError("syntax_error", format_syntax_error(syntax_diag))
        return await self._diff_preview(normalized, text, new_text, preimage)

    async def apply_edit(
        self,
        path: str,
        old: str,
        new: str,
        expected_preimage: str,
        *,
        occurrence: int | None = None,
        replace_all: bool = False,
        skip_syntax_check: bool = False,
    ) -> EditOutcome:
        """重新校验前像后原子写入编辑结果，防止 TOCTOU 覆盖并发修改。"""
        target, normalized = self._require_inside(path)
        text, preimage = await self._load_editable(target, normalized)
        if preimage != expected_preimage:
            raise WorkspaceError(
                "stale_preimage",
                f"file changed since it was read/approved: {normalized!r}",
            )
        new_text = self._apply_replacement(
            text, old, new, normalized, occurrence=occurrence, replace_all=replace_all
        )
        if not skip_syntax_check:
            syntax_diag = await self._check_syntax(normalized, new_text)
            if not syntax_diag.is_valid:
                raise WorkspaceError("syntax_error", format_syntax_error(syntax_diag))

        # Haven 自己的编辑路径经过 `load_editable`，大小与 UTF-8 都已校验，
        # 因此运行前内容必然拿得到——只有进程写入才可能留下 uncaptured。
        self._run_diff.remember(normalized, OriginalContent.captured(text))

        postimage = await self._atomic_write(target, normalized, new_text)
        return EditOutcome(path=normalized, preimage_digest=preimage, postimage_digest=postimage)

    # -- 创建 ------------------------------------------------------------------

    async def preview_create(
        self, path: str, content: str, *, skip_syntax_check: bool = False
    ) -> EditPreview:
        """校验新路径和大小限制，并生成创建差异预览。"""
        normalized = self._require_creatable(path, content)
        if not skip_syntax_check:
            syntax_diag = await self._check_syntax(normalized, content)
            if not syntax_diag.is_valid:
                raise WorkspaceError("syntax_error", format_syntax_error(syntax_diag))
        return await self._diff_preview(normalized, None, content, preimage="")

    async def apply_create(
        self, path: str, content: str, *, skip_syntax_check: bool = False
    ) -> EditOutcome:
        """原子创建新文件；已有目标或受保护路径会失败。"""
        normalized = self._require_creatable(path, content)
        if not skip_syntax_check:
            syntax_diag = await self._check_syntax(normalized, content)
            if not syntax_diag.is_valid:
                raise WorkspaceError("syntax_error", format_syntax_error(syntax_diag))
        target = self._root / normalized
        target.parent.mkdir(parents=True, exist_ok=True)

        try:
            postimage = await self._atomic_create(target, normalized, content)
        except EffectUnknownError:
            # 发布已经发生但后像无法确认；仍要把可能落盘的文件纳入运行差异。
            self._run_diff.remember(normalized, OriginalContent.missing())
            raise
        self._run_diff.remember(normalized, OriginalContent.missing())
        return EditOutcome(path=normalized, preimage_digest="", postimage_digest=postimage)

    # -- 删除 ------------------------------------------------------------------

    async def preview_delete(self, path: str) -> EditPreview:
        """读取并固定待删除文件的前像，返回从内容到空文件的差异。"""
        target, normalized = self._require_inside(path)
        text, preimage = await self._load_editable(target, normalized)
        # 删除就是从文件内容到空内容的 diff。
        return await self._diff_preview(normalized, text, None, preimage=preimage)

    async def apply_delete(self, path: str, expected_preimage: str) -> EditOutcome:
        """校验前像后删除文件，并将删除纳入运行级差异基线。"""
        target, normalized = self._require_inside(path)
        text, preimage = await self._load_editable(target, normalized)
        if preimage != expected_preimage:
            raise WorkspaceError(
                "stale_preimage", f"file changed since it was approved: {normalized!r}"
            )
        self._run_diff.remember(normalized, OriginalContent.captured(text))
        await self._unlink(target)
        # 空 postimage 表示删除，与 ledger 使用相同约定。
        return EditOutcome(path=normalized, preimage_digest=preimage, postimage_digest="")

    # -- 移动 ------------------------------------------------------------------

    async def preview_move(self, src: str, dest: str) -> tuple[EditPreview, EditPreview]:
        """同时生成源文件删除和目标文件创建两个预览。"""
        src_target, src_norm = self._require_inside(src)
        text, preimage = await self._load_editable(src_target, src_norm)
        dest_norm = self._require_creatable(dest, text)
        removal = await self._diff_preview(src_norm, text, None, preimage=preimage)
        addition = await self._diff_preview(dest_norm, None, text, preimage="")
        return removal, addition

    async def apply_move(
        self, src: str, dest: str, expected_preimage: str
    ) -> tuple[EditOutcome, EditOutcome]:
        """校验源前像后写入目标并删除源文件，不覆盖已有目标。"""
        src_target, src_norm = self._require_inside(src)
        text, preimage = await self._load_editable(src_target, src_norm)
        if preimage != expected_preimage:
            raise WorkspaceError(
                "stale_preimage", f"file changed since it was approved: {src_norm!r}"
            )
        dest_norm = self._require_creatable(dest, text)
        dest_target = self._root / dest_norm
        dest_target.parent.mkdir(parents=True, exist_ok=True)
        try:
            postimage = await self._atomic_create(dest_target, dest_norm, text)
        except EffectUnknownError:
            self._run_diff.remember(src_norm, OriginalContent.captured(text))
            self._run_diff.remember(dest_norm, OriginalContent.missing())
            raise
        self._run_diff.remember(src_norm, OriginalContent.captured(text))
        self._run_diff.remember(dest_norm, OriginalContent.missing())
        await self._unlink(src_target)
        removal = EditOutcome(path=src_norm, preimage_digest=preimage, postimage_digest="")
        addition = EditOutcome(path=dest_norm, preimage_digest="", postimage_digest=postimage)
        return removal, addition

    # -- 补丁（多文件、单事务）--------------------------------------------------

    async def preview_patch(
        self,
        ops: tuple[PatchOpSpec, ...],
        files_read: dict[str, str],
        *,
        skip_syntax_check: bool = False,
    ) -> PatchPreview:
        """在内存中模拟补丁并返回其确定性计划。"""
        return await self._patch_engine.preview_patch(
            ops, files_read, skip_syntax_check=skip_syntax_check
        )

    async def apply_patch(
        self, plan: PatchPreview, *, skip_syntax_check: bool = False
    ) -> tuple[EditOutcome, ...]:
        """提交计划中的补丁：验证每个固定值，暂存所有写入，然后重命名写入结果并删除待移除项。"""
        return await self._patch_engine.apply_patch(plan, skip_syntax_check=skip_syntax_check)

    def _require_creatable(self, path: str, content: str) -> str:
        """create 只适用于真正的新文件；覆盖已有文件必须通过 repo.edit，以便继续绑定 preimage。"""
        facts = self._validate_create_path(path, content)
        if facts.exists:
            kind = "directory" if facts.is_dir else "file"
            raise WorkspaceError(
                "invalid_arguments",
                f"{facts.normalized!r} already exists as a {kind}; use repo.edit to change it",
            )
        return facts.normalized

    def _validate_create_path(self, path: str, content: str) -> PathFacts:
        """校验创建路径与内容，但不要求磁盘目标当前不存在。"""
        return validate_create_path(path, content, self._path_facts)

    # -- 共享写入基础设施 -------------------------------------------------------

    async def _atomic_write(
        self, target: Path, normalized: str, new_text: str, *, mode: int | None = None
    ) -> str:
        """通过临时文件 + fsync + rename 写入，然后重新读取以确认结果。

        卸载到线程：fsync 加两次整文件读取在忙磁盘上要上百毫秒，而这是在事件循环里
        跑的——TUI 会为此卡住，stderr 排空和后台任务输出收集也会一起停。
        """
        return await asyncio.to_thread(atomic_write, target, normalized, new_text, mode=mode)

    async def _atomic_create(self, target: Path, normalized: str, new_text: str) -> str:
        """用硬链接原子发布新文件，保证审批后出现的目标永远不会被覆盖。

        同样卸载到线程，理由见 `_atomic_write`。
        """
        fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".haven-create-")
        try:
            os.fchmod(fd, 0o644)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(new_text)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(tmp_name, target)
            except FileExistsError as exc:
                raise WorkspaceError(
                    "stale_preimage", f"path appeared before create committed: {normalized!r}"
                ) from exc
        finally:
            Path(tmp_name).unlink(missing_ok=True)

        postimage = sha256_bytes(target.read_bytes())
        if postimage != sha256_text(new_text):
            raise EffectUnknownError(f"postimage mismatch after create: {normalized!r}")
        return postimage

    @staticmethod
    async def _check_syntax(path: str, content: str) -> SyntaxDiagnostic:
        """AST/结构化语法预检。

        大块走子进程：`ast.parse` 是纯 Python、占着 GIL，卸载到线程只能把 240 KB 文件的
        循环停顿从约 150 ms 降到约 85 ms（A/B 实测，噪声量级）。换成进程后实测 6.3 ms。
        小块的阈值判断和失败回退都在 `validate_syntax_async` 里。
        """
        return await validate_syntax_async(path, content)

    @staticmethod
    async def _diff_preview(
        normalized: str,
        before: str | None,
        after: str | None,
        preimage: str,
    ) -> EditPreview:
        """计算编辑差异预览；同为纯计算，理由见 `_check_syntax`。"""
        return await asyncio.to_thread(diff_preview, normalized, before, after, preimage)

    async def _load_editable(self, target: Path, normalized: str) -> tuple[str, str]:
        return await asyncio.to_thread(load_editable, target, normalized)

    @staticmethod
    async def _unlink(target: Path) -> None:
        await asyncio.to_thread(target.unlink)

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
        return apply_replacement(
            text, old, new, normalized, occurrence=occurrence, replace_all=replace_all
        )

    # -- 运行范围 diff ----------------------------------------------------------

    async def run_diff(self) -> RunDiff:
        """返回当前工作区相对本次运行基线的累计差异。"""
        return await self._run_diff.render()

    def original_contents(self) -> dict[str, OriginalContent]:
        """返回运行级差异组件保存的原始文件内容。"""
        return self._run_diff.original_contents()

    def restore_originals(self, originals: dict[str, OriginalContent]) -> None:
        """恢复运行级差异组件的原始内容索引，不直接写磁盘。"""
        self._run_diff.restore(originals)

    # -- 进程写入归因（ADR 0012）------------------------------------------------

    def register_run_original(self, path: str, content: OriginalContent) -> None:
        """为进程修改过的路径填充运行 diff 的原始内容。"""
        self._run_diff.remember(path, content)
