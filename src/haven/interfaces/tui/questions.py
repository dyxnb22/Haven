"""用户追问表单；验证规则由共享契约提供。"""

from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Select, Static

from haven.contracts.user_input import UserInputRequest, UserInputResponse
from haven.interfaces.tui.presenter import sanitize


class QuestionScreen(ModalScreen[UserInputResponse | None]):
    """展示选择题和自由回答，提交前保留用户修订机会。"""

    BINDINGS = [("escape", "cancel_question", "Cancel task")]
    DEFAULT_CSS = """
    QuestionScreen { align: center middle; }
    #question-box { width: 80%; max-height: 90%; border: thick $accent; padding: 1 2; }
    #question-fields { height: auto; max-height: 30; }
    #question-error { color: $error; height: auto; }
    """

    def __init__(self, request: UserInputRequest) -> None:
        super().__init__()
        self._request = request

    def compose(self) -> ComposeResult:
        with Vertical(id="question-box"):
            yield Static("[b]Your input is needed[/b]")
            with VerticalScroll(id="question-fields"):
                for index, question in enumerate(self._request.questions):
                    label = question.question + (" *" if question.required else " (optional)")
                    yield Static(sanitize(label, 600))
                    if question.options:
                        yield Select(
                            [(sanitize(option, 120), option) for option in question.options],
                            prompt="Choose an option",
                            id=f"choice-{index}",
                        )
                    if question.allow_custom:
                        yield Input(
                            placeholder="Or enter your own answer"
                            if question.options
                            else "Answer",
                            id=f"answer-{index}",
                        )
            yield Static("", id="question-error")
            yield Button("Continue", id="question-submit", variant="success")
            yield Button("Keep paused (reopen with /answer)", id="question-keep")
            yield Button("Cancel task (Esc)", id="question-cancel", variant="error")

    @on(Button.Pressed, "#question-submit")
    @on(Input.Submitted)
    def _submit(self) -> None:
        answers: dict[str, str] = {}
        for index, question in enumerate(self._request.questions):
            value = ""
            if question.options:
                choice = self.query_one(f"#choice-{index}", Select).value
                if isinstance(choice, str):
                    value = choice
            if question.allow_custom:
                custom = self.query_one(f"#answer-{index}", Input).value.strip()
                if custom:
                    value = custom
            if value:
                answers[question.id] = value
        try:
            response = UserInputResponse(request_id=self._request.request_id, answers=answers)
            self._request.validate_response(response)
        except ValueError as exc:
            self.query_one("#question-error", Static).update(sanitize(str(exc), 500))
            return
        self.dismiss(response)

    @on(Button.Pressed, "#question-cancel")
    def action_cancel_question(self) -> None:
        self.dismiss(UserInputResponse(request_id=self._request.request_id, action="cancel"))

    @on(Button.Pressed, "#question-keep")
    def keep_paused(self) -> None:
        self.dismiss(None)
