"""Deterministic compaction: dropped tool outputs become recorded facts.

When the transcript outgrows the context budget, the oldest tool outputs are
removed and replaced by one program-assembled digest of what they contained.
The model is never asked to summarize — a summary it wrote could invent facts
that later turns treat as established, including permission-shaped ones.

The digest is derived from the dropped messages themselves rather than from
live run state. That keeps this a pure function, and it keeps the digest
byte-identical between compaction events, so it does not move the cacheable
prefix on every turn (ADR 0008).
"""

from __future__ import annotations

import json
import re
from typing import Any

from haven.contracts.model import ModelMessage

#: Only structured metadata goes in, so the digest can honestly be labelled
#: trusted. File content and model prose must never reach it.
DIGEST_HEADER = (
    "Earlier steps, condensed by the program (originals dropped to fit the "
    "context budget; these are recorded facts, not a summary):"
)

_TOOL_ATTR = re.compile(r'<tool_output tool="([^"]+)">')
_MAX_ITEMS_PER_LINE = 12
_DIGEST_PREFIX_CHARS = 8


def message_chars(message: ModelMessage) -> int:
    """Wire-size estimate for the context budget.

    Content is not the only thing sent: replayed reasoning (ADR 0014) and
    tool-call arguments both cost tokens, so counting only `content` would
    undercount input and let the transcript overrun the budget silently.
    """
    size = len(message.content) + len(message.provider_reasoning)
    for call in message.tool_calls:
        size += len(call.arguments_json) + len(call.tool_name)
    return size


def _tool_name(message: ModelMessage) -> str:
    match = _TOOL_ATTR.search(message.content)
    return match.group(1) if match else "unknown"


def _payload(message: ModelMessage) -> dict[str, Any]:
    """The JSON body of a tool output, or an empty mapping.

    A malformed entry degrades to no facts rather than raising: one bad message
    must never be able to abort a run.
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
    """Condense dropped messages into recorded facts. Pure and total."""
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
    """Fit `messages` into `limit` characters by dropping oldest tool outputs.

    Returns the surviving messages, the digest of what was dropped, and the
    index in the surviving list where the digest belongs — or -1 when nothing
    was dropped.

    A tool result is dropped together with the assistant turn that requested it,
    as a whole unit, so a kept assistant message never carries a tool call whose
    result was dropped — an orphaned tool call that OpenAI and DeepSeek reject.
    Assistant turns without tool calls (narrative) and user turns (gate feedback)
    are never dropped. The two most recent tool outputs are always kept whole,
    protected by role rather than position because the volatile tail sits after
    the transcript.
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
    # The digest takes the place of the first message it replaces. Everything
    # before that index is kept (the first dropped index is, by definition, the
    # smallest of a dropped unit), so its position in `kept` is exactly that
    # index.
    position = min(dropped_indices)
    return kept, digest, position


def _droppable_units(messages: list[ModelMessage]) -> list[list[int]]:
    """Group each assistant-with-tool-calls with its following tool results.

    A unit is dropped or kept as a whole, which is what keeps tool calls and
    their results paired. Standalone tool messages (none precede them) are their
    own droppable units; user and plain-assistant turns are not droppable and
    form no unit.
    """
    units: list[list[int]] = []
    index = 0
    count = len(messages)
    while index < count:
        message = messages[index]
        if message.role == "assistant" and message.tool_calls:
            group = [index]
            follow = index + 1
            while follow < count and messages[follow].role == "tool":
                group.append(follow)
                follow += 1
            units.append(group)
            index = follow
        elif message.role == "tool":
            units.append([index])
            index += 1
        else:
            index += 1
    return units


def _protected_units(messages: list[ModelMessage], units: list[list[int]]) -> set[int]:
    """The trailing units covering the two most recent tool outputs."""
    protected: set[int] = set()
    tools_protected = 0
    for unit_index in range(len(units) - 1, -1, -1):
        if tools_protected >= 2:
            break
        protected.add(unit_index)
        tools_protected += sum(1 for i in units[unit_index] if messages[i].role == "tool")
    return protected
