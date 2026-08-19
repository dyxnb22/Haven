"""与提供商无关的模型契约。

适配器将提供商的线协议格式转换为这些类型；任何提供商特有的内容都不能越过
此边界泄漏到核心层。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from haven.contracts.base import StrictModel


class Usage(StrictModel):
    """模型一次响应的 token 用量；部分提供商可能只返回估算值。"""

    #: 提供商报告的提示输入 token 总数，包含缓存输入 token。
    input_tokens: int = Field(default=0, ge=0)
    #: 生成 token 总数，包含可能存在的隐藏推理 token。
    output_tokens: int = Field(default=0, ge=0)
    #: 提供商报告时，表示 output_tokens 中用于隐藏推理的部分。它已经包含在
    #: output_tokens 内，但单独跟踪后，成本报告可以说明这些 token 的去向。
    reasoning_tokens: int = Field(default=0, ge=0)
    #: input_tokens 中由提供商提示缓存提供的部分。它已经包含在 input_tokens
    #: 内；较高的比例说明稳定前缀排序发挥了作用（ADR 0008）。
    cached_input_tokens: int = Field(default=0, ge=0)
    #: 提供商未返回用量、Haven 根据字符数估算时为 True。
    estimated: bool = False

    @model_validator(mode="after")
    def _parts_fit_totals(self) -> Usage:
        if self.cached_input_tokens > self.input_tokens:
            raise ValueError("cached_input_tokens cannot exceed input_tokens")
        if self.reasoning_tokens > self.output_tokens:
            raise ValueError("reasoning_tokens cannot exceed output_tokens")
        return self


class ToolCallProposal(StrictModel):
    """模型提出的完整工具调用。参数在流水线根据工具模式完成校验前，保持为原始
    JSON 文本。"""

    #: 提供商分配或 Haven 生成的标识，用于将结果与调用配对。
    call_id: str
    #: Haven 注册表名称，可处于提供商线协议名称转换前或转换后。
    tool_name: str
    #: 原始 JSON 对象文本；模式校验在应用流水线中完成。
    arguments_json: str


class ToolSchema(StrictModel):
    """提供给模型的工具名称和 JSON Schema。"""

    #: Haven 内部注册表中的稳定工具名称。
    name: str
    #: 面向模型的工具用途和安全边界说明。
    description: str
    #: 发送给提供商的工具参数 JSON Schema。
    parameters: dict[str, Any]


class ModelMessage(StrictModel):
    """提供商无关的对话消息；工具调用和工具结果也用消息表示。"""

    #: 消息角色；tool 消息必须通过 tool_call_id 关联到助手提议。
    role: Literal["system", "user", "assistant", "tool"]
    #: 可见文本内容；对 assistant/tool 调用消息可为空。
    content: str
    #: assistant 消息携带的提议；其他角色通常为空。
    tool_calls: tuple[ToolCallProposal, ...] = ()
    #: role=tool 消息所回答的 assistant 调用 ID。
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
    """发送给模型的一次完整请求，包括消息、工具和采样参数。"""

    #: 按提供商无关格式排列的完整对话前缀。
    messages: tuple[ModelMessage, ...]
    #: 本轮允许模型调用的工具及其参数 schema。
    tools: tuple[ToolSchema, ...] = ()
    #: 提供商输出上限；None 表示使用提供商默认值。
    max_output_tokens: int | None = None
    #: 透传给提供商的采样温度。
    temperature: float = 0.0
    #: 提供商的推理预算（例如 “low”/“medium”/“high”）。仅在设置后发送，因此
    #: 保持为 None 表示“使用提供商默认值”，而不是 Haven 选择的某个值。该字段
    #: 从模型 profile 传递而来。
    reasoning_effort: str | None = None


# --- 流式事件 -----------------------------------------------------------------


class TextDelta(StrictModel):
    """模型流中的一段文本增量。"""

    #: 标识可见文本片段的判别字段。
    kind: Literal["text_delta"] = "text_delta"
    #: 本次流事件新增的可见答案文本。
    text: str


class ReasoningDelta(StrictModel):
    """推理模型产生的隐藏思维链。

    它会展示给 UI，避免长时间思考看起来像卡死，但绝不会加入
    `ModelResult.text`，也不会回显到对话记录中：它不是答案，而且大多数提供商
    会拒绝把自身的推理内容作为输入再次接收。
    """

    #: 标识提供商推理片段的判别字段。
    kind: Literal["reasoning_delta"] = "reasoning_delta"
    #: 仅供 UI 展示的推理片段。
    text: str


class ToolCallReady(StrictModel):
    """模型流中已收集完整、可执行的工具调用。"""

    #: 标识完整工具调用事件的判别字段。
    kind: Literal["tool_call"] = "tool_call"
    #: 已从流中组装并可交给工具注册表校验的调用。
    call: ToolCallProposal


class UsageReport(StrictModel):
    """模型流中报告的用量信息。"""

    #: 标识用量事件的判别字段。
    kind: Literal["usage"] = "usage"
    #: 提供商报告的本轮 token 用量。
    usage: Usage


class StreamFinished(StrictModel):
    """模型流结束时携带的停止原因。"""

    #: 标识提供商流结束的判别字段。
    kind: Literal["finished"] = "finished"
    #: 提供商结束本轮流的分类。
    finish_reason: Literal["stop", "tool_calls", "length", "error"] = "stop"


ModelEvent = Annotated[
    TextDelta | ReasoningDelta | ToolCallReady | UsageReport | StreamFinished,
    Field(discriminator="kind"),
]


class ModelResult(StrictModel):
    """一次模型流式轮次组装完成的结果。"""

    #: 本轮模型生成的可见文本；推理内容不放在这里。
    text: str
    #: 本轮完整收集的工具调用提议。
    tool_calls: tuple[ToolCallProposal, ...] = ()
    #: 提供商报告或 Haven 估算的本轮用量。
    usage: Usage = Usage()
    #: 提供商停止分类：普通答案、工具调用、长度上限或错误。
    finish_reason: Literal["stop", "tool_calls", "length", "error"] = "stop"
    #: 首个流式 token 的到达时间，单位为毫秒。
    ttft_ms: int = 0
    #: 提供商流式响应总耗时，单位为毫秒。
    duration_ms: int = 0
    #: 本轮流式传出的提供商推理，为线协议重放而携带（ADR 0014）。
    provider_reasoning: str = ""
