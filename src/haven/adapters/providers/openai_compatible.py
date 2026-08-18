"""OpenAI 兼容聊天补全适配器（流式）。

提供商线协议字段只存在于本模块中。适配器将 SSE 数据块映射为与提供商无关的
ModelEvents，强制执行首事件和总超时，限制响应大小，并将失败转换为稳定的
ProviderError 代码。API 密钥从配置中读取，绝不会出现在错误或追踪记录中。
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
        return self._model

    async def aclose(self) -> None:
        await self._client.aclose()

    def generate_stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
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
                    body = await response.aread()
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
                        # OpenAI 在 prompt_tokens_details 下报告缓存命中；DeepSeek 使用顶层的
                        # 字段名：prompt_cache_hit_tokens。
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
                        # 推理模型会在任何答案内容出现前，将思考过程放在单独字段中流式传出。
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

    # -- 线协议转换辅助函数 ------------------------------------------------------
    # 以下内容负责在 Haven 与提供商无关的契约（contracts/model.py）和 OpenAI
    # chat-completions 线协议格式之间转换。提供商的特殊行为在这里吸收，
    # 上游代码无需感知。


#: OpenAI 兼容 API 将函数名限制为 ^[a-zA-Z0-9_-]+$，因此会拒绝 Haven 带命名
#: 空间的 `repo.read`。点号是核心命名选择，所以替换只存在于线协议边界，
#: 绝不向内部泄漏。
_WIRE_NAME_SEPARATOR = "__"


def _to_wire_tool_name(name: str) -> str:
    return name.replace(".", _WIRE_NAME_SEPARATOR)


def _inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """将 Pydantic 的 `$defs`/`$ref` 解析为自包含的模式。

    嵌套模型会生成 `$ref`，而几个 OpenAI 兼容提供商会在函数参数中拒绝它。本处
    模式规模较小且不存在递归，因此内联是安全的，并能让面向模型的契约保持为
    字面结构。
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


#: 在 OpenAI 兼容提供商中识别“prompt 过长”400 的子字符串。DeepSeek 和 OpenAI
#: 将其表述为 “maximum context length”；规范的 OpenAI 错误代码是
#: `context_length_exceeded`。按消息匹配使逻辑停留在线协议边界，不会向内部
#: 泄漏提供商字符串——核心只会看到 `context_overflow` 代码。
_CONTEXT_OVERFLOW_MARKERS = ("maximum context length", "context_length_exceeded", "context length")


def _is_context_overflow(detail: str) -> bool:
    lowered = detail.lower()
    return any(marker in lowered for marker in _CONTEXT_OVERFLOW_MARKERS)


def _parse_retry_after(value: str | None) -> float | None:
    """解析 `Retry-After` 标头指定的等待秒数；无法解析时返回 None。

    标头要么是非负整数秒数，要么是 HTTP 日期；实际环境中两种形式都存在。过去的
    日期或无法解析的日期会返回 None，而不是负数或虚假的延迟，因此格式错误的
    标头最多让程序回退到重试循环自身的退避时间，绝不会缩短它。
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
    """修复严格提供商（DeepSeek V4）会返回 400 的结构缺陷。

    每个 OpenAI 兼容提供商都执行相同的工具调用/工具结果契约：assistant 轮次中的
    每个 tool_calls 必须恰好由其后的一条 tool 消息回答，而 tool 消息必须回答紧邻
    的上一条 assistant 轮次中的某个 tool_call。压缩和崩溃恢复可能在局部破坏这个
    不变量；与其相信所有上游路径都能维护它，不如在每个请求都会经过的线协议边界
    处由适配器确定性地强制执行。

    修复方式（全部是确定性的，不会凭空编造内容）：
    - 丢弃没有回答任何未完成 tool_call 的 tool 消息（孤立结果——发起调用的
      assistant 轮次已被丢弃）；
    - 为历史记录从未回答的每个 tool_call 合成最小错误工具结果（孤立调用——
      结果已被丢弃），因为悬空调用正是触发 400 的原因。
    """
    out: list[ModelMessage] = []
    pending: dict[str, int] = {}  # 未回答的 call_id -> `out` 中的索引

    def flush_pending() -> None:
        # 下一次 assistant/user 轮次开始时，仍未回答的所有 tool_call 都会按
        # 调用顺序追加一个合成结果。
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
            # 否则是孤立结果——静默丢弃。
            continue
        # 新的 assistant 或 user 轮次：先关闭所有未回答的调用。
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
        # DeepSeek V4 要求逐字重放工具调用之前的推理，否则会返回 400；对于
        # 早于捕获机制的历史，空字符串是可接受的回填值（ADR 0014）。
        if replay_reasoning:
            wire["reasoning_content"] = message.provider_reasoning
    if message.role == "tool" and message.tool_call_id:
        wire["tool_call_id"] = message.tool_call_id
    if message.role == "assistant" and message.is_prefix:
        # 原生前缀续写（ADR 0022）：提供商会原地扩展这段部分内容，而不是回复
        # 它。DeepSeek 用 `prefix: true` 标记这类消息（beta prefix-completion 模式）。
        wire["prefix"] = True
    return wire


class _ToolCallCollector:
    """按索引累积流式工具调用增量。"""

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
                    # 未知名称保持不变地传递，使注册表可以用 `unknown_tool` 拒绝它，
                    # 而不是静默改写成一个确实存在的名称。
                    tool_name=from_wire.get(wire_name, wire_name),
                    arguments_json=slot["arguments"] or "{}",
                )
            )
        return calls
