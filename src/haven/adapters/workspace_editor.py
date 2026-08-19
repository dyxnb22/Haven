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
    EffectUnknownError,
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

    async def preview_edit(
        self,
        path: str,
        old: str,
        new: str,
        *,
        occurrence: int | None = None,
        replace_all: bool = False,
    ) -> EditPreview:
        """读取文件并计算单次文本替换的预览，不写入磁盘。"""
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
        """重新校验前像后原子写入编辑结果，防止 TOCTOU 覆盖并发修改。"""
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
        """校验新路径和大小限制，并生成创建差异预览。"""
        normalized = self._require_creatable(path, content)
        return self._diff_preview(normalized, None, content, preimage="")

    async def apply_create(self, path: str, content: str) -> EditOutcome:
        """原子创建新文件；已有目标或受保护路径会失败。"""
        normalized = self._require_creatable(path, content)
        target = self._root / normalized
        target.parent.mkdir(parents=True, exist_ok=True)

        try:
            postimage = self._atomic_create(target, normalized, content)
        except EffectUnknownError:
            # 发布已经发生但后像无法确认；仍要把可能落盘的文件纳入运行差异。
            self._run_diff.remember(normalized, None)
            raise
        self._run_diff.remember(normalized, None)
        return EditOutcome(path=normalized, preimage_digest="", postimage_digest=postimage)

    # -- 删除 ------------------------------------------------------------------

    async def preview_delete(self, path: str) -> EditPreview:
        """读取并固定待删除文件的前像，返回从内容到空文件的差异。"""
        target, normalized = self._require_inside(path)
        text, preimage = self._load_editable(target, normalized)
        # 删除就是从文件内容到空内容的 diff。
        return self._diff_preview(normalized, text, None, preimage=preimage)

    async def apply_delete(self, path: str, expected_preimage: str) -> EditOutcome:
        """校验前像后删除文件，并将删除纳入运行级差异基线。"""
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
        """同时生成源文件删除和目标文件创建两个预览。"""
        src_target, src_norm = self._require_inside(src)
        text, preimage = self._load_editable(src_target, src_norm)
        dest_norm = self._require_creatable(dest, text)
        removal = self._diff_preview(src_norm, text, None, preimage=preimage)
        addition = self._diff_preview(dest_norm, None, text, preimage="")
        return removal, addition

    async def apply_move(
        self, src: str, dest: str, expected_preimage: str
    ) -> tuple[EditOutcome, EditOutcome]:
        """校验源前像后写入目标并删除源文件，不覆盖已有目标。"""
        src_target, src_norm = self._require_inside(src)
        text, preimage = self._load_editable(src_target, src_norm)
        if preimage != expected_preimage:
            raise WorkspaceError(
                "stale_preimage", f"file changed since it was approved: {src_norm!r}"
            )
        dest_norm = self._require_creatable(dest, text)
        dest_target = self._root / dest_norm
        dest_target.parent.mkdir(parents=True, exist_ok=True)
        try:
            postimage = self._atomic_create(dest_target, dest_norm, text)
        except EffectUnknownError:
            self._run_diff.remember(src_norm, text)
            self._run_diff.remember(dest_norm, None)
            raise
        self._run_diff.remember(src_norm, text)
        self._run_diff.remember(dest_norm, None)
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
            """将路径载入补丁模拟状态，并记录磁盘初始文本和摘要。"""
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
                normalized = seed(op.path)
                self._validate_create_path(op.path, op.content)
                if state[normalized] is not None:
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
                dest_norm = seed(op.dest)
                self._validate_create_path(op.dest, moving)
                if state[dest_norm] is not None:
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
            preview = self._diff_preview(normalized, before_text, after_text, before_digest)
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
                mode = target.stat().st_mode & 0o7777 if effect.tool_shape == "repo.edit" else 0o644
                os.fchmod(fd, mode)
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
        performed: list[tuple[str, str, str | None, int | None]] = []
        outcomes: list[EditOutcome] = []

        def rollback() -> None:
            """按已执行动作的逆序恢复补丁提交前的文件状态。"""
            for action, normalized, original, mode in reversed(performed):
                target = self._root / normalized
                if action == "write":
                    if original is None:
                        target.unlink(missing_ok=True)
                    else:
                        self._atomic_write(target, normalized, original, mode=mode)
                elif action == "unlink" and original is not None:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    self._atomic_write(target, normalized, original, mode=mode)

        try:
            for effect in writes:
                target = self._root / effect.path
                original = on_disk_text = None
                original_mode: int | None = None
                if effect.tool_shape == "repo.edit":
                    on_disk_text = target.read_text(encoding="utf-8")
                    original = on_disk_text
                    original_mode = target.stat().st_mode & 0o7777
                staged_name = staged[effect.path]
                if effect.tool_shape == "repo.create":
                    try:
                        os.link(staged_name, target)
                    except FileExistsError as exc:
                        raise WorkspaceError(
                            "stale_preimage",
                            f"path appeared while the patch was committing: {effect.path!r}",
                        ) from exc
                    self._run_diff.remember(effect.path, None)
                    performed.append(("write", effect.path, None, None))
                    os.unlink(staged_name)
                    staged.pop(effect.path)
                else:
                    self._run_diff.remember(effect.path, on_disk_text)
                    os.replace(staged.pop(effect.path), target)
                    performed.append(("write", effect.path, original, original_mode))
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
                original_mode = target.stat().st_mode & 0o7777
                self._run_diff.remember(effect.path, original)
                target.unlink()
                performed.append(("unlink", effect.path, original, original_mode))
                outcomes.append(
                    EditOutcome(
                        path=effect.path,
                        preimage_digest=effect.preimage_digest,
                        postimage_digest="",
                    )
                )
        except (OSError, WorkspaceError, EffectUnknownError) as exc:
            try:
                rollback()
            except (OSError, WorkspaceError, EffectUnknownError) as rollback_exc:
                # 目录树现在处于无法撤销的部分状态：必须将其暴露为 unknown 副作用，
                # 不能作为干净失败处理，以便恢复逻辑阻止继续运行，由用户调和。
                raise PatchRollbackError(
                    f"patch failed ({exc}) and rollback also failed ({rollback_exc}); "
                    "the workspace is in a partial state"
                ) from exc
            if isinstance(exc, WorkspaceError):
                raise
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
        facts = self._validate_create_path(path, content)
        if facts.exists:
            kind = "directory" if facts.is_dir else "file"
            raise WorkspaceError(
                "invalid_arguments",
                f"{facts.normalized!r} already exists as a {kind}; use repo.edit to change it",
            )
        return facts.normalized

    def _validate_create_path(self, path: str, content: str) -> PathFacts:
        """校验创建路径与内容，但不要求磁盘目标当前不存在。

        补丁模拟需要这个较窄的校验：前序 delete 后的 create 应依据模拟状态，
        而不能被仍未提交到磁盘的旧文件误判为冲突。
        """
        facts = self._path_facts(path)
        if not facts.within_workspace:
            raise WorkspaceError("denied", f"path escapes the workspace: {path!r}")
        if facts.is_protected:
            raise WorkspaceError("denied", f"path is protected: {facts.normalized!r}")
        if facts.normalized in (".", ""):
            raise WorkspaceError("invalid_arguments", "a file path is required")
        if len(content.encode("utf-8")) > MAX_CREATE_BYTES:
            raise WorkspaceError(
                "invalid_arguments", f"content too large to create: {facts.normalized!r}"
            )
        return facts

    # -- 共享写入基础设施 -------------------------------------------------------

    def _atomic_write(
        self, target: Path, normalized: str, new_text: str, *, mode: int | None = None
    ) -> str:
        """通过临时文件 + fsync + rename 写入，然后重新读取以确认结果。

        成功的 write() 不是证据；postimage 摘要才是。
        """
        fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".haven-write-")
        try:
            if mode is None:
                mode = target.stat().st_mode & 0o7777 if target.exists() else 0o644
            os.fchmod(fd, mode)
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
            raise EffectUnknownError(f"postimage mismatch after write: {normalized!r}")
        return postimage

    def _atomic_create(self, target: Path, normalized: str, new_text: str) -> str:
        """用硬链接原子发布新文件，保证审批后出现的目标永远不会被覆盖。"""
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
    def _diff_preview(
        normalized: str,
        before: str | None,
        after: str | None,
        preimage: str,
    ) -> EditPreview:
        diff_lines = list(
            difflib.unified_diff(
                (before or "").splitlines(keepends=True),
                (after or "").splitlines(keepends=True),
                fromfile=f"a/{normalized}" if before is not None else "/dev/null",
                tofile=f"b/{normalized}" if after is not None else "/dev/null",
            )
        )
        if before != after and not diff_lines:
            diff_lines = [
                f"--- {'a/' + normalized if before is not None else '/dev/null'}\n",
                f"+++ {'b/' + normalized if after is not None else '/dev/null'}\n",
            ]
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
            postimage_digest=sha256_text(after) if after is not None else "",
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
        """返回当前工作区相对本次运行基线的累计差异。"""
        return await self._run_diff.render()

    def original_contents(self) -> dict[str, str | None]:
        """返回运行级差异组件保存的原始文件内容。"""
        return self._run_diff.original_contents()

    def restore_originals(self, originals: dict[str, str | None]) -> None:
        """恢复运行级差异组件的原始内容索引，不直接写磁盘。"""
        self._run_diff.restore(originals)

    # -- 进程写入归因（ADR 0012）------------------------------------------------

    def register_run_original(self, path: str, content: str | None) -> None:
        """为进程修改过的路径填充运行 diff 的原始内容，但仅限于该路径尚未被跟踪的
        情况——运行中更早编辑过的文件必须保留真正的运行开始内容，不能被重置为进程
        修改前的内容。"""
        self._run_diff.remember(path, content)
