"""决策笔记必须记录该决策击败了什么，而不只是记录决策内容。

ADR 承载关键架构决策，但大多数决策比 ADR 小，目前没有固定归处——它们存在未来
读者无法查阅的聊天记录中。笔记就是这个归处。唯一不能省略的章节是备选方案：不
记录决策否定了什么，就会邀请下一个想到相同点子的人重新争论。
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
        """对空目录运行门禁无法证明任何事情。"""
        from scripts.check_notes import note_paths

        assert len(note_paths()) >= 2


def test_the_template_is_a_valid_note() -> None:
    from pathlib import Path

    from scripts.check_notes import ROOT

    template = Path(ROOT) / "docs" / "notes" / "TEMPLATE.md"
    if not template.is_file():
        pytest.fail("docs/notes/TEMPLATE.md must exist so a note has a starting point")
    assert missing_sections(template.read_text(encoding="utf-8")) == []


def test_a_missing_notes_directory_fails_instead_of_passing() -> None:
    """目录不存在必须是"检查没能运行"，不是"一切正常"。

    一次目录搬迁或一次坏合并会让所有笔记落到被检查的树之外，而
    `0 note(s) well-formed` + 退出码 0 会让这件事无声无息地通过。`evalkit/runner.py`
    对同样的情形就是抛错的那个。
    """
    import scripts.check_notes as notes

    original = notes.NOTES_DIR
    notes.NOTES_DIR = original.parent / "does-not-exist-xyz"
    try:
        assert notes.notes_dir_missing() is True
        assert notes.main() == 1
    finally:
        notes.NOTES_DIR = original
    # 恢复之后必须仍然正常
    assert notes.notes_dir_missing() is False
