"""运行报告渲染（已脱敏）。"""

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

#: 名称以其中任一后缀结尾的环境变量都会视为秘密，因此无需维护列表就能
#: 覆盖提供商特定名称（DEEPSEEK_API_KEY、GROQ_TOKEN 等）。
_SECRET_ENV_SUFFIXES = ("_API_KEY", "_KEY", "_TOKEN", "_SECRET", "_PASSWORD")
_MIN_SECRET_LENGTH = 8

#: 用于兜底处理本进程从未持有、因而上面的环境变量扫描看不到的凭据：粘贴
#: 到 goal 中的密钥、工具从文件读取的密钥，或模型输出中引用的其他服务 token。
#: 按发行方公布的自身格式匹配，从而降低误报——普通 prose 不会意外匹配
#: `ghp_` + 36 个 base62 字符。
#: 特意不使用通用的“高熵字符串”启发式：那会遮蔽摘要和 diff，使无人信任
#: 的导出内容被读过去。
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"-----BEGIN (?P<label>[A-Z0-9 ]*PRIVATE KEY)-----.*?"
        r"-----END (?P=label)-----",
        re.DOTALL,
    ),  # 完整 PEM/OpenSSH 私钥块
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}"),  # Anthropic 密钥
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),  # OpenAI 及兼容服务密钥
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS 访问密钥 ID
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}"),  # GitHub 令牌
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),  # Slack 令牌
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),  # Google API 密钥
    re.compile(r"\bSG\.[A-Za-z0-9_-]{16,}"),  # SendGrid API 密钥
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),  # 不完整密钥块的头部兜底
)


def _secret_values() -> list[str]:
    values = [
        value
        for name, value in os.environ.items()
        if name.upper().endswith(_SECRET_ENV_SUFFIXES) and len(value) >= _MIN_SECRET_LENGTH
    ]
    # 按长度从长到短排列，避免包含其他秘密的字符串只被部分遮蔽。
    return sorted(set(values), key=len, reverse=True)


def _redact(text: str) -> str:
    """遮盖渲染报告中的凭据。

    分两轮处理，因为两者的失效方式不同：环境变量扫描可以精确捕获当前进程自身
    的秘密（没有误报，但看不到进程从未持有的内容）；模式扫描则能从任意位置捕获
    常见凭据形状（例如粘贴到目标中的密钥，或工具从文件读出的密钥）。两者都不
    完整；这是对构件进行脱敏的步骤，并不意味着应该把秘密交给代理。
    """
    for value in _secret_values():
        if value in text:
            text = text.replace(value, "[redacted]")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[redacted]", text)
    return text


def render_jsonl(envelopes: list[EventEnvelope]) -> str:
    """将事件逐行导出为稳定的 JSONL，便于脚本消费。"""
    return "\n".join(_redact(env.model_dump_json()) for env in envelopes) + "\n"


def _fenced_block(text: str, language: str) -> list[str]:
    """生成不会被内容中的反引号提前闭合的 Markdown 围栏。"""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return [f"{fence}{language}", text, fence]


def render_markdown(run: RunRecord, envelopes: list[EventEnvelope]) -> str:
    """将运行索引和事件轨迹渲染为面向人的 Markdown 报告。"""
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
                    *_fenced_block(event.preview, "diff"),
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
                    (
                        f"- cost: ${event.cost_usd:.4f}"
                        if event.cost_known
                        else "- cost: unknown (pricing unavailable)"
                    ),
                ]
            )
    return _redact("\n".join(lines) + "\n")
