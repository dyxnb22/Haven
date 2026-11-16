"""CLI 使用的事件输出适配器。"""

import re
import sys
from pathlib import Path

import typer

from haven.contracts.events import (
    TRANSIENT_KINDS,
    ApprovalRequested,
    ChildStarted,
    DiffPreview,
    EventEnvelope,
    HandoffUpdated,
    ModelCompleted,
    Notice,
    PlanUpdated,
    PolicyDecided,
    RunCreated,
    RunFinished,
    StepStarted,
    ToolCompleted,
    ToolProposed,
    UserInputRequested,
)
from haven.interfaces.feedback import bell_enabled, event_hint, needs_attention
from haven.interfaces.model_display import cost_text, model_call_text
from haven.interfaces.process_output import process_result_lines

#: 控制字符会在任何内容到达终端前被移除。TUI 一直这样做
#:（`presenter.sanitize`）；无头输出以前会原样回显模型控制的文本，因此模型
#: 可以发出 ANSI 转义序列来改写操作员的终端或代码托管日志行。这里不需要
#: 转义 Rich 标记——typer.echo 写入普通字节，不会渲染标记。
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _plain(text: str) -> str:
    return _CONTROL_CHARS.sub("", text)


class ConsoleSink:
    """供无头运行和重放使用的紧凑、适合人类阅读的事件流。"""

    def __init__(self, verbose: bool = True, *, notify: bool = False) -> None:
        self._verbose = verbose
        self._notify = notify
        self._child_run_ids: set[str] = set()

    async def emit(self, envelope: EventEnvelope) -> None:
        """将可展示事件格式化为一行安全的终端文本；静默模式不输出。"""
        event = envelope.event
        if isinstance(event, ChildStarted):
            self._child_run_ids.add(event.child_run_id)
        line: str | None = None
        if isinstance(event, RunCreated):
            line = f"run {event.run_id} [{event.mode}] goal: {event.goal}"
            if event.agent_mode == "plan":
                line += " (plan mode)"
            if event.model_preset:
                line += f"\nmodel preset {event.model_preset} ({event.model_name})"
            if event.verification:
                line += f"\nrequired checks ({event.verification.group_name}): " + ", ".join(
                    item.recipe_id for item in event.verification.required
                )
        elif isinstance(event, UserInputRequested):
            line = "Your input is needed:\n" + "\n".join(
                f"  {q.id}: {q.question}" + (f" [{', '.join(q.options)}]" if q.options else "")
                for q in event.request.questions
            )
            line += (
                f"\nAnswer with: haven answer {event.run_id} "
                f'--request {event.request.request_id} --answers \'{{"question_id":"answer"}}\''
            )
        elif isinstance(event, PlanUpdated):
            line = f"plan ({event.plan_status or 'updated'}):\n" + "\n".join(
                f"  [{step.status}] {step.title}" for step in event.steps
            )
            if event.plan_status == "ready":
                line += f"\nReview the plan, then run: haven implement {event.run_id}"
        elif isinstance(event, HandoffUpdated):
            action = "revised" if event.notes is not None else "cleared"
            line = f"handoff {action} (event {envelope.seq}, {event.source}; untrusted notes)"
        elif isinstance(event, StepStarted):
            line = f"step {event.step}"
        elif isinstance(event, ToolProposed):
            line = f"  tool {event.tool_name} {event.args_summary}"
        elif isinstance(event, PolicyDecided) and event.decision != "allow":
            line = f"  policy {event.decision} ({event.reason_code})"
        elif isinstance(event, ToolCompleted):
            state = event.status if not event.error_code else f"error:{event.error_code}"
            line = f"  -> {state} ({event.duration_ms}ms) {event.summary}"
            if event.process:
                line += "\n" + "\n".join(process_result_lines(event))
        elif isinstance(event, ApprovalRequested):
            line = f"  approval needed: {event.summary}"
        elif isinstance(event, ModelCompleted):
            details = f"  model {model_call_text(event.model_info)}" if event.model_info else ""
            answer = f"assistant: {event.text}" if event.text else ""
            line = "\n".join(part for part in (details, answer) if part)
        elif isinstance(event, DiffPreview):
            line = f"  diff: {event.files_changed} file(s) +{event.insertions} -{event.deletions}"
        elif isinstance(event, Notice):
            line = f"  [{event.level}] {event.message}"
        elif isinstance(event, RunFinished):
            cached = f" cached={event.cached_input_tokens}" if event.cached_input_tokens else ""
            estimated = " (estimated)" if event.usage_estimated else ""
            line = (
                f"finished: {event.status} ({event.stop_reason}) "
                f"steps={event.steps} tools={event.tool_calls} "
                f"tokens={event.input_tokens}/{event.output_tokens}{cached}{estimated} "
                f"cost={cost_text(event.cost_usd, event.cost_known)}"
            )
        if line and self._verbose:
            typer.echo(_plain(line))
        if self._verbose:
            if hint := event_hint(event):
                typer.echo(f"Next: {hint}")
            if (
                self._notify
                and getattr(event, "run_id", "") not in self._child_run_ids
                and needs_attention(event)
                and bell_enabled()
                and sys.stdout.isatty()
                and sys.stderr.isatty()
            ):
                typer.echo("\a", nl=False, err=True)


class NullSink:
    """丢弃事件的 sink，用于不需要展示轨迹的调用方。"""

    async def emit(self, envelope: EventEnvelope) -> None:
        """丢弃事件，不产生任何输出。"""
        return None


class JsonlEventSink:
    """实时将每个已持久化事件以一行 JSON 写入文件。"""

    def __init__(self, path: Path) -> None:
        self._fh = path.open("w", encoding="utf-8")

    async def emit(self, envelope: EventEnvelope) -> None:
        """跳过临时事件，将持久化事件追加为一行 JSON 并立即刷新。"""
        if envelope.event.kind in TRANSIENT_KINDS:
            return
        self._fh.write(envelope.model_dump_json() + "\n")
        self._fh.flush()

    def close(self) -> None:
        """关闭 JSONL 输出文件。"""
        self._fh.close()
