"""查看会话已加载的项目指引；不重新读文件，也不调用模型。"""

from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from haven.contracts.guidance import GuidanceSnapshot
from haven.contracts.project_memory import (
    PROJECT_NOTE_LIMIT,
    PROJECT_NOTES_TOTAL_CHARS,
    render_project_notes,
)
from haven.interfaces.tui.presenter import sanitize


class ProjectMemoryScreen(ModalScreen[None]):
    BINDINGS = [("escape", "close_memory", "Close")]
    DEFAULT_CSS = """
    ProjectMemoryScreen { align: center middle; }
    #memory-box { width: 85%; height: 85%; padding: 1 2; border: thick $accent; }
    #memory-scroll { height: 1fr; }
    #memory-box Static { height: auto; }
    #memory-close { width: 100%; }
    """

    def __init__(self, snapshot: GuidanceSnapshot) -> None:
        super().__init__()
        self._snapshot = snapshot

    def compose(self) -> ComposeResult:
        snapshot = self._snapshot
        cap = f" / {snapshot.max_chars}" if snapshot.max_chars else ""
        sources = [
            f"{source.path} (scope: {source.scope}): {source.status}, "
            f"{source.included_chars} included chars"
            + (" [truncated]" if source.truncated else "")
            + (f" — {source.reason}" if source.reason else "")
            for source in snapshot.sources
        ]
        if snapshot.skipped_scopes:
            sources.append(f"{snapshot.skipped_scopes} scoped files omitted by the file limit.")
        if snapshot.unreported_sources:
            sources.append(f"{snapshot.unreported_sources} further source reports omitted.")
        if snapshot.per_file_chars:
            sources.append(
                f"Per-file limit: {snapshot.per_file_chars} chars / "
                f"{snapshot.per_file_lines} lines."
            )
        with Vertical(id="memory-box"):
            yield Static("[b]Project guidance — snapshot for new runs[/b]")
            yield Static(f"{len(snapshot.text)}{cap} characters", id="memory-size")
            yield Static(
                "Advisory data; permissions unchanged. Use /memory reload while idle. "
                "Resumed tasks keep their saved guidance."
            )
            with VerticalScroll(id="memory-scroll"):
                yield Static("[b]User-saved project notes[/b]")
                yield Static(
                    f"{len(snapshot.notes)}/{PROJECT_NOTE_LIMIT} notes; "
                    f"{sum(len(note.text) for note in snapshot.notes)}/"
                    f"{PROJECT_NOTES_TOTAL_CHARS} text characters. "
                    "Manage while idle: /remember TEXT, /memory forget ID, /memory clear."
                )
                yield Static(
                    sanitize(render_project_notes(snapshot.notes), 4000)
                    or "(no user-saved notes loaded)",
                    id="memory-notes",
                )
                yield Static("[b]Sources[/b]")
                yield Static(
                    sanitize(
                        "\n".join(sources) or "Source details unavailable for this session.", 8000
                    ),
                    id="memory-sources",
                )
                yield Static("[b]Loaded text[/b]")
                yield Static(
                    sanitize(snapshot.text, max(snapshot.max_chars, 4000))
                    or "(no project guidance loaded)",
                    id="memory-text",
                )
            yield Button("Close (Esc)", id="memory-close", variant="primary")

    @on(Button.Pressed, "#memory-close")
    def action_close_memory(self) -> None:
        self.dismiss(None)
