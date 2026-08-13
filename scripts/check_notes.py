"""Check that decision notes carry the sections that make them worth keeping.

An ADR is the right home for a load-bearing architectural call and the wrong
home for the twenty smaller decisions a week produces — so those decisions have
been living in chat logs, which no future reader (human or agent) can consult.
Notes are the lightweight tier: one page, written in the same change as the
code, filed under the state it is in.

The section this gate really exists for is *Alternatives considered*. A record
of what a decision defeated is what stops the same rejected idea coming back
every few months; without it a note is just a description of the code.

    uv run python scripts/check_notes.py
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NOTES_DIR = ROOT / "docs" / "notes"

#: A note lives in exactly one of these, and moves between them as it changes
#: state. `rejected/` is kept deliberately: it is the most re-read of the three.
VALID_STATES = ("proposed", "implemented", "rejected")

REQUIRED_SECTIONS = ("Context", "Decision", "Alternatives considered", "Consequences")


def missing_sections(text: str) -> list[str]:
    """Required `## ` headings absent from a note, in declaration order."""
    present = {match.strip().lower() for match in re.findall(r"^##\s+(.+)$", text, re.MULTILINE)}
    return [section for section in REQUIRED_SECTIONS if section.lower() not in present]


def state_of(path: str) -> str | None:
    """The lifecycle state a note's path puts it in, or None if it has none."""
    parts = Path(path).parts
    for state in VALID_STATES:
        if state in parts:
            return state
    return None


def note_paths() -> list[Path]:
    """Every note file, excluding the README and the template."""
    if not NOTES_DIR.is_dir():
        return []
    return sorted(
        path for path in NOTES_DIR.rglob("*.md") if path.name not in {"README.md", "TEMPLATE.md"}
    )


def collect_problems() -> list[str]:
    """Every note problem found, as human-readable lines."""
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
