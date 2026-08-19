"""捕获运行开始时工作区的 Git 基线。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class GitBaseline:
    """从 Git 工作树获取运行基线；Git 不可用时由上层决定是否继续。"""

    #: 工作区是否位于可用的 Git 工作树中。
    is_repo: bool
    #: 运行开始时记录的分支名；Git 不可用时为空。
    branch: str = ""
    #: 运行开始时记录的短提交标识。
    commit: str = ""
    #: 运行开始前就已存在未提交变更的文件。
    dirty_files: tuple[str, ...] = ()


async def capture_git_baseline(root: Path) -> GitBaseline:
    """读取当前分支和提交，构造用于审计的基线信息。"""
    branch = await _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if branch is None:
        return GitBaseline(is_repo=False)
    commit = await _git(root, "rev-parse", "HEAD") or ""
    porcelain = await _git(root, "status", "--porcelain") or ""
    dirty = tuple(line[3:].strip() for line in porcelain.splitlines() if len(line) > 3)
    return GitBaseline(is_repo=True, branch=branch, commit=commit[:12], dirty_files=dirty)


async def _git(root: Path, *args: str) -> str | None:
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return out.decode("utf-8", errors="replace").strip()
