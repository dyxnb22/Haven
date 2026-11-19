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
    branch = (await _git(root, "rev-parse", "--abbrev-ref", "HEAD") or "").strip()
    if not branch:
        return GitBaseline(is_repo=False)
    commit = (await _git(root, "rev-parse", "HEAD") or "").strip()
    porcelain = await _git(root, "status", "--porcelain", "-z") or ""
    return GitBaseline(
        is_repo=True, branch=branch, commit=commit[:12], dirty_files=_dirty_paths(porcelain)
    )


def _dirty_paths(porcelain: str) -> tuple[str, ...]:
    """从 `git status --porcelain -z` 的输出里取出所有涉及的文件路径。

    用 `-z` 而不是按行切：按行时 git 会给含空白的路径加 C 风格引号（
    `" leading.txt"`），还得自己反转义八进制字节；`-z` 根本不加引号。此前这里还有
    第二个 bug——`_git` 把整段输出 `.strip()` 了，于是 porcelain **第一行**的 `XY `
    前缀被削掉，`line[3:]` 便啃掉了第一个文件名的头一个字符（`normal.txt` 记成
    `ormal.txt`）。

    条目形如 `XY <路径>`；重命名与拷贝多带一个原始路径，形如
    `XY <新路径>\\0<原路径>\\0`。两边都属于"运行前就有未提交变更的文件"。
    """
    fields = porcelain.split("\0")
    paths: list[str] = []
    index = 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if len(entry) < 4:
            continue
        paths.append(entry[3:])
        if entry[0] in ("R", "C") and index < len(fields):
            paths.append(fields[index])
            index += 1
    return tuple(paths)


async def _git(root: Path, *args: str) -> str | None:
    """运行 git 并原样返回 stdout；失败返回 None。

    **不要在这里 strip**：调用方各自决定。对 `rev-parse` 削掉换行是对的，但对
    `status --porcelain` 削掉首部空白会毁掉第一行的状态列（见 `_dirty_paths`）。
    """
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
    return out.decode("utf-8", errors="replace")
