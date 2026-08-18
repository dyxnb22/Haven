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
    kind: Literal["run.created"] = "run.created"
    run_id: str
    workspace: str
    workspace_digest: str
    goal: str
    mode: str
    model_name: str
    git_branch: str = ""
    git_commit: str = ""
    max_steps: int = 0
    sandbox_backend: str = ""
    #: 如果这是会话后续轮次（Phase 2），则表示它所延续的运行。
    parent_run_id: str = ""


class StepStarted(StrictModel):
    kind: Literal["step.started"] = "step.started"
    run_id: str
    step: int


class AssistantDelta(StrictModel):
    """瞬态流式文本；只供 UI 使用，永不持久化。"""

    kind: Literal["assistant.delta"] = "assistant.delta"
    run_id: str
    step: int
    text: str


class AssistantReasoning(StrictModel):
    """瞬态推理模型思考内容；只供 UI 使用，永不持久化。"""

    kind: Literal["assistant.reasoning"] = "assistant.reasoning"
    run_id: str
    step: int
    text: str


class StreamRestarted(StrictModel):
    """正在重试本轮；丢弃此步骤已经显示的所有内容。"""

    kind: Literal["stream.restarted"] = "stream.restarted"
    run_id: str
    step: int


class ModelCompleted(StrictModel):
    kind: Literal["model.completed"] = "model.completed"
    run_id: str
    step: int
    text: str
    tool_call_count: int
    input_tokens: int
    output_tokens: int
    usage_estimated: bool
    ttft_ms: int
    duration_ms: int
    finish_reason: str
    reasoning_tokens: int = 0
    cached_input_tokens: int = 0


class ToolProposed(StrictModel):
    kind: Literal["tool.proposed"] = "tool.proposed"
    run_id: str
    step: int
    call_id: str
    tool_name: str
    args_summary: str


class PolicyDecided(StrictModel):
    kind: Literal["policy.decided"] = "policy.decided"
    run_id: str
    call_id: str
    decision: str
    reason_code: str
    risk: str


class ApprovalRequested(StrictModel):
    kind: Literal["approval.requested"] = "approval.requested"
    run_id: str
    call_id: str
    approval_id: str
    tool_name: str
    summary: str
    preview: str
    risk: str
    request_digest: str


class ApprovalDecided(StrictModel):
    kind: Literal["approval.decided"] = "approval.decided"
    run_id: str
    approval_id: str
    decision: str


class ExecutionStarted(StrictModel):
    kind: Literal["execution.started"] = "execution.started"
    run_id: str
    call_id: str
    tool_name: str
    ticket_digest: str
    #: 限制此次执行所用的操作系统机制；非进程工具为空。
    sandbox_backend: str = ""


class ToolCompleted(StrictModel):
    kind: Literal["tool.completed"] = "tool.completed"
    run_id: str
    call_id: str
    tool_name: str
    status: str
    error_code: str = ""
    summary: str = ""
    truncated: bool = False
    duration_ms: int = 0


class EvidenceRecorded(StrictModel):
    kind: Literal["evidence.recorded"] = "evidence.recorded"
    run_id: str
    evidence_kind: Literal["edit", "check", "diff"]
    summary: str


class DiffPreview(StrictModel):
    kind: Literal["diff.preview"] = "diff.preview"
    run_id: str
    files_changed: int
    insertions: int
    deletions: int
    preview: str


class ContextSegment(StrictModel):
    source: str
    trust: Literal["trusted", "untrusted"]
    size_bytes: int
    reason: str


class ContextBuilt(StrictModel):
    kind: Literal["context.built"] = "context.built"
    run_id: str
    step: int
    segments: tuple[ContextSegment, ...]
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

    kind: Literal["request.envelope"] = "request.envelope"
    run_id: str
    step: int
    reason: Literal["initial", "changed"]
    system_prompt_digest: str
    system_prompt_chars: int
    tool_names: tuple[str, ...]
    reasoning_effort: str = ""
    max_output_tokens: int = 0


class PlanStepView(StrictModel):
    title: str
    status: str


class PlanUpdated(StrictModel):
    kind: Literal["plan.updated"] = "plan.updated"
    run_id: str
    steps: tuple[PlanStepView, ...]


class Notice(StrictModel):
    kind: Literal["notice"] = "notice"
    run_id: str
    level: Literal["info", "warning", "error"]
    message: str


class EffectUnknown(StrictModel):
    kind: Literal["effect.unknown"] = "effect.unknown"
    run_id: str
    call_id: str
    tool_name: str
    detail: str


class SteerQueued(StrictModel):
    """运行处于活动状态时接受的用户输入，会在下一次轮次边界交付。
    该输入会写入日志，因此即使运行中断，尚未交付的指令也能在崩溃后保留；
    交付过程会体现为它最终转换成的用户消息。"""

    kind: Literal["steer.queued"] = "steer.queued"
    run_id: str
    text: str


class RunFinished(StrictModel):
    kind: Literal["run.finished"] = "run.finished"
    run_id: str
    status: str
    stop_reason: str
    gate_reason: str = ""
    steps: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cost_usd: float = 0.0
    #: 模型是否存在费率卡。False 表示 `cost_usd` 是占位值而非测量结果——读者
    #: 不能看到 `$0.0000` 就得出本次运行免费的结论。默认值为 true，以便在该
    #: 字段加入前写入的日志继续按原样渲染。
    cost_known: bool = True
    usage_estimated: bool = False
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
    """事件及其在日志中的位置。瞬态事件的 seq 为 0。"""

    seq: int
    at: str
    event: ApplicationEvent
