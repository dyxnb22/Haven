"""OpenAI-compatible chat completions adapter (streaming).

Provider wire fields live only in this module. The adapter maps SSE chunks to
provider-neutral ModelEvents, enforces first-event and total timeouts, bounds
the response size, and converts failures to stable ProviderError codes. The
API key is read from configuration and never appears in errors or traces.
"""

from __future__ import annotations

import asyncio
import json
import ssl
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
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
    """Implements ModelPort over an OpenAI-compatible /chat/completions API.

    Call path for one request:

        generate_stream -> _stream_with_reasoning_retry   (one precise retry)
          -> _stream          SSE loop with first-event + total deadlines
             -> _build_payload   sanitized history -> wire JSON
             -> _ToolCallCollector   accumulates streamed tool-call deltas

    yielding provider-neutral ModelEvents (TextDelta / ReasoningDelta /
    ToolCallReady / StreamFinished). Retryable transport failures raise
    ProviderError("network"|"timeout"...) and are retried by the run loop,
    never silently inside this adapter (a partial stream must surface).
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        connect_timeout: float = 10.0,
        first_event_timeout: float = 30.0,
        idle_timeout: float = 120.0,
        transport: httpx.AsyncBaseTransport | None = None,
        requires_tool_call_reasoning: bool = False,
    ) -> None:
        self._model = model
        self._first_event_timeout = first_event_timeout
        # Bounds the gap *between* streamed events, reset on every one — not a
        # total deadline. A reasoning model (DeepSeek `high`/`max`) can stream
        # steadily for minutes; only a genuine stall should time out. The
        # overall run stays bounded by RunService's wall-clock budget.
        self._idle_timeout = idle_timeout
        # DeepSeek V4 rejects a tool-call assistant turn whose reasoning_content
        # is not replayed (ADR 0014); set per model profile at bootstrap.
        self._requires_tool_call_reasoning = requires_tool_call_reasoning
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
        return self._stream_with_reasoning_retry(request)

    async def _stream_with_reasoning_retry(
        self, request: ModelRequest
    ) -> AsyncIterator[ModelEvent]:
        """Self-heal the one 400 that reasoning replay exists to prevent.

        If the profile flag is off but the provider actually demands replayed
        reasoning on a tool-call turn, the first request 400s before any event
        is yielded. That is safe to retry precisely — nothing was emitted — so
        we re-issue once with replay forced on. A 400 for any other reason, or
        one after streaming has begun, is not retried here (the run loop's
        bounded retry still covers genuinely transient failures).
        """
        force_reasoning = self._requires_tool_call_reasoning
        yielded_any = False
        try:
            async for event in self._stream(request, replay_reasoning=force_reasoning):
                yielded_any = True
                yield event
            return
        except ProviderError as exc:
            retryable_reasoning = (
                not yielded_any  # a mid-stream retry would double-emit deltas
                and not force_reasoning
                and exc.code == "protocol"
                and "reasoning" in str(exc).lower()
                and any(m.role == "assistant" and m.tool_calls for m in request.messages)
            )
            if not retryable_reasoning:
                raise
        # Retry path, outside the except block so its own errors surface plainly.
        async for event in self._stream(request, replay_reasoning=True):
            yield event

    async def _stream(
        self, request: ModelRequest, *, replay_reasoning: bool
    ) -> AsyncIterator[ModelEvent]:
        payload = self._build_payload(request, replay_reasoning=replay_reasoning)
        # Exact reverse map for this request, so a wire name is never guessed.
        from_wire = {_to_wire_tool_name(t.name): t.name for t in request.tools}

        try:
            async with self._client.stream("POST", "/chat/completions", json=payload) as response:
                if response.status_code != 200:
                    body = await response.aread()
                    raise self._map_status(response.status_code, body, response.headers)

                collector = _ToolCallCollector()
                finish_reason: str = "stop"
                usage: Usage | None = None
                received_bytes = 0
                first = True

                lines = response.aiter_lines().__aiter__()
                while True:
                    # First read bounds time-to-first-token; every read after it
                    # bounds the idle gap and resets on arrival, so a long but
                    # steady generation is never cut short.
                    timeout = self._first_event_timeout if first else self._idle_timeout
                    try:
                        line = await asyncio.wait_for(anext(lines), timeout)
                    except StopAsyncIteration:
                        break
                    except TimeoutError:
                        code = "provider_ttft_timeout" if first else "provider_idle_timeout"
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
                        prompt_details = raw_usage.get("prompt_tokens_details") or {}
                        # OpenAI reports cache hits under prompt_tokens_details;
                        # DeepSeek uses a top-level prompt_cache_hit_tokens.
                        cached = int(prompt_details.get("cached_tokens", 0)) or int(
                            raw_usage.get("prompt_cache_hit_tokens", 0)
                        )
                        usage = Usage(
                            input_tokens=int(raw_usage.get("prompt_tokens", 0)),
                            output_tokens=int(raw_usage.get("completion_tokens", 0)),
                            reasoning_tokens=int(details.get("reasoning_tokens", 0)),
                            cached_input_tokens=cached,
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
            # A dropped, reset, or server-closed connection is transient, and a
            # model call has no side effects, so RunService may safely retry it.
            # A protocol or URL misconfiguration fails identically every time,
            # so retrying it would only burn the budget more slowly.
            transient = isinstance(exc, httpx.NetworkError | httpx.RemoteProtocolError)
            raise ProviderError(
                "network", f"transport error: {type(exc).__name__}", retryable=transient
            ) from exc
        except OSError as exc:
            # Not every I/O failure arrives wrapped as an httpx error: a TLS
            # record-layer failure surfaces as ssl.SSLError (an OSError), and
            # escaping unwrapped means it sails past every `except ProviderError`
            # and kills the run. Certificate rejection is the one that retrying
            # cannot fix. `except Exception` is deliberately not used here, so a
            # bug in our own parsing still surfaces as itself.
            raise ProviderError(
                "network",
                f"transport error: {type(exc).__name__}",
                retryable=not isinstance(exc, ssl.SSLCertVerificationError),
            ) from exc

    def _build_payload(
        self, request: ModelRequest, *, replay_reasoning: bool | None = None
    ) -> dict[str, Any]:
        replay = (
            self._requires_tool_call_reasoning if replay_reasoning is None else replay_reasoning
        )
        payload: dict[str, Any] = {
            "model": self._model,
            "stream": True,
            "stream_options": {"include_usage": True},
            "temperature": request.temperature,
            "messages": [
                _to_wire_message(m, replay_reasoning=replay)
                for m in _sanitize_history(request.messages)
            ],
        }
        if request.max_output_tokens is not None:
            payload["max_tokens"] = request.max_output_tokens
        if request.reasoning_effort is not None:
            payload["reasoning_effort"] = request.reasoning_effort
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
    def _map_status(
        status: int, body: bytes = b"", headers: httpx.Headers | None = None
    ) -> ProviderError:
        if status in (401, 403):
            # Never echo an auth failure body.
            return ProviderError("auth", "provider rejected credentials")
        if status == 402:
            # DeepSeek returns 402 "Insufficient Balance" when the account is out
            # of credit. Retrying cannot add funds, so this is terminal and gets
            # its own code — a user seeing `quota` knows to top up, where the
            # generic `protocol` bucket would read as a Haven bug.
            return ProviderError("quota", "provider account has insufficient balance")
        retry_after = _parse_retry_after(headers.get("retry-after") if headers else None)
        if status == 429:
            return ProviderError(
                "rate_limited", "provider rate limit", retryable=True, retry_after_s=retry_after
            )
        if status >= 500:
            return ProviderError(
                "server",
                f"provider server error ({status})",
                retryable=True,
                retry_after_s=retry_after,
            )
        # A 4xx here is almost always our request being malformed. The body says
        # what is wrong and contains no credentials, so surfacing a bounded
        # snippet turns an opaque failure into a fixable one.
        detail = body.decode("utf-8", errors="replace").strip()[:300]
        if status == 400 and _is_context_overflow(detail):
            # The prompt is too long for the model's window. Unlike other 400s
            # this is recoverable: RunService can force compaction and retry, so
            # it gets a distinct code rather than the terminal `protocol` bucket.
            return ProviderError("context_overflow", "provider context window exceeded")
        return ProviderError(
            "protocol",
            f"unexpected provider status ({status}): {detail}"
            if detail
            else f"unexpected provider status ({status})",
        )


# -- wire translation helpers -------------------------------------------------
# Everything below converts between Haven's provider-neutral contracts
# (contracts/model.py) and the OpenAI chat-completions wire format. Provider
# quirks are absorbed here so nothing upstream ever sees them.

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


#: Substrings that identify a "prompt too long" 400 across OpenAI-compatible
#: providers. DeepSeek and OpenAI phrase it as "maximum context length"; the
#: canonical OpenAI error code is `context_length_exceeded`. Matching on the
#: message keeps this at the wire boundary rather than leaking provider strings
#: inward — the core only ever sees the `context_overflow` code.
_CONTEXT_OVERFLOW_MARKERS = ("maximum context length", "context_length_exceeded", "context length")


def _is_context_overflow(detail: str) -> bool:
    lowered = detail.lower()
    return any(marker in lowered for marker in _CONTEXT_OVERFLOW_MARKERS)


def _parse_retry_after(value: str | None) -> float | None:
    """Seconds to wait from a `Retry-After` header, or None.

    The header is either a non-negative integer count of seconds or an HTTP
    date; both forms appear in the wild. A past or unparseable date yields
    None rather than a negative or bogus delay, so a malformed header can only
    ever fall back to the retry loop's own backoff, never shorten it.
    """
    if value is None:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    from email.utils import parsedate_to_datetime

    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    delta = (when - datetime.now(tz=UTC)).total_seconds()
    return delta if delta > 0 else None


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


def _sanitize_history(messages: Sequence[ModelMessage]) -> list[ModelMessage]:
    """Repair structural defects a strict provider (DeepSeek V4) 400s on.

    Every OpenAI-compatible provider enforces the same tool-call/tool-result
    contract: an assistant turn's tool_calls must each be answered by exactly
    one following tool message, and a tool message must answer a tool_call in
    the immediately preceding assistant turn. Compaction and crash recovery
    can leave that invariant locally broken; rather than trust every upstream
    path to keep it, the adapter enforces it deterministically at the wire
    boundary — the one place every request passes through.

    Repairs (all deterministic, none inventing content):
    - drop a tool message that answers no in-flight tool_call (orphaned
      result — the assistant turn that made the call was dropped);
    - synthesize a minimal error tool result for any tool_call the history
      never answered (orphaned call — the result was dropped), because a
      dangling call is exactly what triggers the 400.
    """
    out: list[ModelMessage] = []
    pending: dict[str, int] = {}  # unanswered call_id -> index in `out`

    def flush_pending() -> None:
        # Any tool_call still unanswered when the next assistant/user turn
        # begins gets a synthetic result appended, in call order.
        for call_id in list(pending):
            out.append(
                ModelMessage(
                    role="tool",
                    content='{"status":"error","message":"result unavailable '
                    '(dropped from history)"}',
                    tool_call_id=call_id,
                )
            )
        pending.clear()

    for message in messages:
        if message.role == "tool":
            if message.tool_call_id and message.tool_call_id in pending:
                del pending[message.tool_call_id]
                out.append(message)
            # else: orphaned result — drop it silently.
            continue
        # A new assistant or user turn: close out any unanswered calls first.
        flush_pending()
        out.append(message)
        if message.role == "assistant":
            for call in message.tool_calls:
                pending[call.call_id] = len(out) - 1
    flush_pending()
    return out


def _to_wire_message(message: ModelMessage, *, replay_reasoning: bool = False) -> dict[str, Any]:
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
        # DeepSeek V4 requires the reasoning that preceded a tool call to be
        # replayed verbatim, or it 400s; an empty string is the accepted
        # back-fill for history that predates capture (ADR 0014).
        if replay_reasoning:
            wire["reasoning_content"] = message.provider_reasoning
    if message.role == "tool" and message.tool_call_id:
        wire["tool_call_id"] = message.tool_call_id
    if message.role == "assistant" and message.is_prefix:
        # Native prefix continuation (ADR 0022): the provider extends this
        # partial content in place instead of replying to it. DeepSeek marks
        # such a message with `prefix: true` (its beta prefix-completion mode).
        wire["prefix"] = True
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
