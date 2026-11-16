"""用户显式保存的项目笔记；有界资料，不携带执行权限。"""

from typing import Literal

from pydantic import Field

from haven.contracts.base import StrictModel

PROJECT_NOTE_LIMIT = 20
PROJECT_NOTE_MAX_CHARS = 500
PROJECT_NOTES_TOTAL_CHARS = 2000
NOTE_CAPACITY_MESSAGE = (
    "project notes are full (20 notes / 2000 total characters); delete a note first"
)
NoteAction = Literal["remember", "forget", "clear"]


class ProjectNote(StrictModel):
    id: int = Field(ge=1)
    text: str = Field(min_length=1, max_length=PROJECT_NOTE_MAX_CHARS)
    created_at: str


def normalize_note_text(text: str) -> str:
    normalized = text.strip()
    if not normalized or len(normalized) > PROJECT_NOTE_MAX_CHARS or "\x00" in normalized:
        raise ValueError("a project note must contain 1–500 characters and no NUL bytes")
    return normalized


def render_project_notes(notes: tuple[ProjectNote, ...]) -> str:
    return "\n".join(f"Note {note.id} ({note.created_at}): {note.text}" for note in notes)
