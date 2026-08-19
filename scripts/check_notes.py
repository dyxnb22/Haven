"""检查决策笔记是否包含使其值得保留的章节。

ADR 适合承载关键架构决策，却不适合承载一周内产生的二十个小决策——因此这些
决策一直存在聊天记录中，未来的读者（人类或代理）无法查阅。笔记是轻量级层：
一页内容，与代码在同一个变更中写入，并放在对应状态的目录下。

本门禁真正关心的章节是 *Alternatives considered*。记录某个决策否定了什么，才能
阻止相同的被拒绝想法每隔几个月卷土重来；没有这一点，笔记就只是代码描述。

    uv run python scripts/check_notes.py
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NOTES_DIR = ROOT / "docs" / "notes"

#: 每条笔记恰好位于其中一个目录，并随状态变化在目录之间移动。
#: 特意保留 `rejected/`：三个目录中它被重新阅读得最多。
VALID_STATES = ("proposed", "implemented", "rejected")

REQUIRED_SECTIONS = ("Context", "Decision", "Alternatives considered", "Consequences")


def missing_sections(text: str) -> list[str]:
    """按声明顺序返回笔记缺失的必需 `## ` 标题。"""
    present = {match.strip().lower() for match in re.findall(r"^##\s+(.+)$", text, re.MULTILINE)}
    return [section for section in REQUIRED_SECTIONS if section.lower() not in present]


def state_of(path: str) -> str | None:
    """笔记路径所属的生命周期状态；不属于任何状态时返回 None。"""
    parts = Path(path).parts
    for state in VALID_STATES:
        if state in parts:
            return state
    return None


def note_paths() -> list[Path]:
    """返回所有笔记文件，不包括 README 和模板。"""
    if not NOTES_DIR.is_dir():
        return []
    return sorted(
        path for path in NOTES_DIR.rglob("*.md") if path.name not in {"README.md", "TEMPLATE.md"}
    )


def collect_problems() -> list[str]:
    """以适合人类阅读的行返回发现的所有笔记问题。"""
    problems: list[str] = []
    for path in note_paths():
        relative = path.relative_to(ROOT).as_posix()
        if state_of(relative) is None:
            problems.append(f"{relative}: not in one of {'/'.join(VALID_STATES)}")
        missing = missing_sections(path.read_text(encoding="utf-8"))
        if missing:
            problems.append(f"{relative}: missing section(s): {', '.join(missing)}")
    return problems


def main() -> int:
    """运行决策笔记格式门禁并返回进程退出码。"""
    problems = collect_problems()
    if problems:
        print(f"decision notes: {len(problems)} problem(s)")
        for line in problems:
            print(f"  {line}")
        return 1
    print(f"decision notes: {len(note_paths())} note(s) well-formed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
