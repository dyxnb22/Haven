"""不依赖 Textual 状态的输入转换。"""

import re
from pathlib import Path

_PROTECTED_PARTS = frozenset({".git", ".haven", ".haven.toml"})


def _is_safe_workspace_file(workspace: Path, mention: str) -> bool:
    """严格校验提及路径是否位于工作区内部且非受保护或逃逸文件。"""
    if not mention or mention.startswith(("/", "~")) or "\x00" in mention:
        return False
    try:
        resolved_ws = workspace.resolve()
        candidate = (workspace / mention).resolve()
        if candidate != resolved_ws and resolved_ws not in candidate.parents:
            return False
        rel = candidate.relative_to(resolved_ws)
        if any(part in _PROTECTED_PARTS for part in rel.parts):
            return False
        return candidate.is_file() and not candidate.is_symlink()
    except (ValueError, OSError, RuntimeError):
        return False


def expand_mentions(text: str, workspace: Path) -> str:
    """将存在的 @path 提及转换为明确的 repo.read 提示。"""
    mentions = re.findall(r"(?:^|\s)@([\w./-]+)", text)
    existing = [mention for mention in mentions if _is_safe_workspace_file(workspace, mention)]
    if not existing:
        return text
    note = " (mentioned files, read them first: " + ", ".join(dict.fromkeys(existing)) + ")"
    return text + note
