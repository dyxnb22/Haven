"""Run report rendering (redacted)."""

from __future__ import annotations

import os
import re

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

#: Backstop for credentials this process never held, so the env sweep above
#: cannot see them: a key pasted into a goal, one read out of a file by a
#: tool, or another service's token quoted in model output. Matching is by
#: the issuer's own published shape, which keeps false positives low —
#: prose does not accidentally look like `ghp_` + 36 base62 characters.
#: Deliberately not a general "high entropy string" heuristic: that would
#: redact digests and diffs, and an export nobody trusts gets read past.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}"),  # Anthropic
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),  # OpenAI and compatibles
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}"),  # GitHub
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),  # Slack
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),  # Google
    re.compile(r"\bSG\.[A-Za-z0-9_-]{16,}"),  # SendGrid
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),  # PEM/OpenSSH block
)


def _secret_values() -> list[str]:
    values = [
        value
        for name, value in os.environ.items()
        if name.upper().endswith(_SECRET_ENV_SUFFIXES) and len(value) >= _MIN_SECRET_LENGTH
    ]
    # Longest first, so a secret that contains another is not partially masked.
    return sorted(set(values), key=len, reverse=True)


def _redact(text: str) -> str:
    """Mask credentials in a rendered report.

    Two passes, because they fail differently: the env sweep catches this
    process's own secrets exactly (no false positives, but blind to anything
    it never held), and the pattern sweep catches well-known credential
    shapes from anywhere (a key pasted into a goal, one a tool read out of a
    file). Neither is complete, and this is a redaction pass on an artifact —
    not a reason to put secrets in front of the agent.
    """
    for value in _secret_values():
        if value in text:
            text = text.replace(value, "[redacted]")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[redacted]", text)
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
