"""OpenAI 兼容流式适配器的契约测试（离线）。"""

import asyncio
import json
import ssl
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from haven.adapters.providers import openai_compatible
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


async def test_historical_native_prefix_is_not_sent_on_follow_up() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        messages = json.loads(req.content)["messages"]
        assert messages[0] == {"role": "assistant", "content": "Previous reply"}
        return httpx.Response(200, content=chunk({"content": "Okay"}, finish="stop"))

    model = make_model(handler)
    follow_up = ModelRequest(
        messages=(
            ModelMessage(role="assistant", content="Previous reply", is_prefix=True),
            ModelMessage(role="user", content="Follow up"),
        )
    )
    try:
        assert [event async for event in model.generate_stream(follow_up)]
    finally:
        await model.aclose()


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


async def test_no_cache_field_means_zero() -> None:
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
    """线协议边界会修复严格提供商会返回 400 的工具调用/工具结果配对，因此任何
    上游路径（压缩、恢复）都不能将格式错误的历史记录泄漏到线协议上。"""

    async def test_a_well_formed_history_is_unchanged(self) -> None:
        wire = await _wire_messages(
            (_assistant_with_tool_call("t"), _tool_result()), requires_reasoning=False
        )
        assert [m["role"] for m in wire] == ["assistant", "tool"]

    async def test_an_orphaned_tool_result_is_dropped(self) -> None:
        # 工具消息回答的调用不在上一条 assistant 轮次中（那条 assistant 轮次
        # 已被压缩丢弃）。
        wire = await _wire_messages(
            (ModelMessage(role="user", content="hi"), _tool_result("ghost")),
            requires_reasoning=False,
        )
        assert [m["role"] for m in wire] == ["user"]

    async def test_an_unanswered_tool_call_gets_a_synthetic_result(self) -> None:
        # 工具结果被丢弃；悬空调用正是会触发 400 的原因，因此合成一个最小
        # 错误结果来关闭它。
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
    """如果 profile 标志关闭但提供商要求重放 reasoning，第一个请求会在任何事件
    产生前返回 400；适配器会强制开启重放并重试一次。"""

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
    """原生前缀续写（ADR 0022）：标记为 is_prefix 的末尾 assistant 消息会以
    `prefix: true` 进入线协议，使提供商在原位置续写，而不是直接回答。"""

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
    """模型可能在一轮中发出多个调用，参数增量会交错到达。每个索引都必须累积到
    自己的调用中。"""
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


class TestRetryAfter:
    """429/503 可能带有 `Retry-After` 标头，说明何时重试。遵守该标头优于使用固定
    退避，因为固定退避会在提供商要求更长暂停时持续施压。适配器暴露这一提示，
    RunService 决定等待时长。"""

    async def test_a_429_surfaces_retry_after_seconds(self) -> None:
        model = make_model(
            lambda req: httpx.Response(429, headers={"retry-after": "5"}, json={"error": "slow"})
        )
        with pytest.raises(ProviderError) as exc:
            await collect(model)
        assert exc.value.retry_after_s == 5.0

    async def test_a_retry_after_http_date_is_converted_to_seconds(self) -> None:
        from datetime import UTC, datetime, timedelta
        from email.utils import format_datetime

        when = format_datetime(datetime.now(tz=UTC) + timedelta(seconds=30))
        model = make_model(
            lambda req: httpx.Response(503, headers={"retry-after": when}, json={"error": "x"})
        )
        with pytest.raises(ProviderError) as exc:
            await collect(model)
        assert exc.value.retry_after_s is not None
        # 为请求期间经过的一秒留出少量余量。
        assert 20.0 <= exc.value.retry_after_s <= 30.0

    async def test_no_retry_after_header_leaves_the_hint_unset(self) -> None:
        model = make_model(lambda req: httpx.Response(429, json={"error": "x"}))
        with pytest.raises(ProviderError) as exc:
            await collect(model)
        assert exc.value.retry_after_s is None


@pytest.mark.parametrize(
    "message",
    [
        "This model's maximum context length is 65536 tokens. However, you requested 70000.",
        "context_length_exceeded: please reduce the length of the messages",
    ],
)
async def test_a_context_length_400_maps_to_context_overflow(message: str) -> None:
    """表示“提示词过长”的提供商 400 必须映射为独立且可恢复的信号——RunService
    可以强制压缩并重试——而不是与其他格式错误请求一样使用终止性的通用
    `protocol` 错误。"""
    model = make_model(lambda req: httpx.Response(400, json={"error": {"message": message}}))
    with pytest.raises(ProviderError) as exc:
        await collect(model)
    assert exc.value.code == "context_overflow"


