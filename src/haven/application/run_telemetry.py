"""模型请求信封与用量计费记录。"""

from __future__ import annotations

from haven.application.emitter import EventEmitter
from haven.application.state import RunContext
from haven.contracts.events import RequestEnvelope
from haven.contracts.model import ModelRequest, ModelResult
from haven.domain.digest import digest_of
from haven.domain.pricing import Pricing


class RunTelemetry:
    def __init__(self, emitter: EventEmitter, pricing: Pricing) -> None:
        self._emitter = emitter
        self._pricing = pricing

    async def record_envelope(
        self, ctx: RunContext, step: int, request: ModelRequest, previous: str
    ) -> str:
        system = next(
            (message.content for message in request.messages if message.role == "system"), ""
        )
        tool_names = tuple(tool.name for tool in request.tools)
        digest = digest_of(
            {
                "system": system,
                "tools": list(tool_names),
                "reasoning_effort": request.reasoning_effort or "",
                "max_output_tokens": request.max_output_tokens or 0,
            }
        )
        if digest == previous:
            return digest
        await self._emitter.emit(
            ctx.run_id,
            RequestEnvelope(
                run_id=ctx.run_id,
                step=step,
                reason="initial" if not previous else "changed",
                system_prompt_digest=digest,
                system_prompt_chars=len(system),
                tool_names=tool_names,
                reasoning_effort=request.reasoning_effort or "",
                max_output_tokens=request.max_output_tokens or 0,
            ),
        )
        return digest

    def charge_usage(self, ctx: RunContext, request: ModelRequest, result: ModelResult) -> None:
        usage = result.usage
        estimated = False
        input_tokens = usage.input_tokens
        output_tokens = usage.output_tokens
        if input_tokens == 0 and output_tokens == 0:
            estimated = True
            input_tokens = sum(len(message.content) for message in request.messages) // 4
            output_tokens = max(1, len(result.text) // 4)
        cost = self._pricing.cost(input_tokens, output_tokens, usage.cached_input_tokens)
        ctx.usage = ctx.usage.charge_tokens(
            input_tokens,
            output_tokens,
            cost,
            estimated=estimated,
            cached_input_tokens=usage.cached_input_tokens,
        )
