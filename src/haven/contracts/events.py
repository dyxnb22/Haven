"""Typed application events: the single trace stream.

Every meaningful state transition emits one event. The TUI, the headless CLI,
the SQLite journal, replay, and export all consume the same stream, so the
interface can never diverge from the audited record.

Events are wrapped in an `EventEnvelope` which carries the sequence number
assigned at persist time. Transient events (streaming text deltas) reach the
UI but are not persisted.
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
    #: The run this one continues, when it is a session follow-up (Phase 2).
    parent_run_id: str = ""


class StepStarted(StrictModel):
    kind: Literal["step.started"] = "step.started"
    run_id: str
    step: int


class AssistantDelta(StrictModel):
    """Transient streaming text; UI only, never persisted."""

    kind: Literal["assistant.delta"] = "assistant.delta"
    run_id: str
    step: int
    text: str


class AssistantReasoning(StrictModel):
    """Transient reasoning-model thinking; UI only, never persisted."""

    kind: Literal["assistant.reasoning"] = "assistant.reasoning"
    run_id: str
    step: int
    text: str


class StreamRestarted(StrictModel):
    """The turn is being retried; discard anything already shown for this step."""

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
    #: Which OS mechanism confined this execution; empty for non-process tools.
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
    """Everything model-visible in a request that is not the messages.

    `context.built` records what the model was *asked*; this records what it was
    *told* — the system rules, the tools offered, and the sampling parameters.
    Without it a replayed run reconstructs the conversation but not the
    instructions that shaped it, and a prompt change between two runs leaves no
    trace at all.

    Logged on the first step and thereafter only when it changes (`reason`), so
    a stable prefix — the thing ADR 0008 works to preserve — costs one event per
    run rather than one per step. Content is carried as a digest plus a size:
    the journal keeps identities and bounded summaries, never payloads.
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
    """User input accepted while a run is active, to be delivered at the next
    turn boundary. Journaled, so an interrupted run's undelivered steering
    survives a crash; delivery is visible as the user message it becomes."""

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
    #: Whether a rate card existed for the model. False means `cost_usd` is a
    #: placeholder, not a measurement — a reader must not see `$0.0000` and
    #: conclude the run was free. Defaults true so a journal written before this
    #: field keeps rendering as it did.
    cost_known: bool = True
    usage_estimated: bool = False
    duration_ms: int = 0


# The one trace stream, in rough lifecycle order. Everything the UI shows,
# `replay` re-renders, and the eval suite asserts on is one of these.
ApplicationEvent = Annotated[
    # run/turn lifecycle
    RunCreated
    | StepStarted
    # model streaming (transient deltas + the persisted completion)
    | AssistantDelta
    | AssistantReasoning
    | StreamRestarted
    | ModelCompleted
    # the execution channel, in pipeline order (see tool_pipeline.py)
    | ToolProposed
    | PolicyDecided
    | ApprovalRequested
    | ApprovalDecided
    | ExecutionStarted
    | ToolCompleted
    # success bookkeeping: what the Evidence Gate will look at
    | EvidenceRecorded
    | DiffPreview
    # context assembly + the agent's plan (rendered from State each turn)
    | RequestEnvelope
    | ContextBuilt
    | PlanUpdated
    # session runtime and diagnostics
    | Notice
    | SteerQueued
    | EffectUnknown
    | RunFinished,
    Field(discriminator="kind"),
]

EVENT_ADAPTER: TypeAdapter[ApplicationEvent] = TypeAdapter(ApplicationEvent)

#: Kinds that stream to the UI but are never persisted.
TRANSIENT_KINDS = frozenset({"assistant.delta", "assistant.reasoning", "stream.restarted"})


class EventEnvelope(StrictModel):
    """An event plus its journal position. seq is 0 for transient events."""

    seq: int
    at: str
    event: ApplicationEvent
