"""OpenAI 兼容聊天补全适配器（流式）。

提供商线协议字段只存在于本模块中。适配器将 SSE 数据块映射为与提供商无关的
ModelEvents，强制执行首事件和总超时，限制响应大小，并将失败转换为稳定的
ProviderError 代码。API 密钥从配置中读取，绝不会出现在错误或追踪记录中。
"""

from __future__ import annotations

import asyncio
import json
import ssl
from collections.abc import AsyncIterator
from typing import Any

import httpx

from haven.adapters.providers.openai_wire import (
    ToolCallCollector as _ToolCallCollector,
)
from haven.adapters.providers.openai_wire import (
    inline_refs as _inline_refs,
)
from haven.adapters.providers.openai_wire import (
    is_context_overflow as _is_context_overflow,
)
from haven.adapters.providers.openai_wire import (
    map_finish_reason as _map_finish_reason,
)
from haven.adapters.providers.openai_wire import (
    parse_retry_after as _parse_retry_after,
)
from haven.adapters.providers.openai_wire import (
    parse_sse_line as _parse_sse_line,
)
from haven.adapters.providers.openai_wire import (
    sanitize_history as _sanitize_history,
)
from haven.adapters.providers.openai_wire import (
    to_wire_message as _to_wire_message,
)
from haven.adapters.providers.openai_wire import (
    to_wire_tool_name as _to_wire_tool_name,
)
from haven.contracts.model import (
    ModelEvent,
    ModelRequest,
    ReasoningDelta,
    StreamFinished,
    TextDelta,
    ToolCallReady,
    Usage,
    UsageReport,
)
from haven.ports.model import ProviderError

MAX_RESPONSE_BYTES = 4 * 1024 * 1024


async def _read_bounded_body(response: httpx.Response, limit: int) -> bytes:
    """读取有界错误体，避免提供商用非 200 响应迫使客户端无界分配内存。"""
    body = bytearray()
    async for chunk in response.aiter_bytes():
        remaining = limit - len(body)
        if remaining <= 0:
            break
        body.extend(chunk[:remaining])
        if len(body) >= limit:
            break
    return bytes(body)


