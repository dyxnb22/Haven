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

    Only tool outputs are droppable: assistant turns are the model's own record
    of what it was doing, and user messages carry gate feedback. The two most
    recent tool outputs are always kept whole, protected by role rather than by
    position because the volatile tail now sits after the transcript.
    """
    total = sum(len(message.content) for message in messages)
    if total <= limit:
        return list(messages), "", -1

    tool_indices = [i for i, message in enumerate(messages) if message.role == "tool"]
    protected = set(tool_indices[-2:])

    dropped_indices: set[int] = set()
    for index in tool_indices:
        if total <= limit or index in protected:
            continue
        total -= len(messages[index].content)
        dropped_indices.add(index)

    if not dropped_indices:
        return list(messages), "", -1

    dropped = [messages[i] for i in sorted(dropped_indices)]
    kept = [message for i, message in enumerate(messages) if i not in dropped_indices]
    digest = build_run_digest(dropped)
    if not digest:
        return kept, "", -1
    # The digest takes the place of the first message it replaces, so
    # everything before that point keeps its bytes.
    position = min(dropped_indices) - sum(1 for i in dropped_indices if i < min(dropped_indices))
    return kept, digest, position
