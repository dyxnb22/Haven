"""OpenAI-compatible chat completions adapter (streaming).

Provider wire fields live only in this module. The adapter maps SSE chunks to
provider-neutral ModelEvents, enforces first-event and total timeouts, bounds
the response size, and converts failures to stable ProviderError codes. The
API key is read from configuration and never appears in errors or traces.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from haven.contracts.model import (
    ModelEvent,
    ModelMessage,
    ModelRequest,
    ReasoningDelta,
    StreamFinished,
    TextDelta,
    ToolCallProposal,
    ToolCallReady,
    Usage,
    UsageReport,
)
from haven.ports.model import ProviderError

MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class OpenAICompatibleModel:
    """Implements ModelPort over an OpenAI-compatible /chat/completions API."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        connect_timeout: float = 10.0,
        first_event_timeout: float = 30.0,
        total_timeout: float = 120.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._model = model
        self._first_event_timeout = first_event_timeout
        self._total_timeout = total_timeout
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(connect=connect_timeout, read=30.0, write=30.0, pool=10.0),
            transport=transport,
        )

    @property
    def model_name(self) -> str:
        return self._model

    async def aclose(self) -> None:
        await self._client.aclose()

    def generate_stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        return self._stream(request)

    async def _stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        payload = self._build_payload(request)
        # Exact reverse map for this request, so a wire name is never guessed.
        from_wire = {_to_wire_tool_name(t.name): t.name for t in request.tools}
        deadline = time.monotonic() + self._total_timeout

        try:
            async with self._client.stream("POST", "/chat/completions", json=payload) as response:
                if response.status_code != 200:
                    body = await response.aread()
                    raise self._map_status(response.status_code, body)

                collector = _ToolCallCollector()
                finish_reason: str = "stop"
                usage: Usage | None = None
                received_bytes = 0
                first = True

                lines = response.aiter_lines().__aiter__()
                while True:
                    timeout = (
                        self._first_event_timeout
                        if first
                        else max(0.0, deadline - time.monotonic())
                    )
                    try:
                        line = await asyncio.wait_for(anext(lines), timeout)
                    except StopAsyncIteration:
                        break
                    except TimeoutError:
                        code = "provider_ttft_timeout" if first else "provider_total_timeout"
                        raise ProviderError(
                            "timeout", f"{code} after {timeout:.0f}s", retryable=True
                        ) from None
                    first = False

                    received_bytes += len(line)
                    if received_bytes > MAX_RESPONSE_BYTES:
                        raise ProviderError("protocol", "response exceeded size limit")

                    data = _parse_sse_line(line)
                    if data is None:
                        continue
                    if data == "[DONE]":
                        break

                    try:
                        chunk: dict[str, Any] = json.loads(data)
                    except json.JSONDecodeError as exc:
                        raise ProviderError("protocol", f"malformed stream chunk: {exc}") from exc

                    if raw_usage := chunk.get("usage"):
                        details = raw_usage.get("completion_tokens_details") or {}
                        usage = Usage(
                            input_tokens=int(raw_usage.get("prompt_tokens", 0)),
                            output_tokens=int(raw_usage.get("completion_tokens", 0)),
                            reasoning_tokens=int(details.get("reasoning_tokens", 0)),
                            estimated=False,
                        )
                    for choice in chunk.get("choices", []):
                        if reason := choice.get("finish_reason"):
                            finish_reason = str(reason)
                        delta = choice.get("delta") or {}
                        if content := delta.get("content"):
                            yield TextDelta(text=str(content))
                        # Reasoning models stream their thinking in a separate
                        # field before any answer content appears.
                        if reasoning := delta.get("reasoning_content"):
                            yield ReasoningDelta(text=str(reasoning))
                        for tc in delta.get("tool_calls") or []:
                            collector.feed(tc)

                for call in collector.completed(from_wire):
                    yield ToolCallReady(call=call)
                if usage is not None:
                    yield UsageReport(usage=usage)
                yield StreamFinished(finish_reason=_map_finish_reason(finish_reason))
        except httpx.TimeoutException as exc:
            raise ProviderError("timeout", "network timeout", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ProviderError("network", f"transport error: {type(exc).__name__}") from exc

    def _build_payload(self, request: ModelRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self._model,
            "stream": True,
            "stream_options": {"include_usage": True},
            "temperature": request.temperature,
            "messages": [_to_wire_message(m) for m in request.messages],
        }
        if request.max_output_tokens is not None:
            payload["max_tokens"] = request.max_output_tokens
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": _to_wire_tool_name(tool.name),
                        "description": tool.description,
                        "parameters": _inline_refs(tool.parameters),
                    },
                }
                for tool in request.tools
            ]
        return payload

    @staticmethod
    def _map_status(status: int, body: bytes = b"") -> ProviderError:
        if status in (401, 403):
            # Never echo an auth failure body.
            return ProviderError("auth", "provider rejected credentials")
        if status == 429:
            return ProviderError("rate_limited", "provider rate limit", retryable=True)
        if status >= 500:
            return ProviderError("server", f"provider server error ({status})", retryable=True)
        # A 4xx here is almost always our request being malformed. The body says
        # what is wrong and contains no credentials, so surfacing a bounded
        # snippet turns an opaque failure into a fixable one.
        detail = body.decode("utf-8", errors="replace").strip()[:300]
        return ProviderError(
            "protocol",
            f"unexpected provider status ({status}): {detail}"
            if detail
            else f"unexpected provider status ({status})",
        )