class OpenAICompatibleModel:
    """通过 OpenAI 兼容的 /chat/completions API 实现 ModelPort。

    一次请求的调用路径：

        generate_stream -> _stream_with_reasoning_retry   （精确重试一次）
          -> _stream          SSE 循环，包含首事件和总截止时间
             -> _build_payload   清理后的历史记录 -> 线协议 JSON
             -> _ToolCallCollector   累积流式工具调用增量

    产生与提供商无关的 ModelEvents（TextDelta / ReasoningDelta /
    ToolCallReady / StreamFinished）。可重试的传输失败会抛出
    ProviderError("network"|"timeout"...)，由运行循环重试，而不会在此适配器
    内部静默处理（部分流必须显式暴露）。
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
        # 限制流式事件之间的间隔，每个事件到达时重置——不是总截止时间。推理模型
        # （DeepSeek `high`/`max`）可能连续数分钟稳定输出；只有真正卡住才应超时。
        # 整个运行仍由 RunService 的墙上时钟预算限制。
        self._idle_timeout = idle_timeout
        # DeepSeek V4 会拒绝未重放 reasoning_content 的工具调用 assistant 轮次
        # （ADR 0014）；在 bootstrap 时按模型 profile 设置。
        self._requires_tool_call_reasoning = requires_tool_call_reasoning
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(connect=connect_timeout, read=30.0, write=30.0, pool=10.0),
            transport=transport,
        )

    @property
    def model_name(self) -> str:
        """返回配置的提供商模型名称。"""
        return self._model

    async def aclose(self) -> None:
        """关闭底层 HTTP 客户端，释放连接池。"""
        await self._client.aclose()

    def generate_stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        """启动一次流式请求；具体网络 I/O 在异步迭代时执行。"""
        return self._stream_with_reasoning_retry(request)

    async def _stream_with_reasoning_retry(
        self, request: ModelRequest
    ) -> AsyncIterator[ModelEvent]:
        """自动修复推理重放本来就是为了避免的那一种 400 错误。

        如果 profile 标志关闭，但提供商实际要求在工具调用轮次中重放 reasoning，
        第一个请求会在产生任何事件之前返回 400。此时可以精确重试——因为尚未
        发出任何内容——因此会强制开启重放并重新发出一次请求。其他原因导致的
        400，或流已经开始后出现的 400，都不会在这里重试（运行循环的有界重试
        仍会处理真正的瞬态失败）。
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
                not yielded_any  # 流式输出中途重试会重复发送增量
                and not force_reasoning
                and exc.code == "protocol"
                and "reasoning" in str(exc).lower()
                and any(m.role == "assistant" and m.tool_calls for m in request.messages)
            )
            if not retryable_reasoning:
                raise
        # 重试路径放在 except 块之外，使其自身的错误能清楚地暴露。
        async for event in self._stream(request, replay_reasoning=True):
            yield event

    async def _stream(
        self, request: ModelRequest, *, replay_reasoning: bool
    ) -> AsyncIterator[ModelEvent]:
        payload = self._build_payload(request, replay_reasoning=replay_reasoning)
        # 本次请求的精确反向映射，因此绝不会猜测线协议名称。
        from_wire = {_to_wire_tool_name(t.name): t.name for t in request.tools}

        try:
            async with self._client.stream("POST", "/chat/completions", json=payload) as response:
                if response.status_code != 200:
                    body = await _read_bounded_body(response, 4096)
                    raise self._map_status(response.status_code, body, response.headers)

                collector = _ToolCallCollector()
                finish_reason: str = "stop"
                usage: Usage | None = None
                received_bytes = 0
                first = True

                lines = response.aiter_lines().__aiter__()
                while True:
                    # 第一次读取限制首 token 延迟；之后每次读取都限制空闲间隔，并在数据
                    # 到达时重置，因此持续时间很长但稳定的生成不会被提前截断。
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

                    # aiter_lines 已移除换行符；按 UTF-8 字节而非 Python 字符计数，
                    # 并保守计入行分隔符，使非 ASCII 响应不能绕过线协议上限。
                    received_bytes += len(line.encode("utf-8")) + 2
                    if received_bytes > MAX_RESPONSE_BYTES:
                        raise ProviderError("protocol", "response exceeded size limit")

                    data = _parse_sse_line(line)
                    if data is None:
                        continue
                    if data == "[DONE]":
                        break

                    try:
                        decoded: object = json.loads(data)
                    except json.JSONDecodeError as exc:
                        raise ProviderError("protocol", f"malformed stream chunk: {exc}") from exc
                    try:
                        if not isinstance(decoded, dict):
                            raise TypeError("chunk is not an object")
                        chunk: dict[str, Any] = decoded
                        raw_usage = chunk.get("usage")
                        if raw_usage is not None:
                            if not isinstance(raw_usage, dict):
                                raise TypeError("usage is not an object")
                            details = raw_usage.get("completion_tokens_details") or {}
                            prompt_details = raw_usage.get("prompt_tokens_details") or {}
                            if not isinstance(details, dict) or not isinstance(
                                prompt_details, dict
                            ):
                                raise TypeError("usage details are not objects")
                            # OpenAI 在 prompt_tokens_details 下报告缓存命中；DeepSeek
                            # 使用顶层字段 prompt_cache_hit_tokens。
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
                        choices = chunk.get("choices", [])
                        if not isinstance(choices, list):
                            raise TypeError("choices is not a list")
                        for choice in choices:
                            if not isinstance(choice, dict):
                                raise TypeError("choice is not an object")
                            if reason := choice.get("finish_reason"):
                                finish_reason = str(reason)
                            delta = choice.get("delta") or {}
                            if not isinstance(delta, dict):
                                raise TypeError("delta is not an object")
                            if content := delta.get("content"):
                                yield TextDelta(text=str(content))
                            # 推理模型会在答案前将思考过程放在单独字段中流式传出。
                            if reasoning := delta.get("reasoning_content"):
                                yield ReasoningDelta(text=str(reasoning))
                            tool_calls = delta.get("tool_calls") or []
                            if not isinstance(tool_calls, list):
                                raise TypeError("tool_calls is not a list")
                            for tool_call in tool_calls:
                                if not isinstance(tool_call, dict):
                                    raise TypeError("tool call is not an object")
                                collector.feed(tool_call)
                    except (TypeError, ValueError) as exc:
                        raise ProviderError("protocol", "malformed stream fields") from exc

                for call in collector.completed(from_wire):
                    yield ToolCallReady(call=call)
                if usage is not None:
                    yield UsageReport(usage=usage)
                yield StreamFinished(finish_reason=_map_finish_reason(finish_reason))
        except httpx.TimeoutException as exc:
            raise ProviderError("timeout", "network timeout", retryable=True) from exc
        except httpx.HTTPError as exc:
            # 连接被丢弃、重置或由服务器关闭属于临时故障，而模型调用没有副作用，
            # 因此 RunService 可以安全重试。协议或 URL 配置错误每次都会以相同方式
            # 失败，重试只会更慢地消耗预算。
            transient = isinstance(exc, httpx.NetworkError | httpx.RemoteProtocolError)
            raise ProviderError(
                "network", f"transport error: {type(exc).__name__}", retryable=transient
            ) from exc
        except OSError as exc:
            # 并非每个 I/O 故障都会包装成 httpx 错误：TLS 记录层故障会表现为
            # ssl.SSLError（一个 OSError），如果不包装就会绕过所有 `except ProviderError`
            # 并终止运行。证书拒绝是重试无法修复的情况。这里特意不使用
            # `except Exception`，这样我们自己的解析 bug 仍会以其本来面目暴露。
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
            # 绝不回显认证失败响应体。
            return ProviderError("auth", "provider rejected credentials")
        if status == 402:
            # 账户余额不足时，DeepSeek 返回 402 “Insufficient Balance”。重试无法
            # 增加余额，因此这是终态错误并使用独立代码——用户看到 `quota` 就知道
            # 需要充值，而不是将通用 `protocol` 类别误认为 Haven 的 bug。
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
        # 这里的 4xx 几乎总是我们的请求格式错误。响应体会说明问题且不含凭据，
        # 因此暴露一个有界片段可以将不透明的失败变成可修复的问题。
        detail = body.decode("utf-8", errors="replace").strip()[:300]
        if status == 400 and _is_context_overflow(detail):
            # prompt 超过了模型窗口。与其他 400 不同，这是可恢复的：RunService
            # 可以强制压缩并重试，因此使用独立代码，而不是终态的 `protocol` 类别。
            return ProviderError("context_overflow", "provider context window exceeded")
        return ProviderError(
            "protocol",
            f"unexpected provider status ({status}): {detail}"
            if detail
            else f"unexpected provider status ({status})",
        )
