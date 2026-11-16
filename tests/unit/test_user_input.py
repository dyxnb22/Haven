"""追问表单与无头入口共享的回答校验。"""

import pytest

from haven.contracts.user_input import UserInputRequest, UserInputResponse, UserQuestion


def test_optional_questions_custom_answers_and_option_normalization() -> None:
    request = UserInputRequest(
        request_id="q1",
        call_id="c1",
        questions=(
            UserQuestion(id="scope", question="Scope?", options=(" Parser ", "CLI")),
            UserQuestion(id="note", question="Anything else?", required=False),
        ),
    )
    assert request.questions[0].options == ("Parser", "CLI")
    response = UserInputResponse(request_id="q1", answers={"scope": " Custom scope "})
    request.validate_response(response)
    assert response.answers["scope"] == "Custom scope"


@pytest.mark.parametrize("options", [("A", "A"), ("A", " A "), (" ",), ("x" * 121,)])
def test_invalid_options_are_rejected(options: tuple[str, ...]) -> None:
    with pytest.raises(ValueError):
        UserQuestion(id="scope", question="Scope?", options=options)


def test_unanswerable_and_duplicate_questions_are_rejected() -> None:
    with pytest.raises(ValueError):
        UserQuestion(id="scope", question="Scope?", allow_custom=False)
    question = UserQuestion(id="scope", question="Scope?")
    with pytest.raises(ValueError):
        UserInputRequest(request_id="q1", call_id="c1", questions=(question, question))


def test_cancellation_cannot_smuggle_an_answer() -> None:
    request = UserInputRequest(
        request_id="q1", call_id="c1", questions=(UserQuestion(id="scope", question="Scope?"),)
    )
    with pytest.raises(ValueError, match="must not contain answers"):
        request.validate_response(
            UserInputResponse(request_id="q1", action="cancel", answers={"scope": "Parser"})
        )
