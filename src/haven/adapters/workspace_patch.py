"""事务性多文件 Patch 引擎与共享文件编辑底层基础设施。"""

from __future__ import annotations

import difflib
import os
import tempfile
from collections.abc import Callable
from pathlib import Path

from haven.adapters.workspace_diff import WorkspaceRunDiff
from haven.domain.digest import sha256_bytes, sha256_text
from haven.domain.syntax_gate import format_syntax_error, validate_syntax
from haven.ports.workspace import (
    EditOutcome,
    EditPreview,
    EffectUnknownError,
    OriginalContent,
    PatchEffect,
    PatchOpSpec,
    PatchPreview,
    PatchRollbackError,
    PathFacts,
    WorkspaceError,
)

MAX_EDIT_FILE_BYTES = 256 * 1024
MAX_CREATE_BYTES = 256 * 1024

RequireInside = Callable[[str], tuple[Path, str]]
PathFactsLookup = Callable[[str], PathFacts]


def diff_preview(
    normalized: str,
    before: str | None,
    after: str | None,
    preimage: str,
) -> EditPreview:
    """计算单个文件修改前后的统一差异预览。"""
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
    deletions = sum(1 for line in diff_lines if line.startswith("-") and not line.startswith("---"))
    return EditPreview(
        path=normalized,
        diff="".join(diff_lines),
        preimage_digest=preimage,
        postimage_digest=sha256_text(after) if after is not None else "",
        insertions=insertions,
        deletions=deletions,
    )


def _normalize_line(line: str) -> str:
    """去除行尾空白与各种换行符。"""
    return line.rstrip(" \t\r\n")


def _find_normalized_occurrence(text: str, old: str) -> tuple[int, int] | str | None:
    """尝试忽略行尾空白与换行符差异的安全容错匹配。

    若找到唯一匹配区间，返回 (start_char_idx, end_char_idx)；
    若匹配到多处，返回 'ambiguous'；
    若未找到，返回 None。
    """
    text_lines = text.splitlines(keepends=True)
    old_lines = old.splitlines(keepends=True)
    if not text_lines or not old_lines:
        return None

    k = len(old_lines)
    if k > len(text_lines):
        return None

    old_norm = [_normalize_line(line) for line in old_lines]
    matches: list[tuple[int, int]] = []
    for i in range(len(text_lines) - k + 1):
        window_norm = [_normalize_line(line) for line in text_lines[i : i + k]]
        if window_norm == old_norm:
            start_char = sum(len(line) for line in text_lines[:i])
            end_char = start_char + sum(len(line) for line in text_lines[i : i + k])
            matches.append((start_char, end_char))

    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        return "ambiguous"
    return None


def _find_closest_block_hint(text: str, old: str, max_preview_lines: int = 5) -> str:
    """为未能匹配的 old_string 生成最相似代码块的诊断定位提示。"""
    text_lines = text.splitlines()
    old_lines = old.splitlines()
    if not text_lines or not old_lines:
        return ""

    k = max(1, min(len(old_lines), len(text_lines)))
    best_ratio = 0.0
    best_idx = -1

    for i in range(len(text_lines) - k + 1):
        window = "\n".join(text_lines[i : i + k])
        ratio = difflib.SequenceMatcher(None, window, old).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_idx = i

    if best_ratio >= 0.4 and best_idx != -1:
        start_line = best_idx + 1
        end_line = best_idx + k
        sample = text_lines[best_idx : min(best_idx + max_preview_lines, len(text_lines))]
        preview = "\n".join(
            f"  {best_idx + idx + 1:4d} | {line}" for idx, line in enumerate(sample)
        )
        return (
            f"\nClosest matching lines around line {start_line}-{end_line} "
            f"(similarity {int(best_ratio * 100)}%):\n{preview}"
        )
    return ""


