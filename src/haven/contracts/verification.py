"""用户选定的检查范围和程序记录的结果；不由模型参数修改。"""

from typing import Self

from pydantic import Field, model_validator

from haven.contracts.base import StrictModel
from haven.domain.evidence import CheckEvidence, RequiredCheck


class RecipeRequirement(StrictModel):
    recipe_id: str = Field(min_length=1, max_length=100)
    recipe_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    def to_domain(self) -> RequiredCheck:
        return RequiredCheck(self.recipe_id, self.recipe_digest)


class VerificationSelection(StrictModel):
    group_name: str = Field(min_length=1, max_length=100)
    required: tuple[RecipeRequirement, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def unique_recipes(self) -> Self:
        if len({item.recipe_id for item in self.required}) != len(self.required):
            raise ValueError("required recipe IDs must be unique")
        return self

    def to_domain(self) -> tuple[RequiredCheck, ...]:
        return tuple(item.to_domain() for item in self.required)


class CheckResultData(StrictModel):
    recipe_id: str
    recipe_digest: str = ""
    exit_code: int
    duration_ms: int
    truncated: bool

    def to_domain(self, seq: int) -> CheckEvidence:
        return CheckEvidence(
            seq=seq,
            recipe_id=self.recipe_id,
            recipe_digest=self.recipe_digest,
            exit_code=self.exit_code,
            duration_ms=self.duration_ms,
            truncated=self.truncated,
        )
