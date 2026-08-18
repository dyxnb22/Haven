"""CLI 使用的事件输出适配器。"""

import re
from pathlib import Path

import typer

from haven.contracts.events import (
    TRANSIENT_KINDS,
    ApprovalRequested,
    DiffPreview,
    EventEnvelope,
    ModelCompleted,
    Notice,
    PolicyDecided,
    RunCreated,
    RunFinished,
    StepStarted,
    ToolCompleted,
    ToolProposed,
)

#: 控制字符会在任何内容到达终端前被移除。TUI 一直这样做
#:（`presenter.sanitize`）；无头输出以前会原样回显模型控制的文本，因此模型
#: 可以发出 ANSI 转义序列来改写操作员的终端或代码托管日志行。这里不需要
#: 转义 Rich 标记——typer.echo 写入普通字节，不会渲染标记。
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _plain(text: str) -> str:
    return _CONTROL_CHARS.sub("", text)


class ConsoleSink:
    """供无头运行和重放使用的紧凑、适合人类阅读的事件流。"""

    def __init__(self, verbose: bool = True) -> None:
        self._verbose = verbose

    async def emit(self, envelope: EventEnvelope) -> None:
        event = envelope.event
        line: str | None = None
        if isinstance(event, RunCreated):
            line = f"run {event.run_id} [{event.mode}] goal: {event.goal}"
        elif isinstance(event, StepStarted):
            line = f"step {event.step}"
        elif isinstance(event, ToolProposed):
            line = f"  tool {event.tool_name} {event.args_summary}"
        elif isinstance(event, PolicyDecided) and event.decision != "allow":
            line = f"  policy {event.decision} ({event.reason_code})"
        elif isinstance(event, ToolCompleted):
            state = event.status if not event.error_code else f"error:{event.error_code}"
            line = f"  -> {state} ({event.duration_ms}ms) {event.summary}"
        elif isinstance(event, ApprovalRequested):
            line = f"  approval needed: {event.summary}"
        elif isinstance(event, ModelCompleted) and event.text:
            line = f"assistant: {event.text}"
        elif isinstance(event, DiffPreview):
            line = f"  diff: {event.files_changed} file(s) +{event.insertions} -{event.deletions}"
        elif isinstance(event, Notice):
            line = f"  [{event.level}] {event.message}"
        elif isinstance(event, RunFinished):
            cached = f" cached={event.cached_input_tokens}" if event.cached_input_tokens else ""
            line = (
                f"finished: {event.status} ({event.stop_reason}) "
                f"steps={event.steps} tools={event.tool_calls} "
                f"tokens={event.input_tokens}/{event.output_tokens}{cached} "
                + (
                    f"cost=${event.cost_usd:.4f}"
                    if event.cost_known
                    else "cost=unknown (no rate card for this model)"
                )
                + (" (estimated)" if event.usage_estimated else "")
            )
        if line and self._verbose:
            typer.echo(_plain(line))


class NullSink:
    async def emit(self, envelope: EventEnvelope) -> None:
        return None


class JsonlEventSink:
    """实时将每个已持久化事件以一行 JSON 写入文件。"""

    def __init__(self, path: Path) -> None:
        self._fh = path.open("w", encoding="utf-8")

    async def emit(self, envelope: EventEnvelope) -> None:
        if envelope.event.kind in TRANSIENT_KINDS:
            return
        self._fh.write(envelope.model_dump_json() + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()