async def test_an_unrelated_400_stays_a_protocol_error() -> None:
    model = make_model(
        lambda req: httpx.Response(400, json={"error": {"message": "invalid temperature"}})
    )
    with pytest.raises(ProviderError) as exc:
        await collect(model)
    assert exc.value.code == "protocol"


async def test_insufficient_balance_maps_to_a_non_retryable_quota_error() -> None:
    """账户余额不足时 DeepSeek 返回 402。重试不能增加余额，因此必须暴露为独立的
    终止性 `quota` 代码，而不是用户无法据此行动的模糊 `protocol` 类别。"""
    model = make_model(
        lambda req: httpx.Response(402, json={"error": {"message": "Insufficient Balance"}})
    )
    with pytest.raises(ProviderError) as exc:
        await collect(model)
    assert exc.value.code == "quota"
    assert exc.value.retryable is False


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


@pytest.mark.parametrize(
    "malformed",
    [
        [],
        {"choices": "not-a-list"},
        {"choices": ["not-an-object"]},
        {"choices": [], "usage": "not-an-object"},
        {"choices": [], "usage": {"prompt_tokens": "not-a-number"}},
        {
            "choices": [],
            "usage": {"prompt_tokens": 1, "prompt_tokens_details": {"cached_tokens": 2}},
        },
    ],
)
async def test_malformed_stream_fields_are_protocol_errors(malformed: object) -> None:
    model = make_model(
        lambda req: httpx.Response(200, content=sse(malformed) + b"data: [DONE]\n\n")  # type: ignore[arg-type]
    )
    with pytest.raises(ProviderError) as exc:
        await collect(model)
    assert exc.value.code == "protocol"


async def test_response_limit_counts_utf8_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    line = "data: " + json.dumps({"choices": [], "note": "汉汉汉汉"}, ensure_ascii=False)
    # 字符计数会刚好放行；UTF-8 字节计数必须拒绝。
    monkeypatch.setattr(openai_compatible, "MAX_RESPONSE_BYTES", len(line) + 2)
    body = line.encode() + b"\n\n"
    model = make_model(lambda req: httpx.Response(200, content=body))
    with pytest.raises(ProviderError, match="size limit"):
        await collect(model)


async def test_error_response_body_is_read_with_a_hard_limit() -> None:
    class CountingStream(httpx.AsyncByteStream):
        chunks = 0

        async def __aiter__(self) -> AsyncIterator[bytes]:
            for _ in range(20):
                type(self).chunks += 1
                yield b"x" * 2048

    model = make_model(lambda req: httpx.Response(400, stream=CountingStream()))
    with pytest.raises(ProviderError):
        await collect(model)
    assert CountingStream.chunks <= 2


