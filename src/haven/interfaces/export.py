"""Run report rendering (redacted)."""

from __future__ import annotations

import os

from haven.contracts.events import (
    ApprovalDecided,
    ApprovalRequested,
    DiffPreview,
    EventEnvelope,
    ModelCompleted,
    Notice,
    PolicyDecided,
    RunFinished,
    StepStarted,
    ToolCompleted,
    ToolProposed,
)
from haven.ports.session import RunRecord

#: Any environment variable whose name ends with one of these is treated as a
#: secret, so a provider-specific name (DEEPSEEK_API_KEY, GROQ_TOKEN, ...) is
#: covered without maintaining a list.
_SECRET_ENV_SUFFIXES = ("_API_KEY", "_KEY", "_TOKEN", "_SECRET", "_PASSWORD")
_MIN_SECRET_LENGTH = 8


def _secret_values() -> list[str]:
    values = [
        value
        for name, value in os.environ.items()
        if name.upper().endswith(_SECRET_ENV_SUFFIXES) and len(value) >= _MIN_SECRET_LENGTH
    ]
    # Longest first, so a secret that contains another is not partially masked.
    return sorted(set(values), key=len, reverse=True)


def _redact(text: str) -> str:
    for value in _secret_values():
        if value in text:
            text = text.replace(value, "[redacted]")
    return text


def render_jsonl(envelopes: list[EventEnvelope]) -> str:
    return "\n".join(_redact(env.model_dump_json()) for env in envelopes) + "\n"


def render_markdown(run: RunRecord, envelopes: list[EventEnvelope]) -> str:
    lines = [
        f"# Haven run report: {run.run_id}",
        "",
        f"- **Goal**: {run.goal}",
        f"- **Status**: {run.status.value} ({run.stop_reason})",
        f"- **Mode**: {run.mode}",
        f"- **Created**: {run.created_at}",
        "",
        "## Timeline",
        "",
    ]
    for env in envelopes:
        event = env.event
        if isinstance(event, StepStarted):
            lines.append(f"**Step {event.step}**")
        elif isinstance(event, ModelCompleted) and event.text:
            lines.append(f"- assistant: {event.text}")
        elif isinstance(event, ToolProposed):
            lines.append(f"- tool `{event.tool_name}` {event.args_summary}")
        elif isinstance(event, PolicyDecided) and event.decision != "allow":
            lines.append(f"- policy **{event.decision}** ({event.reason_code})")
        elif isinstance(event, ApprovalRequested):
            lines.append(f"- approval requested: {event.summary}")
        elif isinstance(event, ApprovalDecided):
            lines.append(f"- approval {event.decision}")
        elif isinstance(event, ToolCompleted):
            state = event.status if not event.error_code else f"error `{event.error_code}`"
            lines.append(f"  - result: {state} ({event.duration_ms}ms)")
        elif isinstance(event, DiffPreview):
            lines.extend(
                [
                    f"- diff: {event.files_changed} file(s) +{event.insertions} -{event.deletions}",
                    "",
                    "```diff",
                    event.preview,
                    "```",
                    "",
                ]
            )
        elif isinstance(event, Notice):
            lines.append(f"- [{event.level}] {event.message}")
        elif isinstance(event, RunFinished):
            lines.extend(
                [
                    "",
                    "## Outcome",
                    "",
                    f"- status: **{event.status}** ({event.stop_reason})",
                    f"- steps: {event.steps}, tool calls: {event.tool_calls}",
                    f"- tokens: {event.input_tokens} in / {event.output_tokens} out"
                    + (" (estimated)" if event.usage_estimated else ""),
                    f"- cost: ${event.cost_usd:.4f}",
                ]
            )
    return _redact("\n".join(lines) + "\n")
