"""临时 Git 投机工作树隔离与原子合流（ADR 0041）。

为高复杂度或探索性运行提供零污染的隔离工作区。在工作树中完成所有探索、编辑与验证，
只有通过 Evidence Gate 时才将变更原子合并回主工作区；未通过或中途取消时瞬时清理，
保证主工作区 100% 保持洁净。
"""

from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from haven.adapters.git_baseline import capture_git_baseline
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
    diff_out, _ = await diff_proc.communicate()
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
        await apply_proc.communicate(input=diff_out)

    return EphemeralWorktree(repo_root=repo_root, worktree_path=target, run_id=run_id)


async def reconcile_worktree_to_main(
    worktree: EphemeralWorktree,
    changed_paths: list[str] | tuple[str, ...] | None = None,
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

    reconciled: list[str] = []
    for rel_path in changed_paths:
        src = wt_path / rel_path
        dst = repo_root / rel_path

        if src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            content = src.read_bytes()
            mode = src.stat().st_mode & 0o7777
            tmp = dst.parent / f".haven-tmp-{os.getpid()}-{rel_path.replace('/', '_')}"
            try:
                tmp.write_bytes(content)
                tmp.chmod(mode)
                tmp.replace(dst)
                reconciled.append(rel_path)
            finally:
                tmp.unlink(missing_ok=True)
        elif not src.exists() and dst.exists():
            dst.unlink(missing_ok=True)
            reconciled.append(rel_path)

    return sorted(set(reconciled))


async def cleanup_ephemeral_worktree(worktree: EphemeralWorktree) -> None:
    """清理并移除临时工作树，确保零残留。"""
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
        shutil.rmtree(worktree.worktree_path, ignore_errors=True)

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
