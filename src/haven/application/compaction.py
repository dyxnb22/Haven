"""确定性压缩：被丢弃的工具输出会变成记录事实。

当 transcript 超出上下文预算时，最旧的工具输出会被移除，并替换为一份由程序根据其
内容组装的摘要。绝不会要求模型进行总结——模型写出的摘要可能会凭空编造事实，后续
轮次却把这些事实当成既定内容，包括形似权限的事实。

摘要直接由被丢弃的消息推导，而不是由运行时状态推导。这样它保持为纯函数，并且在多次
压缩事件之间保持逐字节相同，不会在每一轮移动可缓存前缀（ADR 0008）。
"""

from __future__ import annotations

import json
import re
from typing import Any

from haven.contracts.model import ModelMessage

#: 这里只放入结构化元数据，因此可以诚实地将该摘要标记为 trusted。
#: 文件内容和模型 prose 绝不能进入其中。
DIGEST_HEADER = (
    "Earlier steps, condensed by the program (originals dropped to fit the "
    "context budget; these are recorded facts, not a summary):"
)

_TOOL_ATTR = re.compile(r'<tool_output tool="([^"]+)">')
_MAX_ITEMS_PER_LINE = 12
_DIGEST_PREFIX_CHARS = 8


def message_chars(message: ModelMessage) -> int:
    """估算消息在线协议中的大小，用于上下文预算。

    发送的内容不只有 content：重放的推理（ADR 0014）和工具调用参数都会消耗 token，
    因此只统计 `content` 会低估输入量，让 transcript 悄悄超过预算。
    """
    size = len(message.content) + len(message.provider_reasoning)
    for call in message.tool_calls:
        size += len(call.arguments_json) + len(call.tool_name)
    return size


def _tool_name(message: ModelMessage) -> str:
    match = _TOOL_ATTR.search(message.content)
    return match.group(1) if match else "unknown"


