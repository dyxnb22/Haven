"""类型化的应用事件：唯一的追踪流。

每次有意义的状态转换都会发出一个事件。TUI、无头 CLI、SQLite 日志、重放和导出
都消费同一条流，因此界面永远不会偏离经过审计的记录。

事件会封装在 `EventEnvelope` 中，其中包含持久化时分配的序列号。瞬态事件
（流式文本增量）会到达 UI，但不会持久化。
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, TypeAdapter

from haven.contracts.base import StrictModel

SCHEMA_VERSION = 1


class RunCreated(StrictModel):
    """运行创建时记录的不可变元数据。"""

    #: 写入追踪流的事件判别字段。
    kind: Literal["run.created"] = "run.created"
    #: 运行及其所有关联事件共用的稳定标识。
    run_id: str
    #: 本次运行对应的规范化工作区根路径。
    workspace: str
    #: 标识创建运行时工作区状态的摘要。
    workspace_digest: str
    #: 首次模型请求前记录的用户目标。
    goal: str
    #: 本次运行使用的权限模式，以稳定字符串值序列化。
    mode: str
    #: 最终配置中选择的模型标识符。
    model_name: str
    #: 创建运行时记录的 Git 分支；不可用时为空。
    git_branch: str = ""
    #: 创建运行时记录的短提交标识；Git 不可用时为空。
    git_commit: str = ""
    #: 复制到追踪流、供 UI 和重放展示的硬轮次上限。
    max_steps: int = 0
    #: 选中的操作系统沙箱后端；为空表示没有可用后端。
    sandbox_backend: str = ""
    #: 如果这是会话后续轮次（Phase 2），则表示它所延续的运行。
    parent_run_id: str = ""


class StepStarted(StrictModel):
    """开始一次模型轮次时发出的持久化事件。"""

    #: 写入追踪流的事件判别字段。
    kind: Literal["step.started"] = "step.started"
    #: 拥有此模型循环轮次的运行。
    run_id: str
    #: 从 1 开始计数的模型循环轮次。
    step: int


class AssistantDelta(StrictModel):
    """瞬态流式文本；只供 UI 使用，永不持久化。"""

    #: 临时可见文本事件的判别字段。
    kind: Literal["assistant.delta"] = "assistant.delta"
    #: 拥有此流式片段的运行。
    run_id: str
    #: 产生此片段的模型循环轮次。
    step: int
    #: 文本片段；片段按设计不会持久化。
    text: str


class AssistantReasoning(StrictModel):
    """瞬态推理模型思考内容；只供 UI 使用，永不持久化。"""

    #: 临时推理文本事件的判别字段。
    kind: Literal["assistant.reasoning"] = "assistant.reasoning"
    #: 拥有此流式片段的运行。
    run_id: str
    #: 产生此片段的模型循环轮次。
    step: int
    #: 提供商推理片段；仅用于展示，永远不会作为模型输入。
    text: str


class StreamRestarted(StrictModel):
    """正在重试本轮；丢弃此步骤已经显示的所有内容。"""

    #: 当前流重试事件的判别字段。
    kind: Literal["stream.restarted"] = "stream.restarted"
    #: 正在重试当前模型流的运行。
    run_id: str
    #: 临时输出已被丢弃的模型循环轮次。
    step: int


class ModelCompleted(StrictModel):
    """模型完成本轮响应后记录的文本、计量数据和停止原因。"""

    #: 模型轮次持久化结束事件的判别字段。
    kind: Literal["model.completed"] = "model.completed"
    #: 拥有已完成响应的运行。
    run_id: str
    #: 产生该响应的模型循环轮次。
    step: int
    #: 从流中拼接出的可见助手文本。
    text: str
    #: 此响应中的工具调用提议数量。
    tool_call_count: int
    #: 此响应的提示输入 token，包含缓存 token。
    input_tokens: int
    #: 生成 token，包含隐藏推理 token。
    output_tokens: int
    #: 提供商未返回用量、因此用量是估算值时为 True。
    usage_estimated: bool
    #: 首个 token 的到达时间，单位为毫秒。
    ttft_ms: int
    #: 流式响应总耗时，单位为毫秒。
    duration_ms: int
    #: 提供商停止分类。
    finish_reason: str
    #: output_tokens 中归因于提供商推理的部分。
    reasoning_tokens: int = 0
    #: input_tokens 中由提供商提示缓存提供的部分。
    cached_input_tokens: int = 0


class ToolProposed(StrictModel):
    """模型提出工具调用，尚未经过策略决策或实际执行。"""

    #: 未校验工具提议事件的判别字段。
    kind: Literal["tool.proposed"] = "tool.proposed"
    #: 拥有该提议的运行。
    run_id: str
    #: 发出该提议的模型循环轮次。
    step: int
    #: 提供商或 Haven 的标识，用于配对后续所有记录。
    call_id: str
    #: 执行前使用的 Haven 注册表名称。
    tool_name: str
    #: 用于追踪流的有界人类可读参数摘要。
    args_summary: str


class PolicyDecided(StrictModel):
    """策略层对工具调用作出的允许、拒绝或需审批等决定。"""

    #: 确定性策略决定事件的判别字段。
    kind: Literal["policy.decided"] = "policy.decided"
    #: 拥有该决定的运行。
    run_id: str
    #: 正在评估的工具调用标识。
    call_id: str
    #: 稳定的策略结果字符串：allow、ask 或 deny。
    decision: str
    #: CLI/TUI 和评估使用的机器可读原因。
    reason_code: str
    #: allow/ask/deny 决定附带的风险等级。
    risk: str


class ApprovalRequested(StrictModel):
    """需要用户确认的工具调用请求及其固定的预览信息。"""

    #: 等待用户输入的请求事件判别字段。
    kind: Literal["approval.requested"] = "approval.requested"
    #: 拥有审批请求的运行。
    run_id: str
    #: 等待审批的工具调用标识。
    call_id: str
    #: 这条一次性审批记录的持久标识。
    approval_id: str
    #: 呈现给用户审批的工具形态。
    tool_name: str
    #: 人工编写或生成的意图摘要。
    summary: str
    #: 绑定到 request_digest 的有界人类可读预览。
    preview: str
    #: 用户决定前展示的风险等级。
    risk: str
    #: 用户正在审批的精确操作和预览的摘要。
    request_digest: str


class ApprovalDecided(StrictModel):
    """用户对待审批工具调用作出的最终决定。"""

    #: 最终审批决定事件的判别字段。
    kind: Literal["approval.decided"] = "approval.decided"
    #: 拥有审批记录的运行。
    run_id: str
    #: 正在消费或拒绝的一次性审批记录。
    approval_id: str
    #: 用户或自动流程对审批请求作出的最终决定。
    decision: str


class ExecutionStarted(StrictModel):
    """工具调用已通过策略和审批，开始实际执行。"""

    #: 策略和审批通过后开始执行事件的判别字段。
    kind: Literal["execution.started"] = "execution.started"
    #: 拥有该执行的运行。
    run_id: str
    #: 正在执行的工具调用标识。
    call_id: str
    #: 执行器接受的工具形态。
    tool_name: str
    #: 流水线消费的一次性执行票据摘要。
    ticket_digest: str
    #: 限制此次执行所用的操作系统机制；非进程工具为空。
    sandbox_backend: str = ""


class ToolCompleted(StrictModel):
    """工具调用结束后的状态、摘要、错误和耗时。"""

    #: 工具调用完成事件的判别字段。
    kind: Literal["tool.completed"] = "tool.completed"
    #: 拥有该结果的运行。
    run_id: str
    #: 与提议配对的工具调用标识。
    call_id: str
    #: 产生该结果的工具形态。
    tool_name: str
    #: 稳定的 ToolStatus 值，通常为 ok 或 error。
    status: str
    #: status 为 error 时使用的稳定 ToolErrorCode 值。
    error_code: str = ""
    #: 有界的人类可读摘要，而不是完整结果载荷。
    summary: str = ""
    #: 面向模型的结果或输出被截断时为 True。
    truncated: bool = False
    #: 工具墙上时钟耗时，单位为毫秒。
    duration_ms: int = 0


class EvidenceRecorded(StrictModel):
    """记录 Evidence Gate 可验证的一项编辑、检查或差异证据。"""

    #: 新增 EvidenceLedger 条目的事件判别字段。
    kind: Literal["evidence.recorded"] = "evidence.recorded"
    #: 证据账本发生变化的运行。
    run_id: str
    #: 此事件追加的 EvidenceLedger 类别。
    evidence_kind: Literal["edit", "check", "diff"]
    #: 证据条目的有界说明。
    summary: str


class DiffPreview(StrictModel):
    """向 UI 或审计记录提供当前变更的有界差异预览。"""

    #: 有界差异展示事件的判别字段。
    kind: Literal["diff.preview"] = "diff.preview"
    #: 正在展示当前差异的运行。
    run_id: str
    #: 存在净变化的文件数量。
    files_changed: int
    #: 完整差异中的新增行数。
    insertions: int
    #: 完整差异中的删除行数。
    deletions: int
    #: 有界统一差异预览；不是完整差异构件。
    preview: str


class ContextSegment(StrictModel):
    """模型上下文中的一段来源信息及其可信度和大小。"""

    #: ContextBuilder 使用的逻辑来源标签，例如 transcript 或 guidance。
    source: str
    #: 来源是可信应用数据，还是不可信仓库文本。
    trust: Literal["trusted", "untrusted"]
    #: 该片段占用上下文预算的 UTF-8 字节数。
    size_bytes: int
    #: 该片段被纳入、压缩或省略的原因。
    reason: str


class ContextBuilt(StrictModel):
    """完成一轮模型上下文组装后记录所选片段及总大小。"""

    #: 上下文组装完成事件的判别字段。
    kind: Literal["context.built"] = "context.built"
    #: 接收此上下文的运行。
    run_id: str
    #: 组装此上下文的模型循环轮次。
    step: int
    #: 按渲染顺序排列的来源选择和省略决定。
    segments: tuple[ContextSegment, ...]
    #: 选中片段大小总和，单位为 UTF-8 字节。
    total_bytes: int


class RequestEnvelope(StrictModel):
    """请求中除消息之外、模型可见的全部内容。

    `context.built` 记录模型被要求做什么；这里记录模型被告知了什么——系统
    规则、提供的工具以及采样参数。没有它，重放运行只能重建对话，却无法重建
    影响对话的指令；两次运行之间即使提示词发生变化，也会完全没有痕迹。

    第一步会记录此事件，之后只有内容发生变化时才记录（`reason`），因此稳定
    的前缀——ADR 0008 试图保留的部分——每次运行只需一个事件，而不是每一步
    一个事件。内容以摘要和大小表示：日志只保存标识和有界摘要，绝不保存原始
    载荷。
    """

    #: 模型可见非消息输入事件的判别字段。
    kind: Literal["request.envelope"] = "request.envelope"
    #: 接收此请求的运行。
    run_id: str
    #: 发送此信封的模型循环轮次。
    step: int
    #: 这是本次运行的首个信封，还是发生变化后的信封。
    reason: Literal["initial", "changed"]
    #: 系统指令、工具名称和采样参数的摘要。
    system_prompt_digest: str
    #: 系统提示词的字符数，不是 token 数。
    system_prompt_chars: int
    #: 请求模式中包含的工具名称，按提供商顺序排列。
    tool_names: tuple[str, ...]
    #: 提供商推理设置；未显式配置时为空。
    reasoning_effort: str = ""
    #: 请求的提供商输出上限；零表示未发送显式上限。
    max_output_tokens: int = 0


class PlanStepView(StrictModel):
    """发送给 UI 的计划步骤展示快照。"""

    #: 展示给用户的简短计划标题。
    title: str
    #: 为 UI 渲染的 PlanStep 状态。
    status: str


class PlanUpdated(StrictModel):
    """当前代理计划发生变化后的展示快照。"""

    #: 结构化计划变更事件的判别字段。
    kind: Literal["plan.updated"] = "plan.updated"
    #: 计划发生变化的运行。
    run_id: str
    #: 完整的有序计划快照。
    steps: tuple[PlanStepView, ...]


class Notice(StrictModel):
    """面向用户的运行时提示，不代表业务状态转换。"""

    #: 面向用户诊断事件的判别字段。
    kind: Literal["notice"] = "notice"
    #: 展示该提示的运行。
    run_id: str
    #: 用于渲染的 UI 严重级别。
    level: Literal["info", "warning", "error"]
    #: 面向用户的诊断文本；它本身不是状态转换。
    message: str


class EffectUnknown(StrictModel):
    """工具执行结果不确定，表示不能安全判断外部副作用是否已发生。"""

    #: 未分类副作用事件的判别字段。
    kind: Literal["effect.unknown"] = "effect.unknown"
    #: 未完成调和、无法安全恢复的运行。
    run_id: str
    #: 效果不确定的工具调用标识。
    call_id: str
    #: 可能产生该效果的工具形态。
    tool_name: str
    #: 无法安全分类该副作用的原因说明。
    detail: str


class SteerQueued(StrictModel):
    """运行处于活动状态时接受的用户输入，会在下一次轮次边界交付。
    该输入会写入日志，因此即使运行中断，尚未交付的指令也能在崩溃后保留；
    交付过程会体现为它最终转换成的用户消息。"""

    #: 在轮次边界排队输入事件的判别字段。
    kind: Literal["steer.queued"] = "steer.queued"
    #: 将接收该输入的运行。
    run_id: str
    #: 等待下一个模型轮次边界交付的用户输入。
    text: str


class RunFinished(StrictModel):
    """运行结束时记录的最终状态、停止原因和累计用量。"""

    #: 终态运行记录事件的判别字段。
    kind: Literal["run.finished"] = "run.finished"
    #: 生命周期已结束的运行。
    run_id: str
    #: 最终 RunStatus 值。
    status: str
    #: 最终 StopReason 值；只记录一个原因。
    stop_reason: str
    #: Evidence Gate 原因；运行未进入验证阶段时为空。
    gate_reason: str = ""
    #: 已完成的模型循环轮次总数。
    steps: int = 0
    #: 已执行的工具调用总数。
    tool_calls: int = 0
    #: 输入 token 总数，包含缓存输入。
    input_tokens: int = 0
    #: 生成的输出 token 总数。
    output_tokens: int = 0
    #: 由提供商缓存提供的输入 token 数。
    cached_input_tokens: int = 0
    #: 估算美元费用；展示账单含义前应先查看 cost_known。
    cost_usd: float = 0.0
    #: 模型是否存在费率卡。False 表示 `cost_usd` 是占位值而非测量结果——读者
    #: 不能看到 `$0.0000` 就得出本次运行免费的结论。默认值为 true，以便在该
    #: 字段加入前写入的日志继续按原样渲染。
    cost_known: bool = True
    #: 任意用量数字来自估算而非提供商报告时为 True。
    usage_estimated: bool = False
    #: 运行墙上时钟总耗时，单位为毫秒。
    duration_ms: int = 0


# 唯一的 trace 流，按大致生命周期顺序排列。UI 展示、`replay` 重新渲染以及
# 评估套件断言的所有内容都来自这里。
ApplicationEvent = Annotated[
    # 运行/轮次生命周期
    RunCreated
    | StepStarted
    # 模型流式输出（临时增量 + 持久化的完成事件）
    | AssistantDelta
    | AssistantReasoning
    | StreamRestarted
    | ModelCompleted
    # 执行通道，按流水线顺序排列（见 tool_pipeline.py）
    | ToolProposed
    | PolicyDecided
    | ApprovalRequested
    | ApprovalDecided
    | ExecutionStarted
    | ToolCompleted
    # 成功状态记录：Evidence Gate 将检查的内容
    | EvidenceRecorded
    | DiffPreview
    # 上下文组装 + 代理计划（每轮从 State 渲染）
    | RequestEnvelope
    | ContextBuilt
    | PlanUpdated
    # 会话运行时信息与诊断
    | Notice
    | SteerQueued
    | EffectUnknown
    | RunFinished,
    Field(discriminator="kind"),
]

EVENT_ADAPTER: TypeAdapter[ApplicationEvent] = TypeAdapter(ApplicationEvent)

#: 会流式发送到 UI、但永不持久化的事件类型。
TRANSIENT_KINDS = frozenset({"assistant.delta", "assistant.reasoning", "stream.restarted"})


class EventEnvelope(StrictModel):
    """事件及其在日志中的位置；瞬态事件的 `seq` 固定为 0，不写入持久化日志。"""

    #: 持久化事件序号；临时事件使用零且不会保存。
    #: 持久化序号单调递增；零表示临时事件。
    seq: int
    #: 事件创建时间，采用 ISO-8601 格式。
    at: str
    #: 类型化的应用事件载荷。
    event: ApplicationEvent
