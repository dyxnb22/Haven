"""逐文件、逐 hunk 的只读审阅界面。"""

from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Select, Static

from haven.application.diff_review import FileDiff, parse_diff
from haven.interfaces.tui.presenter import sanitize


class DiffReviewScreen(ModalScreen[bool]):
    BINDINGS = [("escape", "keep", "Close / keep isolated")]
    DEFAULT_CSS = """
    DiffReviewScreen { align: center middle; }
    #review-box { width: 95%; height: 95%; border: thick $accent; padding: 0 1; }
    #review-content { height: 1fr; }
    #review-actions { height: auto; }
    """

    def __init__(
        self,
        diff: str,
        *,
        confirm: bool = False,
        binding: str = "",
        selected_file: str = "",
        truncated: bool = False,
    ) -> None:
        super().__init__()
        parsed = parse_diff(diff)
        self._files = parsed or (
            FileDiff("Changes", diff or "No changes", (diff or "No changes",)),
        )
        self._confirm = confirm
        self._binding = binding
        self._selected = (
            selected_file
            if any(f.path == selected_file for f in self._files)
            else self._files[0].path
        )
        self._truncated = truncated

    def compose(self) -> ComposeResult:
        with Vertical(id="review-box"):
            yield Static("[b]Verified worktree review[/b]" if self._confirm else "[b]Changes[/b]")
            if self._binding:
                yield Static(f"Snapshot: {self._binding[:16]}")
            if self._truncated:
                yield Static("Preview is limited; inspect full files in the isolated worktree.")
            yield Select(
                [(sanitize(f.path, 300), f.path) for f in self._files],
                value=self._selected,
                allow_blank=False,
                id="review-file",
            )
            yield Select([("Whole file", -1)], value=-1, allow_blank=False, id="review-hunk")
            with VerticalScroll(id="review-content"):
                yield Static("", id="review-diff")
            with Vertical(id="review-actions"):
                if self._confirm:
                    yield Button("Apply all verified changes", variant="success", id="review-apply")
                yield Button(
                    "Keep isolated / close" if self._confirm else "Close", id="review-keep"
                )

    def on_mount(self) -> None:
        self._select_file()

    @on(Select.Changed, "#review-file")
    def _select_file(self) -> None:
        if not self.is_mounted:
            return
        path = self.query_one("#review-file", Select).value
        selected = next((file for file in self._files if file.path == path), self._files[0])
        select = self.query_one("#review-hunk", Select)
        select.set_options(
            [
                ("Whole file", -1),
                *[
                    (sanitize(hunk.splitlines()[0], 120), index)
                    for index, hunk in enumerate(selected.hunks)
                ],
            ]
        )
        select.value = -1
        self._show_hunk()

    @on(Select.Changed, "#review-hunk")
    def _show_hunk(self) -> None:
        path = self.query_one("#review-file", Select).value
        selected = next((file for file in self._files if file.path == path), self._files[0])
        hunk = self.query_one("#review-hunk", Select).value
        value = selected.hunks[hunk] if isinstance(hunk, int) and hunk >= 0 else selected.text
        self.query_one("#review-diff", Static).update(sanitize(value, 200000))

    @on(Button.Pressed, "#review-apply")
    def _apply(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#review-keep")
    def action_keep(self) -> None:
        self.dismiss(False)
