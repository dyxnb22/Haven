"""可搜索的历史会话选择器；只查询当前工作区的用户会话。"""

from dataclasses import dataclass
from typing import Literal

from textual import on, work
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Select, Static

from haven.interfaces.tui.presenter import sanitize
from haven.ports.session import SessionStorePort


@dataclass(frozen=True, slots=True)
class SessionSelection:
    run_id: str
    action: Literal["open", "resume", "fork"]


class SessionPickerScreen(ModalScreen[SessionSelection]):
    """搜索在存储中完成，避免只过滤最近一页记录。"""

    BINDINGS = [("escape", "close_picker", "Close")]
    DEFAULT_CSS = """
    SessionPickerScreen { align: center middle; }
    #session-box { width: 85%; height: auto; max-height: 90%; padding: 1 2;
                   border: thick $accent; background: $surface; }
    #session-box Button { width: 100%; }
    """

    def __init__(self, store: SessionStorePort, workspace_digest: str, query: str = "") -> None:
        super().__init__()
        self._store = store
        self._workspace_digest = workspace_digest
        self._query = query

    def compose(self) -> ComposeResult:
        with Vertical(id="session-box"):
            yield Static("[b]Sessions in this workspace[/b]")
            yield Input(
                value=self._query, placeholder="Search ID, goal or status", id="session-search"
            )
            yield Select[str]([], prompt="Select a session", id="session-choice")
            yield Static("", id="session-count")
            yield Button("Open history", id="session-open", variant="primary")
            yield Button("Resume interrupted task", id="session-resume")
            yield Button("Fork on next message", id="session-fork")
            yield Button("Close (Esc)", id="session-close")

    def on_mount(self) -> None:
        self._search(self._query)

    @on(Input.Changed, "#session-search")
    def _changed(self, event: Input.Changed) -> None:
        self._search(event.value)

    @work(exclusive=True, group="session-search")
    async def _search(self, query: str) -> None:
        records = await self._store.search_runs(query, 100, self._workspace_digest)
        if not self.is_mounted:
            return
        choices = self.query_one("#session-choice", Select)
        choices.set_options(
            (sanitize(f"{r.run_id}  [{r.status.value}]  {r.goal}", 180), r.run_id) for r in records
        )
        if records:
            choices.value = records[0].run_id
        self.query_one("#session-count", Static).update(
            f"{len(records)} session(s)"
            + (" — refine search to see more" if len(records) == 100 else "")
        )

    @on(Button.Pressed)
    def _select(self, event: Button.Pressed) -> None:
        action = {"session-open": "open", "session-resume": "resume", "session-fork": "fork"}.get(
            event.button.id or ""
        )
        if action is None:
            self.dismiss(None)
            return
        run_id = self.query_one("#session-choice", Select).value
        if isinstance(run_id, str):
            if action == "resume":
                self.dismiss(SessionSelection(run_id, "resume"))
            elif action == "fork":
                self.dismiss(SessionSelection(run_id, "fork"))
            else:
                self.dismiss(SessionSelection(run_id, "open"))

    def action_close_picker(self) -> None:
        self.dismiss(None)
