"""一次实际模型调用的选择、预算和计价来源，不含凭据。"""

from typing import Literal

from pydantic import Field, field_validator, model_validator

from haven.contracts.base import StrictModel

ModelRouteReason = Literal["single", "exploration", "always_deep", "edits", "fast_turns", "plan"]


class ModelSpec(StrictModel):
    """当前 Provider 上的单模型或双轨路由。model 为单模型或深度模型。"""

    model: str = Field(min_length=1, max_length=200)
    fast_model: str = Field(default="", max_length=200)
    fast_turns: int | None = None

    @field_validator("model", "fast_model")
    @classmethod
    def validate_model_name(cls, name: str) -> str:
        if name != name.strip() or any(ord(char) < 32 for char in name):
            raise ValueError(
                "model names cannot contain surrounding whitespace or control characters"
            )
        return name

    @model_validator(mode="after")
    def validate_route(self) -> "ModelSpec":
        if not self.fast_model and self.fast_turns is not None:
            raise ValueError("fast_turns requires a fast model")
        return self

    @property
    def model_names(self) -> tuple[str, ...]:
        return (self.fast_model, self.model) if self.fast_model else (self.model,)


class ModelSelection(StrictModel):
    """一次运行的配置快照，包含路由规则及非秘密 Provider 设置的摘要。"""

    spec: ModelSpec
    preset_name: str = Field(default="", max_length=48)
    provider_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ModelCallInfo(StrictModel):
    model_name: str = Field(max_length=500)
    selection_reason: ModelRouteReason
    profile_name: str = Field(max_length=500)
    profile_source: Literal["builtin", "fallback"]
    context_chars: int = Field(gt=0)
    context_window_tokens: int = Field(ge=0)
    output_limit_tokens: int = Field(default=0, ge=0)
    reasoning_supported: bool | None = None
    reasoning_effort: str | None = None
    reasoning_replay: bool = False
    prefix_continuation: bool = False
    usage_source: Literal["provider", "estimated"]
    cost_usd: float = Field(ge=0, allow_inf_nan=False)
    cost_known: bool
    pricing_source: str = Field(max_length=600)
