"""Assert that an overturned ADR points at the ADR that overturned it.

Decision records are read one at a time, usually by someone who found exactly
one of them. So when a later ADR amends, supersedes, corrects, or reverses an
earlier one, the earlier one has to say so — otherwise the stale reasoning is
what the reader takes away.

This gate exists because that happened: ADR 0026 refuted the central premise of
ADR 0009 ("a misclassification costs a skipped prompt, not an escape") and ADR
0009 carried no pointer to it. Only strong verbs are checked; an ADR may cite
another for context freely without owing it a backlink.

    uv run python scripts/check_adr_links.py
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ADR_DIR = ROOT / "docs" / "adr"

#: Verbs that change what an earlier ADR means, and therefore oblige a backlink.
#: "extends" and a bare citation are deliberately excluded: they add to a
#: decision without invalidating anything a reader would act on.
#:
#: The `(?!\s+by\b)` is load-bearing and was found by this gate failing on its
#: own repository. "Corrected **by** ADR 0026" is the passive form: the document
#: saying it is the one being overturned, so that sentence *is* the backlink and
#: creates no obligation. Without the lookahead, ADR 0001's "reversed by ADR
#: 0009" read as 0001 overturning 0009 — the relationship backwards.
_STRONG = re.compile(
    r"\b(?:amend(?:s|ed|ment)?|supersed(?:e|es|ed)|correct(?:s|ed)|revers(?:e|es|ed))\b"
    r"(?!\s+by\b)"
    r"[^.\n]{0,40}?\bADR\s+(\d{4})",
    re.IGNORECASE,
)


def strong_references(text: str) -> set[int]:
    """ADR numbers this text claims to amend, supersede, correct, or reverse."""
    return {int(match) for match in _STRONG.findall(text)}


def backlink_problems(docs: dict[int, str]) -> list[str]:
    """Every strong reference whose target does not point back."""
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
