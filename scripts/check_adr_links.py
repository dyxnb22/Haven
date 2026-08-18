"""断言被推翻的 ADR 指向推翻它的 ADR。

决策记录通常由刚好找到其中一篇的人逐篇阅读。因此，当后来的 ADR 修订、取代、
纠正或推翻较早的 ADR 时，较早的记录必须明确说明，否则读者带走的就会是过时的
推理。

设置此门禁是因为这种情况确实发生过：ADR 0026 反驳了 ADR 0009 的核心前提
（“分类错误会漏掉提示，但不会造成逃逸”），而 ADR 0009 没有指向它。这里只检查
强语义动词；ADR 可以自由引用其他记录作为上下文，不因此欠下反向链接。

    uv run python scripts/check_adr_links.py
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ADR_DIR = ROOT / "docs" / "adr"

#: 会改变早期 ADR 含义、因而必须建立反向链接的动词。
#: 特意排除 "extends" 和单纯引用：它们只是在决策上追加内容，并不会使
#: 读者据此采取行动的任何内容失效。
#:
#: `(?!\s+by\b)` 是关键约束，是这个门禁在自身仓库中失败后发现的。
#: “Corrected **by** ADR 0026” 是被动语态：该文档说明自己是被推翻的一方，
#: 所以这句话本身就是反向链接，不会产生额外义务。如果没有这个前瞻，ADR 0001
#: 的 “reversed by ADR 0009” 会被读成 0001 推翻 0009——把关系方向反过来了。
_STRONG = re.compile(
    r"\b(?:amend(?:s|ed|ment)?|supersed(?:e|es|ed)|correct(?:s|ed)|revers(?:e|es|ed))\b"
    r"(?!\s+by\b)"
    r"[^.\n]{0,40}?\bADR\s+(\d{4})",
    re.IGNORECASE,
)


def strong_references(text: str) -> set[int]:
    """本文声称要修订、取代、纠正或推翻的 ADR 编号。"""
    return {int(match) for match in _STRONG.findall(text)}


def backlink_problems(docs: dict[int, str]) -> list[str]:
    """找出每个强引用中、目标记录没有反向指向当前记录的引用。"""
    problems: list[str] = []
    for number, text in sorted(docs.items()):
        for target in sorted(strong_references(text)):
            if target == number:
                continue
            if target not in docs:
                problems.append(f"ADR {number:04d} references ADR {target:04d}, which is missing")
                continue
            if f"{number:04d}" not in docs[target]:
                problems.append(
                    f"ADR {target:04d} is overturned by ADR {number:04d} but never mentions it; "
                    f"add a pointer so a reader of {target:04d} alone is not misled"
                )
    return problems


def _load() -> dict[int, str]:
    docs: dict[int, str] = {}
    for path in sorted(ADR_DIR.glob("*.md")):
        match = re.match(r"(\d{4})-", path.name)
        if match:
            docs[int(match.group(1))] = path.read_text(encoding="utf-8")
    return docs


def collect_problems() -> list[str]:
    return backlink_problems(_load())


def main() -> int:
    problems = collect_problems()
    if problems:
        print(f"ADR cross-links: {len(problems)} problem(s)")
        for line in problems:
            print(f"  {line}")
        return 1
    print(f"ADR cross-links: {len(_load())} ADRs, every overturned one points forward")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
