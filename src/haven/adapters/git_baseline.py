"""Capture the Git baseline of a workspace at run start."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class GitBaseline:
    is_repo: bool
    branch: str = ""
    commit: str = ""
    dirty_files: tuple[str, ...] = ()


async def capture_git_baseline(root: Path) -> GitBaseline:
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
