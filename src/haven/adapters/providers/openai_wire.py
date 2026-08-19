"""OpenAI chat-completions 线协议的纯转换与流式工具调用收集。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Literal

from haven.contracts.model import ModelMessage, ToolCallProposal

_WIRE_NAME_SEPARATOR = "__"
_CONTEXT_OVERFLOW_MARKERS = (
    "maximum context length",
    "context_length_exceeded",
    "context length",
)


def to_wire_tool_name(name: str) -> str:
    """将 Haven 工具名中的点号转换为提供商可接受的名称。"""
    return name.replace(".", _WIRE_NAME_SEPARATOR)


def inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """递归展开 JSON Schema 的本地 $defs 引用，供兼容提供商使用。"""
    defs = schema.get("$defs")
    if not isinstance(defs, dict):
        return schema

    def resolve(node: Any) -> Any:
        """递归解析节点中的本地定义引用。"""
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                target = defs.get(ref.rsplit("/", 1)[-1], {})
                return {
                    **resolve(target),
                    **{key: value for key, value in node.items() if key != "$ref"},
                }
            return {key: resolve(value) for key, value in node.items() if key != "$defs"}
        if isinstance(node, list):
            return [resolve(item) for item in node]
        return node

    inlined = resolve(schema)
    return inlined if isinstance(inlined, dict) else schema


def is_context_overflow(detail: str) -> bool:
    """判断提供商错误文本是否表示上下文窗口溢出。"""
    lowered = detail.lower()
    return any(marker in lowered for marker in _CONTEXT_OVERFLOW_MARKERS)


def parse_retry_after(value: str | None) -> float | None:
    """将 Retry-After 秒数或 HTTP 日期转换为剩余等待秒数。"""
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


def parse_sse_line(line: str) -> str | None:
    """提取 SSE data 行；注释、空行和其他字段返回 None。"""
    line = line.strip()
    if not line or line.startswith(":"):
        return None
    if line.startswith("data:"):
        return line[5:].strip()
    return None


def map_finish_reason(reason: str) -> Literal["stop", "tool_calls", "length", "error"]:
    """将提供商停止原因映射为 Haven 的稳定停止分类。"""
    if reason == "tool_calls":
        return "tool_calls"
    if reason == "length":
        return "length"
    if reason == "error":
        return "error"
    return "stop"


def sanitize_history(messages: Sequence[ModelMessage]) -> list[ModelMessage]:
    """补齐缺失工具结果，保证重放历史符合提供商消息配对规则。"""
    out: list[ModelMessage] = []
    pending: dict[str, int] = {}

    def flush_pending() -> None:
        """为尚未收到结果的工具调用补入稳定的错误占位消息。"""
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
            continue
        flush_pending()
        out.append(message)
        if message.role == "assistant":
            for call in message.tool_calls:
                pending[call.call_id] = len(out) - 1
    flush_pending()
    return out


def to_wire_message(message: ModelMessage, *, replay_reasoning: bool = False) -> dict[str, Any]:
    """将提供商无关消息转换为 OpenAI 兼容线协议字典。"""
    wire: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.role == "assistant" and message.tool_calls:
        wire["tool_calls"] = [
            {
                "id": call.call_id,
                "type": "function",
                "function": {
                    "name": to_wire_tool_name(call.tool_name),
                    "arguments": call.arguments_json,
                },
            }
            for call in message.tool_calls
        ]
        if replay_reasoning:
            wire["reasoning_content"] = message.provider_reasoning
    if message.role == "tool" and message.tool_call_id:
        wire["tool_call_id"] = message.tool_call_id
    if message.role == "assistant" and message.is_prefix:
        wire["prefix"] = True
    return wire


class ToolCallCollector:
    def __init__(self) -> None:
        self._calls: dict[int, dict[str, str]] = {}

    def feed(self, delta: dict[str, Any]) -> None:
        """合并一条流式工具调用增量，按 index 保持调用顺序。"""
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
        """将已收集的线协议调用转换为 Haven 工具调用提议。"""
        calls = []
        for index in sorted(self._calls):
            slot = self._calls[index]
            if not slot["name"]:
                continue
            wire_name = slot["name"]
            calls.append(
                ToolCallProposal(
                    call_id=slot["id"] or f"call-{index}",
                    tool_name=from_wire.get(wire_name, wire_name),
                    arguments_json=slot["arguments"] or "{}",
                )
            )
        return calls