class TestTransportErrorClassification:
    """适配器将哪些传输失败标记为可重试。

    RunService 的重试循环只有在设置 `ProviderError.retryable` 时才会触发，因此
    该分类决定连接断开是否会耗尽整个运行。此类测试会手动构造可重试错误，这正是
    非可重试 ConnectError 一直未被发现的原因，直到 31 个实时真实仓库用例中有 2 个
    因此失败。
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
        """重试配置错误只会让预算消耗得更慢。"""

        def handler(req: httpx.Request) -> httpx.Response:
            raise httpx.UnsupportedProtocol("unsupported scheme")

        assert (await self._error_from(handler)).retryable is False

    async def test_the_raw_exception_text_does_not_leak(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(f"failed talking to {API_KEY}@host")

        assert API_KEY not in str(await self._error_from(handler))

    async def test_a_tls_failure_becomes_a_retryable_provider_error(self) -> None:
        """实时运行中发现：TLS 记录层失败不是 httpx.HTTPError，因此它曾未经包装地
        逃出，绕过 RunService 的 `except ProviderError`，并在套件运行中途击垮整个
        31 用例套件。"""

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
        """重试不受信任的证书不会使它变得可信。"""

        def handler(req: httpx.Request) -> httpx.Response:
            raise ssl.SSLCertVerificationError("certificate verify failed")

        assert (await self._error_from(handler)).retryable is False

    async def test_a_programming_error_is_not_disguised_as_a_network_fault(self) -> None:
        """只有 I/O 失败会被转换；逻辑错误必须以自身形式暴露，而不是被报告（并作为
        网络抖动重试）。"""

        def handler(req: httpx.Request) -> httpx.Response:
            raise KeyError("a bug in our own parsing")

        with pytest.raises(KeyError):
            await collect(make_model(handler))


class TestIdleTimeout:
    """TTFT 之后的超时限制的是事件*之间*的间隔，而不是整个流。持续稳定流式输出
    数分钟的推理模型不应因总截止时间被杀死；只有真正卡住时才应超时。"""

    async def test_a_slow_but_steady_stream_outlasts_the_idle_bound(self) -> None:
        # 五个间隔 0.1 秒的事件总跨度为 0.5 秒，远超 0.3 秒的空闲上限；但
        # 没有任何单个间隔超过上限，因此流必须完成。总截止时间（旧行为）
        # 会在流式过程中杀掉它。
        class SteadyStream(httpx.AsyncByteStream):
            async def __aiter__(self) -> AsyncIterator[bytes]:
                for i in range(5):
                    await asyncio.sleep(0.1)
                    yield chunk({"content": str(i)})
                yield chunk({}, finish="stop")
                yield b"data: [DONE]\n\n"

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=SteadyStream())

        model = make_model(handler, idle_timeout=0.3)
        events = await collect(model)
        text = "".join(e.text for e in events if isinstance(e, TextDelta))
        assert text == "01234"
        assert isinstance(events[-1], StreamFinished)

    async def test_a_stalled_stream_times_out_on_the_gap(self) -> None:
        class StallingStream(httpx.AsyncByteStream):
            async def __aiter__(self) -> AsyncIterator[bytes]:
                yield chunk({"content": "one"})
                await asyncio.sleep(30)
                yield chunk({}, finish="stop")

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=StallingStream())

        model = make_model(handler, idle_timeout=0.2)
        with pytest.raises(ProviderError) as exc:
            await collect(model)
        assert exc.value.code == "timeout"
        assert exc.value.retryable is True


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
    huge = chunk({"content": "x" * 1024}) * 8192  # 远超 4 MiB

    model = make_model(lambda req: httpx.Response(200, content=huge))
    with pytest.raises(ProviderError) as exc:
        await collect(model)
    assert exc.value.code == "protocol"
    assert "size" in str(exc.value)


def test_inline_refs_handles_circular_schema() -> None:
    from haven.adapters.providers.openai_wire import inline_refs

    circular_schema = {
        "$defs": {
            "TreeNode": {
                "type": "object",
                "properties": {
                    "val": {"type": "integer"},
                    "child": {"$ref": "#/$defs/TreeNode"},
                },
            }
        },
        "type": "object",
        "properties": {"root": {"$ref": "#/$defs/TreeNode"}},
    }
    inlined = inline_refs(circular_schema)
    assert isinstance(inlined, dict)
    assert "root" in inlined["properties"]


def test_tool_call_collector_handles_null_index() -> None:
    from haven.adapters.providers.openai_wire import ToolCallCollector

    collector = ToolCallCollector()
    collector.feed({"index": None, "id": "call-0", "function": {"name": "test_tool"}})
    completed = collector.completed({"test_tool": "test_tool"})
    assert len(completed) == 1
    assert completed[0].call_id == "call-0"


# -- 真实网络上的空闲超时 ------------------------------------------------------
#
# 下面这些用例必须用真实的 TCP 服务端，不能用 `httpx.MockTransport`：内存字节流
# 根本不经过 httpcore 的网络读取层，也就不会触发 httpx 自己的 read 超时。这个 bug
# 正是那样存活下来的——所有既有的流式超时测试用的都是 MockTransport。


async def _serve_sse_with_a_gap(gap_seconds: float) -> tuple[asyncio.Server, int]:
    """起一个真的 HTTP 服务端：发完头就沉默 `gap_seconds`，再发一行 SSE。"""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, ConnectionError):
            return
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"content-type: text/event-stream\r\n"
            b"transfer-encoding: chunked\r\n\r\n"
        )
        await writer.drain()
        await asyncio.sleep(gap_seconds)
        for payload in (
            b'data: {"choices":[{"delta":{"content":"late"}}]}\n\n',
            b"data: [DONE]\n\n",
        ):
            writer.write(f"{len(payload):x}".encode() + b"\r\n" + payload + b"\r\n")
        await writer.drain()
        writer.write(b"0\r\n\r\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def test_a_gap_longer_than_httpxs_default_read_timeout_still_streams() -> None:
    """空闲超过 httpx 默认 30 秒 read 超时的流必须能继续，而不是被网络层杀掉。

    推理模型（DeepSeek `high`/`max`）可能连续数分钟不出一个事件；`stream_idle_timeout`
    就是为这个准备的。此前 httpx 的 read 固定为 30.0，于是它先炸，idle 超时永远没机会
    触发，错误还是笼统的 "network timeout"——重试也治不好，因为每次都一样。
    """
    gap = 2.0  # 比 30 秒短，但足以暴露"谁先触发"的顺序问题
    server, port = await _serve_sse_with_a_gap(gap)
    model = OpenAICompatibleModel(
        base_url=f"http://127.0.0.1:{port}/v1",
        api_key=API_KEY,
        model="gpt-test",
        # 故意把空闲超时设得比间隔长：这一轮本该成功
        idle_timeout=gap * 4,
        first_event_timeout=gap * 4,
    )
    try:
        events = await collect(model)
        text = "".join(e.text for e in events if isinstance(e, TextDelta))
        assert text == "late"
        assert isinstance(events[-1], StreamFinished)
    finally:
        server.close()
        await server.wait_closed()


async def test_the_idle_timeout_is_what_fires_not_httpxs_read_timeout() -> None:
    """真正卡住时，必须由外层空闲超时给出 `provider_idle_timeout` 这个准确错误码。

    首事件要快到（于是走的是 idle 分支，不是 ttft 分支），之后才沉默。
    """
    stall = 2.0

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, ConnectionError):
            return
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"content-type: text/event-stream\r\n"
            b"transfer-encoding: chunked\r\n\r\n"
        )
        payload = b'data: {"choices":[{"delta":{"content":"first"}}]}\n\n'
        writer.write(f"{len(payload):x}".encode() + b"\r\n" + payload + b"\r\n")
        await writer.drain()
        await asyncio.sleep(stall)
        tail = b"data: [DONE]\n\n"
        writer.write(f"{len(tail):x}".encode() + b"\r\n" + tail + b"\r\n0\r\n\r\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    model = OpenAICompatibleModel(
        base_url=f"http://127.0.0.1:{port}/v1",
        api_key=API_KEY,
        model="gpt-test",
        idle_timeout=stall / 4,
        first_event_timeout=stall * 4,
    )
    try:
        with pytest.raises(ProviderError) as exc:
            await collect(model)
        assert exc.value.code == "timeout"
        # 关键：错误必须点名是空闲超时，而不是被 httpx 抢先后的 "network timeout"
        assert "idle" in str(exc.value)
        assert "ttft" not in str(exc.value)
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize(
    ("idle_timeout", "first_event_timeout"),
    [
        (120.0, 30.0),
        (300.0, 30.0),
        (30.0, 30.0),
        (600.0, 90.0),
        (0.5, 0.5),
    ],
)
@pytest.mark.asyncio
async def test_the_clients_read_timeout_never_preempts_the_outer_ones(
    idle_timeout: float, first_event_timeout: float
) -> None:
    """httpx 自己的 read 超时必须**大于**外层 wait_for 用的那两个。

    否则它先炸，`provider_idle_timeout` / `provider_ttft_timeout` 永远没机会触发，
    而被映射成的 "network timeout" 又与网络抖动无从区分（实测：真实 TCP 上沉默 45 秒，
    read=30 在 30.0s 抛 ReadTimeout）。这是唯一能在不等待 30 秒的前提下钉住它的断言，
    所以这里直接查配置值而不是等超时。
    """

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=httpx.AsyncByteStream())

    model = make_model(handler, idle_timeout=idle_timeout, first_event_timeout=first_event_timeout)
    try:
        read = model._client.timeout.read
        assert read is not None
        assert read > idle_timeout, f"read={read} 会在 idle={idle_timeout} 之前触发"
        assert read > first_event_timeout, f"read={read} 会在 ttft={first_event_timeout} 之前触发"
    finally:
        await model.aclose()


async def test_an_unbounded_single_line_is_rejected_without_buffering_it_all() -> None:
    """一个没有换行的巨行必须在被完整拼出来之前就触发上限。

    `response.aiter_lines()` 会把一整行完整拼出来才返回，于是这道闸设在看见它之后
    就没有意义——峰值内存已经吃满了。这里用一个真的服务端持续灌字节但从不发换行，
    断言它被 `protocol` 错误挡下，而不是把 4 MiB（或更多）都收进来。
    """
    from haven.adapters.providers.openai_compatible import MAX_RESPONSE_BYTES

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, ConnectionError):
            return
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"content-type: text/event-stream\r\n"
            b"transfer-encoding: chunked\r\n\r\n"
        )
        await writer.drain()
        # 只发 data: 和一个超长的payload，始终不发换行
        filler = b"x" * (MAX_RESPONSE_BYTES // 2)
        try:
            for _ in range(8):
                writer.write(f"{len(filler):x}".encode() + b"\r\n" + filler + b"\r\n")
                await writer.drain()
            writer.write(b"0\r\n\r\n")
            await writer.drain()
        except (ConnectionError, BrokenPipeError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    model = OpenAICompatibleModel(
        base_url=f"http://127.0.0.1:{port}/v1",
        api_key=API_KEY,
        model="gpt-test",
        idle_timeout=5.0,
        first_event_timeout=5.0,
    )
    try:
        with pytest.raises(ProviderError) as exc:
            await collect(model)
        assert exc.value.code == "protocol"
        assert "size limit" in str(exc.value)
    finally:
        server.close()
        await server.wait_closed()


async def test_bounded_lines_raises_before_materializing_an_oversized_line() -> None:
    """`_bounded_lines` 必须在拼出完整的一行之前就抛错。

    `response.aiter_lines()` 会把一整行**完整拼出来**才返回，于是峰值内存是无界的，
    总量上限也就形同虚设。这里直接喂一个永不加换行的字节流，断言它在交付任何一行
    之前就报错——端到端测试观察不到这一点（两边的错误信息完全一样），只有这里能。
    """
    from haven.adapters.providers.openai_compatible import MAX_RESPONSE_BYTES, _bounded_lines

    class EndlessLine:
        """一个只说 `x`、永远不发换行的响应。

        带一个 `aiter_lines`，行为与 httpx 一致（把整行拼出来才返回），这样若产品代码
        退回用 `aiter_lines()`，失败会落在下面的断言上，而不是 AttributeError。
        """

        async def aiter_bytes(self) -> AsyncIterator[bytes]:
            chunk = b"x" * (MAX_RESPONSE_BYTES // 4)
            for _ in range(8):
                yield chunk

        async def aiter_lines(self) -> AsyncIterator[str]:
            buffer = b""
            async for chunk in self.aiter_bytes():
                buffer += chunk
                *complete, buffer = buffer.split(b"\n")
                for raw in complete:
                    yield raw.decode("utf-8", errors="replace")
            if buffer:
                yield buffer.decode("utf-8", errors="replace")

    produced: list[str] = []
    with pytest.raises(ProviderError) as exc:
        async for line in _bounded_lines(EndlessLine(), MAX_RESPONSE_BYTES):  # type: ignore[arg-type]
            produced.append(line)

    assert exc.value.code == "protocol"
    assert "size limit" in str(exc.value)
    # 关键：抛错之前一行都没交付过。换成 aiter_lines() 时，它会把整行拼出来才返回，
    # 于是这里会先拿到一个 4 MiB 的字符串。
    assert produced == []


async def test_oversize_bytes_are_rejected_immediately_not_after_the_line_ends() -> None:
    """越界必须**立刻**报出来，而不是等这一行结束。

    这是两种实现唯一可观察的差异：服务端发够越界字节后就此挂住，
    `aiter_lines()` 会一直等那个永远不来的换行，最后走到空闲超时；按块计量的实现则在
    越线那一刻就报 `protocol`。若这条测试变红，说明又回到了"先拼完整行再检查"。
    """
    from haven.adapters.providers.openai_compatible import MAX_RESPONSE_BYTES

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, ConnectionError):
            return
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"content-type: text/event-stream\r\n"
            b"transfer-encoding: chunked\r\n\r\n"
        )
        await writer.drain()
        # 发够越界的字节，且始终不发换行，然后挂住
        filler = b"x" * (MAX_RESPONSE_BYTES // 2)
        try:
            for _ in range(4):
                writer.write(f"{len(filler):x}".encode() + b"\r\n" + filler + b"\r\n")
                await writer.drain()
            await asyncio.sleep(30)
        except (ConnectionError, BrokenPipeError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    model = OpenAICompatibleModel(
        base_url=f"http://127.0.0.1:{port}/v1",
        api_key=API_KEY,
        model="gpt-test",
        # 空闲超时故意设得比 30 秒短：若实现退回 aiter_lines()，先触发的会是它
        idle_timeout=5.0,
        first_event_timeout=5.0,
    )
    try:
        with pytest.raises(ProviderError) as exc:
            await collect(model)
        assert exc.value.code == "protocol", "必须是越界错误，不是超时"
        assert "size limit" in str(exc.value)
    finally:
        server.close()
        await server.wait_closed()
