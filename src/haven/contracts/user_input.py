"""可持久化的用户追问及回答契约。"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator, model_validator

from haven.contracts.base import StrictModel


class UserQuestion(StrictModel):
    """一项选择题或自由文本问题。"""

    id: str = Field(min_length=1, max_length=40, pattern=r"^[a-zA-Z0-9_-]+$")
    question: str = Field(min_length=1, max_length=500)
    options: tuple[str, ...] = Field(default=(), max_length=6)
    required: bool = True
    allow_custom: bool = True

    @field_validator("options")
    @classmethod
    def _trim_options(cls, options: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(option.strip() for option in options)

    @model_validator(mode="after")
    def _valid_choices(self) -> UserQuestion:
        if not self.question.strip():
            raise ValueError("question must not be blank")
        if any(not option.strip() or len(option) > 120 for option in self.options):
            raise ValueError("options must contain 1 to 120 characters")
        if len({option.strip() for option in self.options}) != len(self.options):
            raise ValueError("options must be unique")
        if not self.options and not self.allow_custom:
            raise ValueError("a question without options must allow a custom answer")
        return self


class UserAskArgs(StrictModel):
    """提出一至三个需要用户回答的问题。"""

    questions: tuple[UserQuestion, ...] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def _unique_ids(self) -> UserAskArgs:
        if len({question.id for question in self.questions}) != len(self.questions):
            raise ValueError("question ids must be unique")
        return self


class UserInputResponse(StrictModel):
    """绑定到具体追问的用户回答；取消不会继续模型循环。"""

    request_id: str = Field(min_length=1, max_length=80)
    answers: dict[str, str] = Field(default_factory=dict, max_length=3)
    action: Literal["submit", "cancel"] = "submit"

    @field_validator("answers")
    @classmethod
    def _bound_answers(cls, answers: dict[str, str]) -> dict[str, str]:
        if any(len(value) > 2000 for value in answers.values()):
            raise ValueError("each answer must be at most 2000 characters")
        return {key: value.strip() for key, value in answers.items()}


class UserInputRequest(UserAskArgs):
    """检查点保存的待答请求；ID 防止旧回答被交给下一轮问题。"""

    request_id: str
    call_id: str

    def validate_response(self, response: UserInputResponse) -> None:
        """在写入任何回答之前验证关联、必填项和选项。"""
        if response.request_id != self.request_id:
            raise ValueError("request_id does not match the pending question")
        if response.action == "cancel":
            if response.answers:
                raise ValueError("cancelled responses must not contain answers")
            return
        known = {question.id for question in self.questions}
        if response.answers.keys() - known:
            raise ValueError("answers contain unknown question ids")
        for question in self.questions:
            answer = response.answers.get(question.id, "")
            if question.required and not answer:
                raise ValueError(f"answer required for {question.id}")
            if answer and not question.allow_custom and answer not in question.options:
                raise ValueError(f"answer for {question.id} must be one of the offered options")