def apply_replacement(
    text: str,
    old: str,
    new: str,
    normalized: str,
    *,
    occurrence: int | None,
    replace_all: bool,
) -> str:
    """替换 `old` 的一个或全部出现位置。

    默认要求“必须唯一”，防止多处匹配意外破坏代码。
    `replace_all` 和 `occurrence` 是显式退出这一约束的方式。
    当精确匹配失败时，尝试安全的行尾空格与换行符规范化匹配；
    若依然失败，则提供最接近代码块的行号与诊断提示。
    """
    if replace_all and occurrence is not None:
        raise WorkspaceError("invalid_arguments", "set either replace_all or occurrence, not both")

    count = text.count(old)
    if count == 0:
        normalized_match = _find_normalized_occurrence(text, old)
        if normalized_match == "ambiguous":
            raise WorkspaceError(
                "ambiguous_match",
                f"old_string not found exactly, but matches multiple locations in {normalized!r} "
                "when ignoring trailing whitespace or newlines. Include more surrounding lines to "
                "make it unique.",
            )
        if isinstance(normalized_match, tuple):
            start_idx, end_idx = normalized_match
            if replace_all or occurrence is None or occurrence == 1:
                return text[:start_idx] + new + text[end_idx:]

        hint = _find_closest_block_hint(text, old)
        raise WorkspaceError(
            "not_found",
            f"old_string not found in {normalized!r}; re-read the file and copy the "
            f"exact text including indentation.{hint}",
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


def load_editable(target: Path, normalized: str) -> tuple[str, str]:
    """校验目标是否为小于大小上限的 UTF-8 普通文件，返回文本和 SHA256 摘要。"""
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
        raise WorkspaceError("invalid_arguments", f"not a UTF-8 text file: {normalized!r}") from exc
    return text, sha256_bytes(data)


def atomic_write(target: Path, normalized: str, new_text: str, *, mode: int | None = None) -> str:
    """通过临时文件 + fsync + rename 写入，然后重新读取确认后像摘要。"""
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
    finally:
        Path(tmp_name).unlink(missing_ok=True)

    postimage = sha256_bytes(target.read_bytes())
    if postimage != sha256_text(new_text):
        raise EffectUnknownError(f"postimage mismatch after write: {normalized!r}")
    return postimage


def validate_create_path(path: str, content: str, path_facts: PathFactsLookup) -> PathFacts:
    """校验创建路径与内容大小限制，确认位于工作区且不受保护。"""
    facts = path_facts(path)
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


class WorkspacePatchEngine:
    """事务性多文件 Patch 引擎。

    负责在内存中按操作顺序模拟树状态、检查前像一致性、生成确定性净效果预览，
    并在提交时执行两阶段提交与补偿回滚。
    """

    def __init__(
        self,
        root: Path,
        require_inside: RequireInside,
        path_facts: PathFactsLookup,
        run_diff: WorkspaceRunDiff,
    ) -> None:
        self._root = root
        self._require_inside = require_inside
        self._path_facts = path_facts
        self._run_diff = run_diff

    async def preview_patch(
        self,
        ops: tuple[PatchOpSpec, ...],
        files_read: dict[str, str],
        *,
        skip_syntax_check: bool = False,
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
                    text, digest = load_editable(target, normalized)
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
                state[normalized] = apply_replacement(
                    current,
                    op.old,
                    op.new,
                    normalized,
                    occurrence=op.occurrence,
                    replace_all=op.replace_all,
                )
            elif op.kind == "create":
                normalized = seed(op.path)
                validate_create_path(op.path, op.content, self._path_facts)
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
                validate_create_path(op.dest, moving, self._path_facts)
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
            preview = diff_preview(normalized, before_text, after_text, before_digest)
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
        if not skip_syntax_check:
            for path, text in final_contents.items():
                syntax_diag = validate_syntax(path, text)
                if not syntax_diag.is_valid:
                    msg = format_syntax_error(syntax_diag)
                    raise WorkspaceError(
                        "syntax_error",
                        f"patch validation failed for '{path}':\n{msg}",
                    )
        return PatchPreview(
            diff="".join(diffs),
            preimages=preimages,
            effects=tuple(effects),
            final_contents=final_contents,
            insertions=insertions,
            deletions=deletions,
        )

    async def apply_patch(
        self, plan: PatchPreview, *, skip_syntax_check: bool = False
    ) -> tuple[EditOutcome, ...]:
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

        if not skip_syntax_check:
            for path, text in plan.final_contents.items():
                syntax_diag = validate_syntax(path, text)
                if not syntax_diag.is_valid:
                    msg = format_syntax_error(syntax_diag)
                    raise WorkspaceError(
                        "syntax_error",
                        f"patch validation failed for '{path}':\n{msg}",
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
                        atomic_write(target, normalized, original, mode=mode)
                elif action == "unlink" and original is not None:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    atomic_write(target, normalized, original, mode=mode)

        try:
            for effect in writes:
                target = self._root / effect.path
                original = on_disk_text = None
                original_mode: int | None = None
                if effect.tool_shape == "repo.edit":
                    # 必须用 `load_editable` 而不是 `read_text`：后者以通用换行模式打开，
                    # 会把 CRLF 折成 LF。这份文本是回滚和运行 diff 的"运行前内容"，
                    # 折过一次之后，`haven rewind` 会按 LF 写回去——悄悄改掉文件的
                    # 换行风格。预览阶段的 `seed` 用的就是 `load_editable`，两边必须一致。
                    on_disk_text, _digest = load_editable(target, effect.path)
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
                    self._run_diff.remember(effect.path, OriginalContent.missing())
                    performed.append(("write", effect.path, None, None))
                    os.unlink(staged_name)
                    staged.pop(effect.path)
                else:
                    # 走到这里意味着 tool_shape 是 repo.edit，上面已经读过磁盘文本。
                    assert on_disk_text is not None
                    self._run_diff.remember(effect.path, OriginalContent.captured(on_disk_text))
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
                # 同写入分支：保留原始换行风格，回滚才是字节级的还原。
                original, _digest = load_editable(target, effect.path)
                original_mode = target.stat().st_mode & 0o7777
                self._run_diff.remember(effect.path, OriginalContent.captured(original))
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
