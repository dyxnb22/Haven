"""模型流式事件组装和可证明安全的重试。"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Literal

from haven.application.emitter import EventEmitter
from haven.application.state import RunContext
from haven.contracts.events import (
    AssistantDelta,
    AssistantReasoning,
    Notice,
    StreamRestarted,
)
from haven.contracts.model import (
    ModelRequest,
    ModelResult,
    ReasoningDelta,
    StreamFinished,
    TextDelta,
    ToolCallProposal,
    ToolCallReady,
    Usage,
    UsageReport,
)
from haven.ports.model import ModelPort, ProviderError

MODEL_RETRY_ATTEMPTS = 2
MODEL_RETRY_BASE_DELAY = 1.0
MODEL_RETRY_MAX_DELAY = 60.0


def retry_delay(attempt: int, retry_after_s: float | None) -> float:
    backoff: float = MODEL_RETRY_BASE_DELAY * (2**attempt)
    wait = backoff if retry_after_s is None else max(backoff, retry_after_s)
    return min(wait, MODEL_RETRY_MAX_DELAY)


@dataclass(slots=True)
class _StreamProgress:
    started: bool = False


class ModelStreamer:
    """将一次 provider 流组装为 ModelResult，并在安全边界内重试。"""

    def __init__(self, emitter: EventEmitter) -> None:
        self._emitter = emitter

    async def stream(
        self, model: ModelPort, ctx: RunContext, step: int, request: ModelRequest
    ) -> ModelResult:
        for attempt in range(MODEL_RETRY_ATTEMPTS + 1):
            progress = _StreamProgress()
            try:
                return await self._stream_once(model, ctx, step, request, progress)
            except ProviderError as exc:
                exhausted = attempt == MODEL_RETRY_ATTEMPTS
                if not exc.retryable or exhausted:
                    raise
                if progress.started:
                    await self._emitter.emit(
                        ctx.run_id, StreamRestarted(run_id=ctx.run_id, step=step)
                    )
                delay = retry_delay(attempt, exc.retry_after_s)
                await self._emitter.emit(
                    ctx.run_id,
                    Notice(
                        run_id=ctx.run_id,
                        level="warning",
                        message=(
                            f"provider error ({exc.code}); retrying in {delay:.1f}s "
                            f"({attempt + 1}/{MODEL_RETRY_ATTEMPTS})"
                        ),
                    ),
                )
                await asyncio.sleep(delay)
        raise ProviderError("server", "model retry loop exhausted")

    async def _stream_once(
        self,
        model: ModelPort,
        ctx: RunContext,
        step: int,
        request: ModelRequest,
        progress: _StreamProgress,
    ) -> ModelResult:
        started = time.monotonic()
        ttft_ms = 0
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls: list[ToolCallProposal] = []
        usage = Usage()
        finish: Literal["stop", "tool_calls", "length", "error"] = "stop"

        async for event in model.generate_stream(request):
            progress.started = True
            if ttft_ms == 0:
                ttft_ms = max(1, int((time.monotonic() - started) * 1000))
            if isinstance(event, TextDelta):
                text_parts.append(event.text)
                await self._emitter.emit(
                    ctx.run_id,
                    AssistantDelta(run_id=ctx.run_id, step=step, text=event.text),
                )
            elif isinstance(event, ReasoningDelta):
                reasoning_parts.append(event.text)
                await self._emitter.emit(
                    ctx.run_id,
                    AssistantReasoning(run_id=ctx.run_id, step=step, text=event.text),
                )
            elif isinstance(event, ToolCallReady):
                tool_calls.append(event.call)
            elif isinstance(event, UsageReport):
                usage = event.usage
            elif isinstance(event, StreamFinished):
                finish = event.finish_reason

        return ModelResult(
            text="".join(text_parts),
            tool_calls=tuple(tool_calls),
            usage=usage,
            finish_reason="tool_calls" if tool_calls else finish,
            ttft_ms=ttft_ms,
            duration_ms=int((time.monotonic() - started) * 1000),
            provider_reasoning="".join(reasoning_parts),
        )
