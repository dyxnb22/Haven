"""工作区与受保护路径的进程写入归因快照。"""

import contextlib
from pathlib import Path

from haven.adapters.workspace_search import iter_workspace_files
from haven.domain.digest import sha256_bytes, sha256_text
from haven.ports.workspace import WorkspaceSnapshot


def capture_workspace_snapshot(
    *,
    root: Path,
    max_text_bytes: int,
    ignored_dirs: frozenset[str],
    protected_components: frozenset[str],
    protected_digests: dict[str, str] | None = None,
) -> WorkspaceSnapshot:
    """扫描工作区文件摘要，并为可解码的小文件保留内容快照。

    `protected_digests` 非 None 时直接采用，跳过对受保护目录的全量内容哈希：那是
    整次快照里最贵的部分，而调用方（进程工具）往往已经算过一遍。
    """
    digests: dict[str, str] = {}
    contents: dict[str, str] = {}
    for file_path in iter_workspace_files(root, ignored_dirs, protected_components):
        try:
            data = file_path.read_bytes()
        except OSError:
            continue
        normalized = file_path.relative_to(root).as_posix()
        digests[normalized] = sha256_bytes(data)
        if len(data) <= max_text_bytes:
            with contextlib.suppress(UnicodeDecodeError):
                contents[normalized] = data.decode("utf-8")
    return WorkspaceSnapshot(
        digests=digests,
        contents=contents,
        protected_digests=(
            _protected_digests(root, protected_components)
            if protected_digests is None
            else protected_digests
        ),
    )


def _protected_digests(root: Path, protected_components: frozenset[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for name in protected_components:
        target = root / name
        if target.is_file():
            with contextlib.suppress(OSError):
                result[name] = sha256_bytes(target.read_bytes())
        elif target.is_dir():
            parts: list[str] = []
            for child in sorted(target.rglob("*")):
                if child.is_file() and not child.is_symlink():
                    with contextlib.suppress(OSError):
                        rel = child.relative_to(root).as_posix()
                        parts.append(f"{rel}:{sha256_bytes(child.read_bytes())}")
            result[name] = sha256_text("\n".join(parts))
    return result