#: OpenAI-compatible APIs constrain function names to ^[a-zA-Z0-9_-]+$, which
#: rejects Haven's namespaced `repo.read`. The dot is a core naming choice, so
#: the substitution lives here at the wire boundary and never leaks inward.
_WIRE_NAME_SEPARATOR = "__"


def _to_wire_tool_name(name: str) -> str:
    return name.replace(".", _WIRE_NAME_SEPARATOR)


def _inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Resolve Pydantic's `$defs`/`$ref` into a self-contained schema.

    Nested models generate a `$ref`, which several OpenAI-compatible providers
    reject in function parameters. Schemas here are small and non-recursive, so
    inlining them is safe and keeps the model-facing contract literal.
    """
    defs = schema.get("$defs")
    if not isinstance(defs, dict):
        return schema

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                target = defs.get(ref.rsplit("/", 1)[-1], {})
                merged = {**resolve(target), **{k: v for k, v in node.items() if k != "$ref"}}
                return merged
            return {k: resolve(v) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [resolve(item) for item in node]
        return node

    inlined = resolve(schema)
    return inlined if isinstance(inlined, dict) else schema


def _parse_sse_line(line: str) -> str | None:
    line = line.strip()
    if not line or line.startswith(":"):
        return None
    if line.startswith("data:"):
        return line[5:].strip()
    return None


def _map_finish_reason(reason: str) -> Any:
    mapping = {"stop": "stop", "tool_calls": "tool_calls", "length": "length"}
    return mapping.get(reason, "stop")


def _to_wire_message(message: ModelMessage) -> dict[str, Any]:
    wire: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.role == "assistant" and message.tool_calls:
        wire["tool_calls"] = [
            {
                "id": call.call_id,
                "type": "function",
                "function": {
                    "name": _to_wire_tool_name(call.tool_name),
                    "arguments": call.arguments_json,
                },
            }
            for call in message.tool_calls
        ]
    if message.role == "tool" and message.tool_call_id:
        wire["tool_call_id"] = message.tool_call_id
    return wire


class _ToolCallCollector:
    """Accumulates streamed tool-call deltas keyed by index."""

    def __init__(self) -> None:
        self._calls: dict[int, dict[str, str]] = {}

    def feed(self, delta: dict[str, Any]) -> None:
        index = int(delta.get("index", 0))
        slot = self._calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
        if call_id := delta.get("id"):
            slot["id"] = str(call_id)
        function = delta.get("function") or {}
        if name := function.get("name"):
            slot["name"] += str(name)
        if args := function.get("arguments"):
            slot["arguments"] += str(args)

    def completed(self, from_wire: dict[str, str]) -> list[ToolCallProposal]:
        calls = []
        for index in sorted(self._calls):
            slot = self._calls[index]
            if not slot["name"]:
                continue
            wire_name = slot["name"]
            calls.append(
                ToolCallProposal(
                    call_id=slot["id"] or f"call-{index}",
                    # Unknown names pass through unchanged so the registry can
                    # reject them with `unknown_tool` rather than being silently
                    # rewritten into something that exists.
                    tool_name=from_wire.get(wire_name, wire_name),
                    arguments_json=slot["arguments"] or "{}",
                )
            )
        return calls
