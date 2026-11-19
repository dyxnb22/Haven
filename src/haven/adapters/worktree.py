"""临时 Git 投机工作树隔离与原子合流（ADR 0041）。

为高复杂度或探索性运行提供零污染的隔离工作区。在工作树中完成所有探索、编辑与验证，
只有通过 Evidence Gate 时才将变更原子合并回主工作区；未通过或中途取消时瞬时清理，
保证主工作区 100% 保持洁净。
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from haven.adapters.git_baseline import capture_git_baseline
from haven.domain.digest import sha256_bytes
from haven.ports.workspace import WorkspaceError


@dataclass(frozen=True, slots=True)
class EphemeralWorktree:
    """临时投机工作树实例。"""

    #: 真实的原始仓库根目录。
    repo_root: Path
    #: 临时独立工作树所在的路径（例如 .haven/worktrees/<run_id>）。
    worktree_path: Path
    #: 运行 ID。
    run_id: str


async def is_git_repo(root: Path) -> bool:
    """检查路径是否位于有效的 Git 仓库中。"""
    baseline = await capture_git_baseline(root)
    return baseline.is_repo


async def reopen_ephemeral_worktree(repo_root: Path, path: Path) -> EphemeralWorktree:
    """重开本仓库中仍注册的受管工作树，不创建或重置任何文件。"""
    target = path.resolve()
    if target.parent != (repo_root / ".haven" / "worktrees").resolve() or not target.is_dir():
        raise WorkspaceError("invalid_arguments", "saved workspace is not a managed worktree")
    proc = await asyncio.create_subprocess_exec(
        "git",
        "worktree",
        "list",
        "--porcelain",
        "-z",
        cwd=repo_root,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    registered = stdout.decode("utf-8", errors="replace").split("\0")
    if proc.returncode != 0 or f"worktree {target}" not in registered:
        raise WorkspaceError(
            "invalid_arguments", "saved worktree is not registered in this repository"
        )
    return EphemeralWorktree(repo_root=repo_root, worktree_path=target, run_id=target.name)


async def create_ephemeral_worktree(repo_root: Path, run_id: str) -> EphemeralWorktree:
    """在 .haven/worktrees/<run_id> 下创建基于当前 HEAD 的临时独立工作树。"""
    if not await is_git_repo(repo_root):
        raise WorkspaceError("invalid_arguments", f"not a git repository: {repo_root}")

    worktrees_dir = repo_root / ".haven" / "worktrees"
    worktrees_dir.mkdir(parents=True, exist_ok=True)
    target = worktrees_dir / run_id

    if target.exists():
        shutil.rmtree(target, ignore_errors=True)

    cmd = ["git", "worktree", "add", "--detach", str(target), "HEAD"]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=repo_root,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        err_msg = stderr.decode("utf-8", errors="replace").strip()
        raise WorkspaceError(
            "internal", f"failed to create ephemeral git worktree at {target}: {err_msg}"
        )

    # 脏工作区支持：若主仓库存在未提交修改（tracked dirty diff），
    # 提取 diff 并应用到临时工作树，确保推测环境与开发者当前草稿状态对齐。
    diff_proc = await asyncio.create_subprocess_exec(
        "git",
        "diff",
        "HEAD",
        cwd=repo_root,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    diff_out, diff_err = await diff_proc.communicate()
    if diff_proc.returncode != 0:
        err_msg = diff_err.decode("utf-8", errors="replace").strip()
        await _discard(repo_root, target)
        raise WorkspaceError("internal", f"failed to read the draft diff: {err_msg}")
    if diff_out.strip():
        apply_proc = await asyncio.create_subprocess_exec(
            "git",
            "apply",
            "--whitespace=nowarn",
            cwd=target,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, apply_err = await apply_proc.communicate(input=diff_out)
        # 应用失败必须说出来。整个这一段的目的就是"让推测环境与开发者的草稿对齐"；
        # 悄悄返回一个没对齐的工作树，之后所有的编辑前像和证据都会建立在另一份
        # 文件状态上，而没有任何迹象表明它不是你眼前那份。
        if apply_proc.returncode != 0:
            err_msg = apply_err.decode("utf-8", errors="replace").strip()
            # 失败的工作树不能留在原地：它仍注册在 git 里，会一直被 `worktree list`
            # 看到，并挡住下一次用同一个 run_id 创建的尝试。
            await _discard(repo_root, target)
            raise WorkspaceError(
                "internal",
                f"failed to carry the uncommitted changes into the worktree: {err_msg}",
            )

    return EphemeralWorktree(repo_root=repo_root, worktree_path=target, run_id=run_id)


async def _discard(repo_root: Path, target: Path) -> None:
    """注销并删掉一个半成品工作树；清理失败不影响原始错误的传播。"""
    with contextlib.suppress(OSError):
        proc = await asyncio.create_subprocess_exec(
            "git",
            "worktree",
            "remove",
            "--force",
            str(target),
            cwd=repo_root,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()
    shutil.rmtree(target, ignore_errors=True)


async def reconcile_worktree_to_main(
    worktree: EphemeralWorktree,
    changed_paths: list[str] | tuple[str, ...] | None = None,
    *,
    expected_preimages: dict[str, str] | None = None,
    expected_postimages: dict[str, str] | None = None,
) -> list[str]:
    """将工作树中通过验证的文件变更原子同步回主仓库。"""
    repo_root = worktree.repo_root
    wt_path = worktree.worktree_path

    if changed_paths is None:
        cmd = ["git", "status", "--porcelain"]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=wt_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        paths: list[str] = []
        for line in out.decode("utf-8", errors="replace").splitlines():
            if len(line) > 3:
                raw = line[3:].strip()
                if " -> " in raw:
                    raw = raw.split(" -> ")[-1].strip()
                paths.append(raw)
        changed_paths = tuple(paths)

    staged: dict[Path, Path | None] = {}
    backups: dict[Path, tuple[bytes | None, int]] = {}
    sources: dict[Path, tuple[Path, str]] = {}
    committed: list[Path] = []

    def fingerprint(content: bytes | None) -> str:
        return sha256_bytes(content) if content is not None else ""

    try:
        for rel_path in sorted(set(changed_paths)):
            parts = Path(rel_path).parts
            if (
                Path(rel_path).is_absolute()
                or ".." in parts
                or any(part in {".git", ".haven", ".haven.toml"} for part in parts)
            ):
                raise WorkspaceError("denied", f"unsafe reconciliation path: {rel_path}")
            src, dst = wt_path / rel_path, repo_root / rel_path
            for root in (repo_root, wt_path):
                target = root
                for part in parts:
                    target = target / part
                    if target.is_symlink():
                        raise WorkspaceError(
                            "denied", f"symlink in reconciliation path: {rel_path}"
                        )
            if src.is_dir() or dst.is_dir():
                raise WorkspaceError("denied", "reconciliation expects regular files")
            before = dst.read_bytes() if dst.exists() else None
            after = src.read_bytes() if src.exists() else None
            if (
                expected_preimages is not None
                and fingerprint(before) != expected_preimages[rel_path]
            ):
                raise WorkspaceError("stale_preimage", f"main workspace changed: {rel_path}")
            if (
                expected_postimages is not None
                and fingerprint(after) != expected_postimages[rel_path]
            ):
                raise WorkspaceError("stale_preimage", f"verified worktree changed: {rel_path}")
            backups[dst] = (before, dst.stat().st_mode & 0o7777 if before is not None else 0o644)
            sources[dst] = (src, fingerprint(after))
            staged[dst] = None
            if after is not None:
                dst.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(
                    prefix=".haven-reconcile-", dir=dst.parent, delete=False
                ) as handle:
                    temporary = Path(handle.name)
                    staged[dst] = temporary
                    handle.write(after)
                # 审阅快照绑定文件内容，不把未审阅的 chmod 隐式带入主工作区。
                mode = (
                    backups[dst][1]
                    if expected_postimages is not None
                    else src.stat().st_mode & 0o7777
                )
                temporary.chmod(mode)
        # 所有文件准备完成后重新验证两侧，任何漂移都在开始合流前拒绝。
        for dst, (before, _) in backups.items():
            actual = dst.read_bytes() if dst.exists() else None
            src, expected = sources[dst]
            current = src.read_bytes() if src.exists() else None
            if fingerprint(actual) != fingerprint(before) or fingerprint(current) != expected:
                raise WorkspaceError(
                    "stale_preimage", "files changed while preparing reconciliation"
                )
        for dst, staged_path in staged.items():
            if staged_path is None:
                dst.unlink(missing_ok=True)
            else:
                staged_path.replace(dst)
            committed.append(dst)
    except BaseException:
        for dst in reversed(committed):
            before, mode = backups[dst]
            if before is None:
                dst.unlink(missing_ok=True)
            else:
                with tempfile.NamedTemporaryFile(
                    prefix=".haven-rollback-", dir=dst.parent, delete=False
                ) as handle:
                    restore = Path(handle.name)
                    handle.write(before)
                try:
                    restore.chmod(mode)
                    restore.replace(dst)
                finally:
                    restore.unlink(missing_ok=True)
        raise
    finally:
        for staged_path in staged.values():
            if staged_path is not None:
                staged_path.unlink(missing_ok=True)
    return sorted(str(dst.relative_to(repo_root)) for dst in committed)


async def cleanup_ephemeral_worktree(worktree: EphemeralWorktree) -> None:
    """清理并移除临时工作树，确保零残留。"""
    # 这里必须吞掉任何异常：`git worktree remove` 失败的原因很多——git 不在 PATH、
    # 目录已被外部删除、worktree 锁尚未释放——而每一种都应该退回到下面的手动
    # rmtree，而不是变成调用方的错误。收窄异常类型会让"git 缺失"这类最常见的情况
    # 重新冒出去。
    try:
        cmd = ["git", "worktree", "remove", "--force", str(worktree.worktree_path)]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=worktree.repo_root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
    except Exception:
        pass

    if worktree.worktree_path.exists():
        await asyncio.to_thread(shutil.rmtree, worktree.worktree_path, ignore_errors=True)

    # prune 只是让 git 忘记这条已消失的记录。文件已经删掉了，失败时没有更多可做的
    # 事，也不值得让清理函数因此抛错——调用方通常在 finally 里调它。
    try:
        proc_prune = await asyncio.create_subprocess_exec(
            "git",
            "worktree",
            "prune",
            cwd=worktree.repo_root,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc_prune.communicate()
    except Exception:
        pass
