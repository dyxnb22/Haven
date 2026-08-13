"""Decision notes must record what the decision beat, not just what it was.

ADRs carry the load-bearing architectural calls, but most decisions are smaller
than an ADR and currently have no home — they live in a chat log that no future
reader has. A note is that home. The one section it cannot omit is the
alternatives: a decision with no record of what it defeated invites being
re-litigated by the next person who has the same idea.
"""

import pytest

from scripts.check_notes import REQUIRED_SECTIONS, VALID_STATES, missing_sections, state_of

GOOD = """# Some decision

## Context
Why this came up.

## Decision
What we do now.

## Alternatives considered
- The other thing, rejected because ...

## Consequences
What this costs.
"""


class TestRequiredSections:
    def test_a_complete_note_is_accepted(self) -> None:
        assert missing_sections(GOOD) == []

    def test_a_note_without_alternatives_is_rejected(self) -> None:
        text = GOOD.replace("## Alternatives considered", "## Something else")
        assert "Alternatives considered" in missing_sections(text)

    def test_every_required_section_is_checked(self) -> None:
        assert missing_sections("# Bare title\n") == list(REQUIRED_SECTIONS)


class TestLifecycle:
    def test_the_state_comes_from_the_directory(self) -> None:
        assert state_of("docs/notes/implemented/0001-a-thing.md") == "implemented"
        assert state_of("docs/notes/rejected/0002-not-this.md") == "rejected"

    def test_an_unknown_directory_has_no_state(self) -> None:
        assert state_of("docs/notes/scratch/0003-x.md") is None

    def test_the_three_states_are_the_lifecycle(self) -> None:
        assert set(VALID_STATES) == {"proposed", "implemented", "rejected"}


class TestTheRealNotes:
    def test_every_shipped_note_passes_its_own_gate(self) -> None:
        from scripts.check_notes import collect_problems

        assert collect_problems() == []

    def test_notes_actually_exist(self) -> None:
        """A gate over an empty directory proves nothing."""
        from scripts.check_notes import note_paths

        assert len(note_paths()) >= 2


def test_the_template_is_a_valid_note() -> None:
    from pathlib import Path

    from scripts.check_notes import ROOT

    template = Path(ROOT) / "docs" / "notes" / "TEMPLATE.md"
    if not template.is_file():
        pytest.fail("docs/notes/TEMPLATE.md must exist so a note has a starting point")
    assert missing_sections(template.read_text(encoding="utf-8")) == []
