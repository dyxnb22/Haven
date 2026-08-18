"""文件编辑、事务 patch 和运行级 diff 的内部组件。"""

from __future__ import annotations

import difflib
import os
import tempfile
from collections.abc import Callable
from pathlib import Path

from haven.adapters.workspace_diff import WorkspaceRunDiff
from haven.domain.digest import sha256_bytes, sha256_text
from haven.ports.workspace import (
    EditOutcome,
    EditPreview,
    PatchEffect,
    PatchOpSpec,
    PatchPreview,
    PatchRollbackError,
    PathFacts,
    RunDiff,
    WorkspaceError,
)

MAX_EDIT_FILE_BYTES = 256 * 1024
MAX_CREATE_BYTES = 256 * 1024

RequireInside = Callable[[str], tuple[Path, str]]
PathFactsLookup = Callable[[str], PathFacts]


class WorkspaceEditor:
    """在已授权路径上实施可预览、preimage 绑定和可回滚的编辑。"""

    def __init__(
        self, root: Path, require_inside: RequireInside, path_facts: PathFactsLookup
    ) -> None:
        self._root = root
        self._require_inside = require_inside
        self._path_facts = path_facts
        self._run_diff = WorkspaceRunDiff(root)

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

        self._run_diff.remember(normalized, text)

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

        # 空的原始内容会使运行 diff 将该文件显示为新增。
        self._run_diff.remember(normalized, "")

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
        self._run_diff.remember(normalized, text)
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
        self._run_diff.remember(src_norm, text)
        self._run_diff.remember(dest_norm, "")
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
                self._run_diff.remember(effect.path, on_disk_text or "")
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
                self._run_diff.remember(effect.path, original)
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
        facts = self._path_facts(path)
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
        return await self._run_diff.render()

    def original_contents(self) -> dict[str, str]:
        return self._run_diff.original_contents()

    def restore_originals(self, originals: dict[str, str]) -> None:
        self._run_diff.restore(originals)

    # -- 进程写入归因（ADR 0012）------------------------------------------------

    def register_run_original(self, path: str, content: str) -> None:
        """为进程修改过的路径填充运行 diff 的原始内容，但仅限于该路径尚未被跟踪的
        情况——运行中更早编辑过的文件必须保留真正的运行开始内容，不能被重置为进程
        修改前的内容。"""
        self._run_diff.remember(path, content)
