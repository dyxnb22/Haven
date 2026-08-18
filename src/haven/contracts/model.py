"""与提供商无关的模型契约。

适配器将提供商的线协议格式转换为这些类型；任何提供商特有的内容都不能越过
此边界泄漏到核心层。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from haven.contracts.base import StrictModel


class Usage(StrictModel):
    input_tokens: int = 0
    output_tokens: int = 0
    #: 提供商报告时，表示 output_tokens 中用于隐藏推理的部分。它已经包含在
    #: output_tokens 内，但单独跟踪后，成本报告可以说明这些 token 的去向。
    reasoning_tokens: int = 0
    #: input_tokens 中由提供商提示缓存提供的部分。它已经包含在 input_tokens
    #: 内；较高的比例说明稳定前缀排序发挥了作用（ADR 0008）。
    cached_input_tokens: int = 0
    estimated: bool = False


class ToolCallProposal(StrictModel):
    """模型提出的完整工具调用。参数在流水线根据工具模式完成校验前，保持为原始
    JSON 文本。"""

    call_id: str
    tool_name: str
    arguments_json: str


class ToolSchema(StrictModel):
    name: str
    description: str
    parameters: dict[str, Any]


class ModelMessage(StrictModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    tool_calls: tuple[ToolCallProposal, ...] = ()
    tool_call_id: str | None = None
    #: 不透明的提供商推理，仅为线协议重放而携带（某些提供商要求在工具调用轮
    #: 中原样传回；ADR 0014）。它永远不是答案：不渲染、不作为证据、不进入压缩
    #: 摘要，也不可信。`content` 仍是唯一同时具备这些属性的字段。
    provider_reasoning: str = ""
    #: 模型应当“原地继续”而不是回复的末尾 assistant 消息——用于继续生成因
    #: 长度限制而截断的答案（ADR 0022）。仅对最后一条消息有意义；当 profile
    #: 声明支持时，适配器会连同提供商的 prefix 标志一起发送。
    is_prefix: bool = False


class ModelRequest(StrictModel):
    messages: tuple[ModelMessage, ...]
    tools: tuple[ToolSchema, ...] = ()
    max_output_tokens: int | None = None
    temperature: float = 0.0
    #: 提供商的推理预算（例如 “low”/“medium”/“high”）。仅在设置后发送，因此
    #: 保持为 None 表示“使用提供商默认值”，而不是 Haven 选择的某个值。该字段
    #: 从模型 profile 传递而来。
    reasoning_effort: str | None = None


# --- 流式事件 -----------------------------------------------------------------


class TextDelta(StrictModel):
    kind: Literal["text_delta"] = "text_delta"
    text: str


class ReasoningDelta(StrictModel):
    """推理模型产生的隐藏思维链。

    它会展示给 UI，避免长时间思考看起来像卡死，但绝不会加入
    `ModelResult.text`，也不会回显到对话记录中：它不是答案，而且大多数提供商
    会拒绝把自身的推理内容作为输入再次接收。
    """

    kind: Literal["reasoning_delta"] = "reasoning_delta"
    text: str


class ToolCallReady(StrictModel):
    kind: Literal["tool_call"] = "tool_call"
    call: ToolCallProposal


class UsageReport(StrictModel):
    kind: Literal["usage"] = "usage"
    usage: Usage


class StreamFinished(StrictModel):
    kind: Literal["finished"] = "finished"
    finish_reason: Literal["stop", "tool_calls", "length", "error"] = "stop"


ModelEvent = Annotated[
    TextDelta | ReasoningDelta | ToolCallReady | UsageReport | StreamFinished,
    Field(discriminator="kind"),
]


class ModelResult(StrictModel):
    """一次模型流式轮次组装完成的结果。"""

    text: str
    tool_calls: tuple[ToolCallProposal, ...] = ()
    usage: Usage = Usage()
    finish_reason: Literal["stop", "tool_calls", "length", "error"] = "stop"
    ttft_ms: int = 0
    duration_ms: int = 0
    #: 本轮流式传出的提供商推理，为线协议重放而携带（ADR 0014）。
    provider_reasoning: str = ""
