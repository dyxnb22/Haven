"""不依赖 Textual 状态的输入转换。"""

import re
from pathlib import Path


def expand_mentions(text: str, workspace: Path) -> str:
    """将存在的 @path 提及转换为明确的 repo.read 提示。"""
    mentions = re.findall(r"(?:^|\s)@([\w./-]+)", text)
    existing = [mention for mention in mentions if (workspace / mention).is_file()]
    if not existing:
        return text
    note = " (mentioned files, read them first: " + ", ".join(dict.fromkeys(existing)) + ")"
    return text + note