def _payload(message: ModelMessage) -> dict[str, Any]:
    """返回工具输出的 JSON 主体，无法获取时返回空映射。

    格式错误的条目会退化为没有事实，而不是抛出异常：单条坏消息绝不能中止运行。
    """
    body = message.content
    start = body.find(">")
    end = body.rfind("</tool_output>")
    if start != -1 and end > start:
        body = body[start + 1 : end]
    try:
        parsed = json.loads(body.strip())
    except (ValueError, TypeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    result = parsed.get("result")
    return result if isinstance(result, dict) else {}


def _append_unique(items: list[str], value: str) -> None:
    if value not in items:
        items.append(value)


def _render_line(label: str, items: list[str]) -> str | None:
    if not items:
        return None
    shown = items[:_MAX_ITEMS_PER_LINE]
    suffix = f", +{len(items) - len(shown)} more" if len(items) > len(shown) else ""
    return f"- {label}: {', '.join(shown)}{suffix}"


def build_run_digest(dropped: list[ModelMessage]) -> str:
    """将被丢弃的消息压缩为记录事实；这是纯函数，并且对所有输入都有定义。"""
    reads: list[str] = []
    edits: list[str] = []
    checks: list[str] = []
    others: list[str] = []
    other_count = 0

    for message in dropped:
        if message.role != "tool":
            continue
        tool = _tool_name(message)
        result = _payload(message)
        path = result.get("path")
        if tool in ("repo.read", "repo.list", "repo.search") and isinstance(path, str):
            digest = result.get("digest")
            marker = (
                f"{path} ({digest[:_DIGEST_PREFIX_CHARS]})"
                if isinstance(digest, str) and digest
                else path
            )
            _append_unique(reads, marker)
            continue
        if tool in ("repo.edit", "repo.create") and isinstance(path, str):
            postimage = result.get("postimage_digest")
            marker = (
                f"{path} -> {postimage[:_DIGEST_PREFIX_CHARS]}"
                if isinstance(postimage, str) and postimage
                else path
            )
            _append_unique(edits, marker)
            continue
        if tool == "repo.check":
            recipe = result.get("recipe_id")
            exit_code = result.get("exit_code")
            if isinstance(recipe, str) and isinstance(exit_code, int):
                _append_unique(checks, f"{recipe} exit {exit_code}")
                continue
        other_count += 1
        _append_unique(others, tool)

    lines = [
        line
        for line in (
            _render_line("read", reads),
            _render_line("edited", edits),
            _render_line("checks", checks),
        )
        if line is not None
    ]
    if other_count:
        lines.append(f"- other tool calls: {other_count} ({', '.join(others)})")
    if not lines:
        return ""
    return "\n".join([DIGEST_HEADER, *lines])


def summarize_dropped(
    messages: list[ModelMessage], limit: int
) -> tuple[list[ModelMessage], str, int]:
    """通过丢弃最旧的工具输出来将 `messages` 放入 `limit` 个字符以内。

    返回保留的消息、被丢弃内容的摘要，以及摘要在保留列表中的插入位置；如果没有丢弃
    内容，则位置为 -1。

    工具结果会与发起该调用的 assistant 轮次作为一个整体一起丢弃，因此保留的 assistant
    消息不会携带一个结果已被丢弃的工具调用——OpenAI 和 DeepSeek 会拒绝这种孤立工具
    调用。没有工具调用的 assistant 轮次（叙述）和 user 轮次（门禁反馈）永远不会丢弃。
    最近的两条工具输出始终整体保留；由于易变尾部位于 transcript 之后，这里按角色而
    不是按位置进行保护。
    """
    total = sum(message_chars(message) for message in messages)
    if total <= limit:
        return list(messages), "", -1

    units = _droppable_units(messages)
    protected = _protected_units(messages, units)

    dropped_indices: set[int] = set()
    for unit_index, unit in enumerate(units):
        if total <= limit:
            break
        if unit_index in protected:
            continue
        for index in unit:
            total -= message_chars(messages[index])
        dropped_indices.update(unit)

    if not dropped_indices:
        return list(messages), "", -1

    dropped = [messages[i] for i in sorted(dropped_indices)]
    kept = [message for i, message in enumerate(messages) if i not in dropped_indices]
    digest = build_run_digest(dropped)
    if not digest:
        return kept, "", -1
    # 摘要会取代它所替换的第一条消息。该索引之前的所有消息都会保留（按定义，
    # 被丢弃单元的第一个索引就是其中最小的索引），因此它在 `kept` 中的位置
    # 恰好就是该索引。
    position = min(dropped_indices)
    return kept, digest, position


def enforce_hard_limit(messages: list[ModelMessage], limit: int) -> list[ModelMessage]:
    """无论历史包含什么，都保证组装后的内容符合 `limit`。

    `summarize_dropped` 只会移除“可丢弃”的工具单元，因此以 user 轮次（门禁反馈）、
    叙述性 assistant 轮次或摘要为主的 transcript 仍可能超过预算——之前的截断是软限制。
    这里是最后的后备保护：从最旧到最新整体丢弃消息，直到总量符合预算；如果连最近的
    单条消息也超出预算，则硬截断该消息的 content。不会整体丢弃最后一条消息，因此请求
    不会为空。

    通常这是空操作——`summarize_dropped` 已经能处理常见情况；这里只在不可丢弃内容
    本身溢出时触发，必须以安全方式失败（发送截断后的请求），不能超预算发送后收到 400。
    """
    if limit < 0:
        raise ValueError("limit must be non-negative")
    total = sum(message_chars(message) for message in messages)
    if total <= limit or not messages:
        return messages
    units = _message_units(messages)

    # 保留最新消息单元以及最新用户意图所在单元；其余单元从旧到新淘汰。
    protected = {len(units) - 1}
    for index in range(len(units) - 1, -1, -1):
        if any(message.role == "user" for message in units[index]):
            protected.add(index)
            break
    kept = list(enumerate(units))
    while sum(message_chars(message) for _, unit in kept for message in unit) > limit:
        candidate = next(
            (i for i, (unit_index, _) in enumerate(kept) if unit_index not in protected), None
        )
        if candidate is None:
            break
        kept.pop(candidate)

    total = sum(message_chars(message) for _, unit in kept for message in unit)
    if total <= limit:
        return [message for _, unit in kept for message in unit]

    # 极端压力下按保护单元分配空间。工具调用与结果要么一起保留，要么被一个诚实
    # 的省略标记替代，绝不会制造孤立的 tool 消息。
    fitted: list[ModelMessage] = []
    remaining = limit
    for position, (_, unit) in enumerate(kept):
        units_left = len(kept) - position
        allocation = remaining if units_left == 1 else remaining // units_left
        compacted = _fit_unit(unit, allocation)
        fitted.extend(compacted)
        used = sum(message_chars(message) for message in compacted)
        remaining = max(0, remaining - used)
    return fitted


def _message_units(messages: list[ModelMessage]) -> list[list[ModelMessage]]:
    """把消息切成不能被硬限制拆开的 provider 协议单元。"""
    return [[messages[index] for index in indices] for indices in _all_units(messages)]


def _all_units(messages: list[ModelMessage]) -> list[list[int]]:
    units: list[list[int]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        if message.role == "assistant" and message.tool_calls:
            group = [index]
            index += 1
            while index < len(messages) and messages[index].role == "tool":
                group.append(index)
                index += 1
            units.append(group)
        else:
            units.append([index])
            index += 1
    return units


def _fit_unit(unit: list[ModelMessage], limit: int) -> list[ModelMessage]:
    if sum(message_chars(message) for message in unit) <= limit:
        return unit
    if len(unit) == 1:
        return [_fit_message(unit[0], limit)]

    fitted: list[ModelMessage] = []
    remaining = limit
    for position, message in enumerate(unit):
        messages_left = len(unit) - position
        allocation = remaining if messages_left == 1 else remaining // messages_left
        compacted = _fit_message(message, allocation)
        fitted.append(compacted)
        remaining = max(0, remaining - message_chars(compacted))
    protocol_intact = (
        fitted[0].role == "assistant"
        and bool(fitted[0].tool_calls)
        and all(message.role == "tool" for message in fitted[1:])
    )
    if protocol_intact and sum(message_chars(message) for message in fitted) <= limit:
        return fitted
    return [_omission_message(limit)]


def _fit_message(message: ModelMessage, limit: int) -> ModelMessage:
    if message_chars(message) <= limit:
        return message

    # 先保留协议元数据，只收缩可见内容。
    metadata_size = message_chars(message.model_copy(update={"content": ""}))
    if metadata_size <= limit:
        return message.model_copy(
            update={"content": _clip_text(message.content, limit - metadata_size)}
        )

    # 如果超限来自 reasoning 或工具参数，将参数收缩为仍然有效的 JSON，并移除
    # 不透明 reasoning；调用 ID 和工具名仍保持配对。
    calls = tuple(call.model_copy(update={"arguments_json": "{}"}) for call in message.tool_calls)
    minimized = message.model_copy(
        update={"content": "", "provider_reasoning": "", "tool_calls": calls}
    )
    fixed = message_chars(minimized)
    if fixed > limit:
        return _omission_message(limit)
    return minimized.model_copy(update={"content": _clip_text(message.content, limit - fixed)})


def _clip_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    marker = "\n...[truncated to fit the context budget]"
    if limit <= len(marker):
        return marker[:limit]
    return value[: limit - len(marker)] + marker


def _omission_message(limit: int) -> ModelMessage:
    marker = "[recent provider turn omitted to fit context]"
    return ModelMessage(role="user", content=marker[:limit])


def _droppable_units(messages: list[ModelMessage]) -> list[list[int]]:
    """将每个带工具调用的 assistant 轮次与其后续工具结果分组。

    一个单元要么整体丢弃，要么整体保留，这样才能让工具调用和结果保持配对。没有前置
    assistant 轮次的独立工具消息属于自己的可丢弃单元；user 轮次和普通 assistant 轮次
    不可丢弃，也不会构成单元。
    """
    return [
        unit
        for unit in _all_units(messages)
        if messages[unit[0]].role == "tool"
        or (messages[unit[0]].role == "assistant" and messages[unit[0]].tool_calls)
    ]


def _protected_units(messages: list[ModelMessage], units: list[list[int]]) -> set[int]:
    """覆盖最近两条工具输出的尾部单元。"""
    protected: set[int] = set()
    tools_protected = 0
    for unit_index in range(len(units) - 1, -1, -1):
        if tools_protected >= 2:
            break
        protected.add(unit_index)
        tools_protected += sum(1 for i in units[unit_index] if messages[i].role == "tool")
    return protected
