"""Contract tests for the OpenAI-compatible streaming adapter (offline)."""

import asyncio
import json
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
