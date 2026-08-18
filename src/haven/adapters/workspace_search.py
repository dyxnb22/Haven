"""文件系统工作区的搜索后端，不负责路径授权。"""

from __future__ import annotations

import asyncio
import os
import re
import time
from pathlib import Path

from haven.ports.workspace import SearchMatch, SearchResult, WorkspaceError

MAX_SEARCH_FILE_BYTES = 1024 * 1024
MAX_SEARCH_TOTAL_BYTES = 64 * 1024
MAX_SEARCH_LINE_CHARS = 240
_DEADLINE_CHECK_LINES = 256


async def search_workspace(
    *,
    root: Path,
    ripgrep: str | None,
    pattern: str,
    target: Path,
    normalized: str,
    max_results: int,
    timeout_seconds: float,
    ignored_dirs: frozenset[str],
    protected_components: frozenset[str],
) -> SearchResult:
    """搜索已由调用方验证位于工作区内的目标。"""
    try:
        re.compile(pattern)
    except re.error as exc:
        raise WorkspaceError("invalid_arguments", f"invalid regex: {exc}") from exc
    if not target.exists():
        raise WorkspaceError("not_found", f"no such path to search: {normalized!r}")

    if ripgrep is not None:
        return await _search_ripgrep(
            root=root,
            ripgrep=ripgrep,
            pattern=pattern,
            target=target,
            max_results=max_results,
            timeout_seconds=timeout_seconds,
            ignored_dirs=ignored_dirs,
            protected_components=protected_components,
        )
    return _search_walk(
        root=root,
        pattern=pattern,
        target=target,
        max_results=max_results,
        timeout_seconds=timeout_seconds,
        ignored_dirs=ignored_dirs,
        protected_components=protected_components,
    )


async def _search_ripgrep(
    *,
    root: Path,
    ripgrep: str,
    pattern: str,
    target: Path,
    max_results: int,
    timeout_seconds: float,
    ignored_dirs: frozenset[str],
    protected_components: frozenset[str],
) -> SearchResult:
    argv = [
        ripgrep,
        "--line-number",
        "--no-heading",
        "--with-filename",
        "--color=never",
        "--sort=path",
        "--no-require-git",
        f"--max-filesize={MAX_SEARCH_FILE_BYTES}",
        *(f"--glob=!{name}" for name in sorted(ignored_dirs | protected_components)),
        f"--regexp={pattern}",
        "--",
        str(target),
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
        )
        raw, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except (OSError, TimeoutError):
        return _search_walk(
            root=root,
            pattern=pattern,
            target=target,
            max_results=max_results,
            timeout_seconds=timeout_seconds,
            ignored_dirs=ignored_dirs,
            protected_components=protected_components,
        )
    if proc.returncode not in (0, 1, 2) or (proc.returncode == 2 and not raw.strip()):
        return _search_walk(
            root=root,
            pattern=pattern,
            target=target,
            max_results=max_results,
            timeout_seconds=timeout_seconds,
            ignored_dirs=ignored_dirs,
            protected_components=protected_components,
        )

    matches: list[SearchMatch] = []
    seen_files: set[str] = set()
    total_bytes = 0
    truncated = False
    for line in raw.decode("utf-8", errors="replace").splitlines():
        parsed = _parse_ripgrep_line(root, line)
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
    return SearchResult(matches=tuple(matches), files_scanned=len(seen_files), truncated=truncated)


def _parse_ripgrep_line(root: Path, line: str) -> tuple[str, int, str] | None:
    head, sep, text = line.partition(":")
    if not sep:
        return None
    number, sep, text = text.partition(":")
    if not sep or not number.isdigit():
        return None
    try:
        rel = Path(head).resolve().relative_to(root).as_posix()
    except ValueError:
        return None
    return rel, int(number), text


def _search_walk(
    *,
    root: Path,
    pattern: str,
    target: Path,
    max_results: int,
    timeout_seconds: float,
    ignored_dirs: frozenset[str],
    protected_components: frozenset[str],
) -> SearchResult:
    compiled = re.compile(pattern)
    matches: list[SearchMatch] = []
    seen_files: set[str] = set()
    total_bytes = 0
    truncated = False
    deadline = time.monotonic() + timeout_seconds

    for file_path in iter_workspace_files(target, ignored_dirs, protected_components):
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
            continue
        text = data.decode("utf-8", errors="replace")
        rel = file_path.relative_to(root).as_posix()
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
    return SearchResult(matches=tuple(matches), files_scanned=len(seen_files), truncated=truncated)


def iter_workspace_files(
    target: Path, ignored_dirs: frozenset[str], protected_components: frozenset[str]
) -> list[Path]:
    """确定性列举普通文件，供搜索与快照共享。"""
    if target.is_file():
        return [target]
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(target):
        dirnames[:] = sorted(
            name
            for name in dirnames
            if name not in protected_components and name not in ignored_dirs
        )
        for name in sorted(filenames):
            if name in protected_components:
                continue
            candidate = Path(dirpath) / name
            if candidate.is_file() and not candidate.is_symlink():
                found.append(candidate)
    return found
