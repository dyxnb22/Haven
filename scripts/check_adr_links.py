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
#: 动词与 `(?!\s+by\b)` 之间允许出现的间隔：跨句号不行（否则 "…see ADR 0003.
#: Supersedes ADR 0004." 会把两句搅在一起），但跨换行、跨多个空格、以及 1-3 位的
#: ADR 编号都必须允许。`[^.\n]` 那个版本把这三类全挡住了，于是一句里点名两个
#: ADR 时只有第一个产生义务，段落换行和短编号则让门禁整个失效。
#: 所有"关系动词"——强关系（推翻/修订）与 merely 修饰性的关系都要认，因为尾部必须在
#: 下一个关系动词前停住。ADR 0017 的状态行是 "amends ADR 0009, extends ADR 0012/0013"：
#: 逗号不能当边界（"ADR 0009, ADR 0025" 这种列举要靠它），于是 "extends ADR 0012" 会被
#: 扫进 amends 的尾部，报出一个假违规。
_RELATION_VERBS = (
    "amend(?:s|ed|ment)?|supersed(?:e|es|ed)|correct(?:s|ed)|revers(?:e|es|ed)"
    "|extend(?:s|ed|ing)?|preserv(?:e|es|ing)|deprecat(?:e|es|ed|ing)"
)

#: 强关系动词 + 其后直到子句末尾的文本。`(?!\s+by\b)` 是被动语态护栏："Corrected
#: **by** ADR 0026" 说明本文件才是被推翻的一方，那句话本身就是反向链接，不产生额外义务。
#:
#: 只匹配"动词 + 一个 ADR 编号"是不够的：一句"Supersedes ADR 0003 and ADR 0004." 里
#: 只有一个动词，`findall` 因此只产生一个义务，第二个被推翻的 ADR 永远不用挂反向链接
#: ——正是这个门禁当初要防的那种漂移。所以这里取动词后的整个子句，再从中抽取所有编号。
#:
#: 尾部用 tempered pattern 在下一个关系动词前收住：`;` `.` `\n` 是边界，而关系动词
#: 也是——否则 "amends ADR 0009, extends ADR 0012" 会把 extends 的目标一并算成被推翻。
_STRONG = re.compile(
    r"\b(?:amend(?:s|ed|ment)?|supersed(?:e|es|ed)|correct(?:s|ed)|revers(?:e|es|ed))\b"
    r"(?!\s+by\b)"
    rf"((?:(?!\b(?:{_RELATION_VERBS})\b)[^.;\n]){{0,60}})",
    re.IGNORECASE,
)

#: 子句里的 ADR 编号引用。允许 1-4 位：短编号（`ADR 9`）此前会让门禁整个失效。
_ADR_REF = re.compile(r"\bADR\s+(\d{1,4})\b", re.IGNORECASE)


def strong_references(text: str) -> set[int]:
    """本文声称要修订、取代、纠正或推翻的 ADR 编号。"""
    claimed: set[int] = set()
    for tail in _STRONG.findall(text):
        claimed.update(int(number) for number in _ADR_REF.findall(tail))
    return claimed


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
    """加载仓库中的 ADR 并返回所有反向链接问题。"""
    return backlink_problems(_load())


def main() -> int:
    """运行 ADR 交叉链接门禁并返回进程退出码。"""
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
