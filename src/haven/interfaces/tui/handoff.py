"""交接查看与五项笔记编辑；事实只读，保存绑定已查看的日志序号。"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Static, TextArea

from haven.application.handoff import HandoffView, render_handoff
from haven.contracts.handoff import HandoffNotes
from haven.interfaces.tui.presenter import sanitize


@dataclass(frozen=True, slots=True)
class HandoffAction:
    kind: Literal["edit", "save", "prepare", "clear"]
    notes: HandoffNotes | None = None


class HandoffScreen(ModalScreen[HandoffAction | None]):
    BINDINGS = [("escape", "close_handoff", "Close")]
    DEFAULT_CSS = """
    HandoffScreen { align: center middle; }
    #handoff-box { width: 90%; height: 90%; border: thick $accent; padding: 1; }
    #handoff-scroll { height: 1fr; }
    #handoff-scroll Static { height: auto; }
    #handoff-scroll TextArea { height: 5; }
    #handoff-controls { height: 3; }
    #handoff-controls Button { min-width: 10; width: 1fr; }
    #handoff-error { height: auto; color: $error; }
    """

    def __init__(
        self,
        view: HandoffView,
        *,
        editing: bool = False,
        editable: bool = False,
        history_text: str | None = None,
        save_notes: Callable[[HandoffNotes], Awaitable[str]] | None = None,
    ) -> None:
        super().__init__()
        self.view, self.editing, self.editable = view, editing, editable
        self.history_text, self.save_notes = history_text, save_notes

    def compose(self) -> ComposeResult:
        notes = self.view.snapshot.notes if self.view.snapshot else HandoffNotes()
        with Vertical(id="handoff-box"):
            yield Static("Handoff — ledger facts and untrusted working notes")
            yield Static("Notes: up to 600 characters each / 2000 total. No model call.")
            with VerticalScroll(id="handoff-scroll"):
                yield Static(
                    sanitize(self.history_text or render_handoff(self.view), 30_000),
                    id="handoff-view",
                )
                if self.editing:
                    for field in HandoffNotes.model_fields:
                        yield Static(field.replace("_", " ").title())
                        yield TextArea(
                            getattr(notes, field),
                            id=f"handoff-{field}",
                            read_only=not self.editable,
                        )
            yield Static("", id="handoff-error")
            with Horizontal(id="handoff-controls"):
                yield Button(
                    "Prepare",
                    id="handoff-prepare",
                    disabled=not self.editable or self.view.snapshot is not None,
                )
                yield Button("Edit", id="handoff-edit", disabled=not self.editable or self.editing)
                yield Button(
                    "Save",
                    id="handoff-save",
                    disabled=not self.editable or not self.editing,
                    variant="primary",
                )
                yield Button(
                    "Clear",
                    id="handoff-clear",
                    disabled=not self.editable or self.view.snapshot is None,
                )
                yield Button("Close", id="handoff-close")

    @on(Button.Pressed)
    async def choose(self, event: Button.Pressed) -> None:
        action = (event.button.id or "").removeprefix("handoff-")
        if action == "close":
            self.dismiss(None)
        elif action == "edit":
            self.dismiss(HandoffAction("edit"))
        elif action in {"prepare", "clear"}:
            self.dismiss(HandoffAction("prepare" if action == "prepare" else "clear"))
        elif action == "save":
            try:
                notes = HandoffNotes.model_validate(
                    {
                        field: self.query_one(f"#handoff-{field}", TextArea).text
                        for field in HandoffNotes.model_fields
                    }
                )
            except ValueError as exc:
                self.query_one("#handoff-error", Static).update(sanitize(str(exc), 500))
                return
            if self.save_notes is not None and (error := await self.save_notes(notes)):
                self.query_one("#handoff-error", Static).update(sanitize(error, 500))
                return
            self.dismiss(HandoffAction("save", notes))

    def action_close_handoff(self) -> None:
        self.dismiss(None)
