"""主任务输入框的有限历史导航；浏览历史时保留尚未提交的草稿。"""

from textual.binding import Binding
from textual.widgets import Input

from haven.ports.session import INPUT_HISTORY_LIMIT


class PromptInput(Input):
    """↑/↓ 仅在这个输入框获得焦点时生效，不接管其他表单的按键。"""

    BINDINGS = [
        Binding("up", "previous_input", "Previous task", show=False),
        Binding("down", "next_input", "Next task / draft", show=False),
    ]

    def __init__(self) -> None:
        super().__init__(placeholder="Describe a coding task… (/help for commands)", id="prompt")
        self._history: list[str] = []
        self._position = 0
        self._draft = ""

    def load_history(self, entries: list[str]) -> None:
        """恢复已提交任务；启动期间正在输入的文字保持原样。"""
        self._history = entries[-INPUT_HISTORY_LIMIT:]
        self.reset_navigation()

    def remember(self, text: str) -> None:
        """记住原始输入，保持与持久化历史相同的顺序和容量。"""
        self._history = [*self._history, text][-INPUT_HISTORY_LIMIT:]
        self.reset_navigation()

    def reset_navigation(self) -> None:
        """提交或执行界面命令后，下一次 ↑ 从最新任务开始。"""
        self._position = len(self._history)
        self._draft = ""

    @property
    def entries(self) -> tuple[str, ...]:
        """返回输入历史的只读快照，最旧的在前。"""
        return tuple(self._history)

    @property
    def navigation_draft(self) -> str:
        """返回进入历史前的草稿，供历史管理命令恢复。"""
        return self._draft if self._position < len(self._history) else ""

    def clear_history(self) -> None:
        """清除导航条目；正在浏览时恢复草稿，否则保持输入文字不变。"""
        if self._position < len(self._history):
            self.value = self._draft
            self.cursor_position = len(self.value)
        self.load_history([])

    def action_previous_input(self) -> None:
        """进入历史时先保存草稿，抵达最旧任务后保持原位。"""
        if not self._history:
            return
        if self._position == len(self._history):
            self._draft = self.value
        if self._position > 0:
            self._position -= 1
            self.value = self._history[self._position]
            self.cursor_position = len(self.value)

    def action_next_input(self) -> None:
        """向新任务浏览；越过最新任务时恢复进入历史前的草稿。"""
        if self._position < len(self._history):
            self._position += 1
            self.value = (
                self._draft
                if self._position == len(self._history)
                else self._history[self._position]
            )
            self.cursor_position = len(self.value)
