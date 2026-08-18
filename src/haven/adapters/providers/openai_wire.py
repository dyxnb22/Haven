"""OpenAI chat-completions 线协议的纯转换与流式工具调用收集。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from haven.contracts.model import ModelMessage, ToolCallProposal

_WIRE_NAME_SEPARATOR = "__"
_CONTEXT_OVERFLOW_MARKERS = (
    "maximum context length",
    "context_length_exceeded",
    "context length",
)


def to_wire_tool_name(name: str) -> str:
    return name.replace(".", _WIRE_NAME_SEPARATOR)


def inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    defs = schema.get("$defs")
    if not isinstance(defs, dict):
        return schema

    def resolve(node: Any) -> Any:
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
    lowered = detail.lower()
    return any(marker in lowered for marker in _CONTEXT_OVERFLOW_MARKERS)


def parse_retry_after(value: str | None) -> float | None:
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
    line = line.strip()
    if not line or line.startswith(":"):
        return None
    if line.startswith("data:"):
        return line[5:].strip()
    return None


def map_finish_reason(reason: str) -> Any:
    return {"stop": "stop", "tool_calls": "tool_calls", "length": "length"}.get(reason, "stop")


def sanitize_history(messages: Sequence[ModelMessage]) -> list[ModelMessage]:
    out: list[ModelMessage] = []
    pending: dict[str, int] = {}

    def flush_pending() -> None:
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
                    tool_name=from_wire.get(wire_name, wire_name),
                    arguments_json=slot["arguments"] or "{}",
                )
            )
        return calls
