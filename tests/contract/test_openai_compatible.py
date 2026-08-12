"""Contract tests for the OpenAI-compatible streaming adapter (offline)."""

import asyncio
import json
import ssl
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from haven.adapters.providers.openai_compatible import OpenAICompatibleModel
from haven.contracts.model import (
    ModelEvent,
    ModelMessage,
    ModelRequest,
    StreamFinished,
    TextDelta,
    ToolCallProposal,
    ToolCallReady,
    UsageReport,
)
from haven.ports.model import ProviderError

API_KEY = "sk-test-secret-key-do-not-leak"


def sse(payload: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode()


def chunk(delta: dict[str, Any], finish: str | None = None) -> bytes:
    return sse({"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]})


def make_model(handler: Any, **kwargs: Any) -> OpenAICompatibleModel:
    return OpenAICompatibleModel(
        base_url="https://api.example.com/v1",
        api_key=API_KEY,
        model="gpt-test",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def request() -> ModelRequest:
    return ModelRequest(messages=(ModelMessage(role="user", content="hi"),))


async def collect(model: OpenAICompatibleModel) -> list[ModelEvent]:
    try:
        return [event async for event in model.generate_stream(request())]
    finally:
        await model.aclose()


async def test_text_stream_and_usage() -> None:
    body = (
        chunk({"role": "assistant"})
        + chunk({"content": "Hel"})
        + chunk({"content": "lo"})
        + chunk({}, finish="stop")
        + sse({"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 3}})
        + b"data: [DONE]\n\n"
    )

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers["authorization"] == f"Bearer {API_KEY}"
        payload = json.loads(req.content)
        assert payload["stream"] is True
        return httpx.Response(200, content=body)

    events = await collect(make_model(handler))
    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert text == "Hello"
    usage = next(e for e in events if isinstance(e, UsageReport))
    assert usage.usage.input_tokens == 12
    assert usage.usage.estimated is False
    finished = events[-1]
    assert isinstance(finished, StreamFinished)


@pytest.mark.parametrize(
    "usage_payload",
    [
        {
            "prompt_tokens": 100,
            "completion_tokens": 5,
            "prompt_tokens_details": {"cached_tokens": 80},
        },
        {"prompt_tokens": 100, "completion_tokens": 5, "prompt_cache_hit_tokens": 80},
    ],
)
async def test_cached_input_tokens_parsed_from_either_wire_shape(usage_payload: dict) -> None:  # type: ignore[type-arg]
    body = (
        chunk({"content": "hi"})
        + chunk({}, finish="stop")
        + sse({"choices": [], "usage": usage_payload})
        + b"data: [DONE]\n\n"
    )
    events = await collect(make_model(lambda req: httpx.Response(200, content=body)))
    usage = next(e for e in events if isinstance(e, UsageReport))
    assert usage.usage.input_tokens == 100
    assert usage.usage.cached_input_tokens == 80


async def test_no_cache_field_means_zero(events_body: bytes | None = None) -> None:
    body = (
        chunk({"content": "hi"})
        + chunk({}, finish="stop")
        + sse({"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 2}})
        + b"data: [DONE]\n\n"
    )
    events = await collect(make_model(lambda req: httpx.Response(200, content=body)))
    usage = next(e for e in events if isinstance(e, UsageReport))
    assert usage.usage.cached_input_tokens == 0


async def test_tool_call_delta_assembly() -> None:
    body = (
        chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_abc",
                        "function": {"name": "repo.read", "arguments": ""},
                    }
                ]
            }
        )
        + chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"path":'}}]})
        + chunk({"tool_calls": [{"index": 0, "function": {"arguments": ' "a.py"}'}}]})
        + chunk({}, finish="tool_calls")
        + b"data: [DONE]\n\n"
    )

    events = await collect(make_model(lambda req: httpx.Response(200, content=body)))
    calls = [e.call for e in events if isinstance(e, ToolCallReady)]
    assert len(calls) == 1
    assert calls[0].call_id == "call_abc"
    assert calls[0].tool_name == "repo.read"
    assert json.loads(calls[0].arguments_json) == {"path": "a.py"}
    finished = events[-1]
    assert isinstance(finished, StreamFinished)
    assert finished.finish_reason == "tool_calls"


async def _send(req: ModelRequest) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, content=chunk({}, finish="stop") + b"data: [DONE]\n\n")

    model = make_model(handler)
    try:
        async for _ in model.generate_stream(req):
            pass
    finally:
        await model.aclose()
    return captured


def _assistant_with_tool_call(reasoning: str = "") -> ModelMessage:
    return ModelMessage(
        role="assistant",
        content="",
        tool_calls=(
            ToolCallProposal(call_id="c1", tool_name="repo.read", arguments_json='{"path":"a"}'),
        ),
        provider_reasoning=reasoning,
    )


async def _wire_messages(messages: tuple[ModelMessage, ...], *, requires_reasoning: bool) -> Any:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, content=chunk({}, finish="stop") + b"data: [DONE]\n\n")

    model = make_model(handler, requires_tool_call_reasoning=requires_reasoning)
    try:
        async for _ in model.generate_stream(ModelRequest(messages=messages)):
            pass
    finally:
        await model.aclose()
    return captured["messages"]


def _tool_result(call_id: str = "c1") -> ModelMessage:
    return ModelMessage(role="tool", content='{"ok":true}', tool_call_id=call_id)


class TestHistorySanitizer:
    """The wire boundary repairs the tool-call/tool-result pairing a strict
    provider 400s on, so no upstream path (compaction, recovery) can leak a
    malformed history onto the wire."""

    async def test_a_well_formed_history_is_unchanged(self) -> None:
        wire = await _wire_messages(
            (_assistant_with_tool_call("t"), _tool_result()), requires_reasoning=False
        )
        assert [m["role"] for m in wire] == ["assistant", "tool"]

    async def test_an_orphaned_tool_result_is_dropped(self) -> None:
        # A tool message answering a call that is not in the prior assistant
        # turn (its assistant turn was compacted away).
        wire = await _wire_messages(
            (ModelMessage(role="user", content="hi"), _tool_result("ghost")),
            requires_reasoning=False,
        )
        assert [m["role"] for m in wire] == ["user"]

    async def test_an_unanswered_tool_call_gets_a_synthetic_result(self) -> None:
        # The tool result was dropped; the dangling call is what 400s, so a
        # minimal error result is synthesized to close it.
        wire = await _wire_messages(
            (_assistant_with_tool_call("t"), ModelMessage(role="user", content="next")),
            requires_reasoning=False,
        )
        assert [m["role"] for m in wire] == ["assistant", "tool", "user"]
        assert wire[1]["tool_call_id"] == "c1"
        assert "unavailable" in wire[1]["content"]

    async def test_a_trailing_unanswered_call_is_closed(self) -> None:
        wire = await _wire_messages((_assistant_with_tool_call("t"),), requires_reasoning=False)
        assert [m["role"] for m in wire] == ["assistant", "tool"]


class TestMissingReasoningRetry:
    """If the profile flag is off but the provider demands replayed reasoning,
    the first request 400s before any event; the adapter retries once with
    replay forced on."""

    async def test_a_reasoning_400_is_retried_with_replay(self) -> None:
        calls: list[dict[str, Any]] = []

        def handler(req: httpx.Request) -> httpx.Response:
            body = json.loads(req.content)
            calls.append(body)
            if len(calls) == 1:
                return httpx.Response(
                    400, json={"error": {"message": "reasoning_content is required"}}
                )
            return httpx.Response(200, content=chunk({}, finish="stop") + b"data: [DONE]\n\n")

        model = make_model(handler, requires_tool_call_reasoning=False)
        try:
            messages = (_assistant_with_tool_call("prior thought"), _tool_result())
            events = [e async for e in model.generate_stream(ModelRequest(messages=messages))]
        finally:
            await model.aclose()

        assert len(calls) == 2, "the 400 was retried exactly once"
        assert "reasoning_content" not in calls[0]["messages"][0]
        assert calls[1]["messages"][0]["reasoning_content"] == "prior thought"
        assert any(isinstance(e, StreamFinished) for e in events)

    async def test_a_non_reasoning_400_is_not_retried(self) -> None:
        calls: list[dict[str, Any]] = []

        def handler(req: httpx.Request) -> httpx.Response:
            calls.append(json.loads(req.content))
            return httpx.Response(400, json={"error": {"message": "bad temperature"}})

        model = make_model(handler, requires_tool_call_reasoning=False)
        try:
            messages = (_assistant_with_tool_call("t"), _tool_result())
            with pytest.raises(ProviderError):
                async for _ in model.generate_stream(ModelRequest(messages=messages)):
                    pass
        finally:
            await model.aclose()
        assert len(calls) == 1, "a 400 unrelated to reasoning is not retried"


class TestPrefixContinuation:
    """Native prefix continuation (ADR 0022): a trailing assistant message
    flagged is_prefix goes on the wire with `prefix: true` so the provider
    extends it in place instead of replying."""

    async def test_a_prefix_assistant_message_carries_the_prefix_flag(self) -> None:
        partial = ModelMessage(role="assistant", content="The answer so far", is_prefix=True)
        wire = await _wire_messages((partial,), requires_reasoning=False)
        assert wire[-1]["role"] == "assistant"
        assert wire[-1]["prefix"] is True
        assert wire[-1]["content"] == "The answer so far"

    async def test_an_ordinary_assistant_message_has_no_prefix_flag(self) -> None:
        plain = ModelMessage(role="assistant", content="done")
        wire = await _wire_messages((plain,), requires_reasoning=False)
        assert "prefix" not in wire[-1]


class TestReasoningReplay:
    async def test_tool_call_turn_carries_reasoning_when_required(self) -> None:
        wire = await _wire_messages(
            (_assistant_with_tool_call("I will read a."),), requires_reasoning=True
        )
        assert wire[0]["reasoning_content"] == "I will read a."

    async def test_missing_reasoning_is_backfilled_with_empty_string(self) -> None:
        wire = await _wire_messages((_assistant_with_tool_call(""),), requires_reasoning=True)
        assert wire[0]["reasoning_content"] == ""

    async def test_no_reasoning_field_when_capability_is_off(self) -> None:
        wire = await _wire_messages(
            (_assistant_with_tool_call("secret think"),), requires_reasoning=False
        )
        assert "reasoning_content" not in wire[0]

    async def test_non_tool_assistant_turn_carries_no_reasoning(self) -> None:
        plain = ModelMessage(role="assistant", content="the answer", provider_reasoning="think")
        wire = await _wire_messages((plain,), requires_reasoning=True)
        assert "reasoning_content" not in wire[0]


async def test_reasoning_effort_is_sent_only_when_set() -> None:
    with_effort = await _send(request().model_copy(update={"reasoning_effort": "high"}))
    assert with_effort["reasoning_effort"] == "high"

    without = await _send(request())
    assert "reasoning_effort" not in without


async def test_interleaved_tool_calls_are_assembled_separately() -> None:
    """A model may emit several calls in one turn, and their argument deltas
    arrive interleaved. Each index must accumulate into its own call."""
    body = (
        chunk(
            {
                "tool_calls": [
                    {"index": 0, "id": "call_a", "function": {"name": "repo.read", "arguments": ""}}
                ]
            }
        )
        + chunk(
            {
                "tool_calls": [
                    {"index": 1, "id": "call_b", "function": {"name": "repo.list", "arguments": ""}}
                ]
            }
        )
        + chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"path":'}}]})
        + chunk({"tool_calls": [{"index": 1, "function": {"arguments": '{"path":'}}]})
        + chunk({"tool_calls": [{"index": 0, "function": {"arguments": ' "a.py"}'}}]})
        + chunk({"tool_calls": [{"index": 1, "function": {"arguments": ' "."}'}}]})
        + chunk({}, finish="tool_calls")
        + b"data: [DONE]\n\n"
    )

    events = await collect(make_model(lambda req: httpx.Response(200, content=body)))
    calls = [e.call for e in events if isinstance(e, ToolCallReady)]

    assert [c.call_id for c in calls] == ["call_a", "call_b"]
    assert [c.tool_name for c in calls] == ["repo.read", "repo.list"]
    assert json.loads(calls[0].arguments_json) == {"path": "a.py"}
    assert json.loads(calls[1].arguments_json) == {"path": "."}


@pytest.mark.parametrize(
    ("status", "code"),
    [(401, "auth"), (403, "auth"), (429, "rate_limited"), (500, "server"), (503, "server")],
)
async def test_error_status_mapping(status: int, code: str) -> None:
    model = make_model(lambda req: httpx.Response(status, json={"error": "x"}))
    with pytest.raises(ProviderError) as exc:
        await collect(model)
    assert exc.value.code == code


async def test_error_messages_never_contain_api_key() -> None:
    model = make_model(lambda req: httpx.Response(401, json={"error": "bad key"}))
    with pytest.raises(ProviderError) as exc:
        await collect(model)
    assert API_KEY not in str(exc.value)


async def test_malformed_json_chunk_is_protocol_error() -> None:
    body = b"data: {not valid json}\n\n"
    model = make_model(lambda req: httpx.Response(200, content=body))
    with pytest.raises(ProviderError) as exc:
        await collect(model)
    assert exc.value.code == "protocol"


class TestTransportErrorClassification:
    """Which transport failures the adapter marks retryable.

    RunService's retry loop only fires when `ProviderError.retryable` is set, so
    this classification is what decides whether a dropped connection costs a
    whole run. Its own tests construct retryable errors by hand, which is how a
    non-retryable ConnectError went unnoticed until 2 of 31 live real-repo cases
    died on one.
    """

    async def _error_from(self, handler: Any, **kwargs: Any) -> ProviderError:
        with pytest.raises(ProviderError) as exc:
            await collect(make_model(handler, **kwargs))
        return exc.value

    async def test_a_refused_connection_is_retryable(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        error = await self._error_from(handler)
        assert error.code == "network"
        assert error.retryable is True

    async def test_a_read_error_is_retryable(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            raise httpx.ReadError("connection reset by peer")

        assert (await self._error_from(handler)).retryable is True

    async def test_a_server_disconnect_mid_stream_is_retryable(self) -> None:
        class DroppingStream(httpx.AsyncByteStream):
            async def __aiter__(self) -> AsyncIterator[bytes]:
                yield chunk({"content": "par"})
                raise httpx.RemoteProtocolError("server disconnected")

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=DroppingStream())

        error = await self._error_from(handler)
        assert error.code == "network"
        assert error.retryable is True

    async def test_a_misconfigured_url_is_not_retryable(self) -> None:
        """Retrying a configuration mistake just burns the budget slower."""

        def handler(req: httpx.Request) -> httpx.Response:
            raise httpx.UnsupportedProtocol("unsupported scheme")

        assert (await self._error_from(handler)).retryable is False

    async def test_the_raw_exception_text_does_not_leak(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(f"failed talking to {API_KEY}@host")

        assert API_KEY not in str(await self._error_from(handler))

    async def test_a_tls_failure_becomes_a_retryable_provider_error(self) -> None:
        """Found live: a TLS record-layer failure is not an httpx.HTTPError, so
        it escaped unwrapped, sailed past RunService's `except ProviderError`,
        and took down a whole 31-case suite mid-run."""

        def handler(req: httpx.Request) -> httpx.Response:
            raise ssl.SSLError("[SSL] record layer failure")

        error = await self._error_from(handler)
        assert error.code == "network"
        assert error.retryable is True

    async def test_a_connection_reset_becomes_a_retryable_provider_error(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            raise ConnectionResetError("peer reset the connection")

        assert (await self._error_from(handler)).retryable is True

    async def test_a_bad_certificate_is_not_retryable(self) -> None:
        """Retrying an untrusted certificate cannot make it trusted."""

        def handler(req: httpx.Request) -> httpx.Response:
            raise ssl.SSLCertVerificationError("certificate verify failed")

        assert (await self._error_from(handler)).retryable is False

    async def test_a_programming_error_is_not_disguised_as_a_network_fault(self) -> None:
        """Only I/O failures are translated; a logic bug must surface as itself
        rather than being reported (and retried) as a network blip."""

        def handler(req: httpx.Request) -> httpx.Response:
            raise KeyError("a bug in our own parsing")

        with pytest.raises(KeyError):
            await collect(make_model(handler))


async def test_first_event_timeout() -> None:
    class HangingStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            await asyncio.sleep(30)
            yield b""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=HangingStream())

    model = make_model(handler, first_event_timeout=0.2)
    with pytest.raises(ProviderError) as exc:
        await collect(model)
    assert exc.value.code == "timeout"
    assert "ttft" in str(exc.value)


async def test_cancellation_mid_stream() -> None:
    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield chunk({"content": "one"})
            await asyncio.sleep(30)
            yield chunk({"content": "never"})

    model = make_model(lambda req: httpx.Response(200, stream=SlowStream()))

    async def consume() -> None:
        async for _ in model.generate_stream(request()):
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await model.aclose()


async def test_response_size_limit() -> None:
    huge = chunk({"content": "x" * 1024}) * 8192  # far beyond 4 MiB

    model = make_model(lambda req: httpx.Response(200, content=huge))
    with pytest.raises(ProviderError) as exc:
        await collect(model)
    assert exc.value.code == "protocol"
    assert "size" in str(exc.value)
