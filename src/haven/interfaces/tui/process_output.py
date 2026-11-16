"""已保存日志的只读分页窗口，每页渲染最多 16000 个字符。"""

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from haven.interfaces.tui.presenter import sanitize

PAGE_CHARS = 16_000


class ProcessOutputScreen(ModalScreen[None]):
    BINDINGS = [("escape", "close_output", "Close")]
    DEFAULT_CSS = """
    ProcessOutputScreen { align: center middle; }
    #output-box { width: 90%; height: 90%; border: thick $accent; padding: 1; }
    #output-scroll { height: 1fr; }
    #output-text { height: auto; }
    #output-buttons { height: 3; }
    """

    def __init__(self, call_id: str, text: str) -> None:
        super().__init__()
        self._call_id, self._text, self._page = call_id, text, 0

    def compose(self) -> ComposeResult:
        with Vertical(id="output-box"):
            yield Static(sanitize(f"Saved output: {self._call_id}", 300))
            yield Static("Untrusted text. Capture status is recorded at the start of the log.")
            yield Static("", id="output-page")
            with VerticalScroll(id="output-scroll"):
                yield Static("", id="output-text")
            with Horizontal(id="output-buttons"):
                yield Button("Previous", id="output-prev")
                yield Button("Next", id="output-next")
                yield Button("Close (Esc)", id="output-close")

    def on_mount(self) -> None:
        self._render_page()

    def _render_page(self) -> None:
        start = self._page * PAGE_CHARS
        stop = start + PAGE_CHARS
        self.query_one("#output-text", Static).update(sanitize(self._text[start:stop], PAGE_CHARS))
        self.query_one("#output-page", Static).update(
            f"Page {self._page + 1}/{max(1, (len(self._text) + PAGE_CHARS - 1) // PAGE_CHARS)}"
        )
        self.query_one("#output-prev", Button).disabled = start == 0
        self.query_one("#output-next", Button).disabled = stop >= len(self._text)
        self.query_one("#output-scroll", VerticalScroll).scroll_home(animate=False)

    @on(Button.Pressed)
    def change_page(self, event: Button.Pressed) -> None:
        if event.button.id == "output-close":
            self.dismiss(None)
        elif event.button.id in {"output-prev", "output-next"}:
            self._page += 1 if event.button.id == "output-next" else -1
            self._render_page()

    def action_close_output(self) -> None:
        self.dismiss(None)
