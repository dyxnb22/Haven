"""CLI 和 TUI 共用的操作提示；只影响展示，不改变权限或运行事件。"""

import os

from haven.contracts.events import (
    ApplicationEvent,
    Notice,
    PolicyDecided,
    RunFinished,
    ToolCompleted,
    UserInputRequested,
)

_RECIPE_HINT = (
    "In the workspace, run `haven discover` to find verification recipes; "
    "review them, then use `haven discover --accept` to save them."
)
_POLICY_HINTS = {
    "read_only_mode": (
        "This run is read-only. To make changes, start a new run with "
        "`haven run GOAL --write` in the workspace and review its approval policy."
    ),
    "plan_mode": (
        "Review the plan, then use `/implement` in the TUI or `haven implement RUN_ID`. "
        "Changing approval mode does not enable writes during planning."
    ),
    "unregistered_recipe": _RECIPE_HINT,
    "outside_workspace": (
        "Choose a path inside the workspace, or start Haven in the intended repository."
    ),
    "protected_path": (
        "Choose a regular source file; repository control paths are protected. "
        "Approval mode cannot override this restriction."
    ),
    "sandbox_unavailable": (
        "Run `haven doctor` in the workspace to inspect sandbox support. "
        "Command execution requires a supported OS sandbox."
    ),
}
_PROVIDER_HINTS = {
    "auth": (
        "Check the API key and provider base URL. Run `haven config explain` in the workspace "
        "to see which environment variable and endpoint are selected."
    ),
    "quota": "Check the provider account balance and spending limits before retrying.",
    "rate_limited": "Wait for the provider rate limit to reset, then retry the task.",
    "network": (
        "Check the connection, proxy, and provider base URL, then retry. "
        "Use `haven config explain` in the workspace to inspect the endpoint."
    ),
    "timeout": "Check the connection and provider availability, then retry the task.",
    "server": "Check the provider service status and retry when it is available.",
    "protocol": (
        "Check that the selected endpoint supports streaming and tool calls "
        "with the configured OpenAI-compatible provider."
    ),
    "context_overflow": (
        "Start a new session with a smaller task, or reduce the configured context budget."
    ),
}


def error_hint(message: str) -> str | None:
    """为程序产生的启动或 Provider 错误提供固定建议，不回显错误载荷。"""
    if message.startswith("no API key found in $"):
        return (
            "Set the API key in the environment variable named above. "
            "Run `haven config explain` in the workspace to check provider settings."
        )
    prefix = "provider error ("
    if message.startswith(prefix):
        code, separator, _ = message[len(prefix) :].partition("):")
        if separator:
            return _PROVIDER_HINTS.get(code)
    return None


def event_hint(event: ApplicationEvent) -> str | None:
    """从现有错误原因生成界面提示，不添加日志事件或反馈给模型。"""
    if isinstance(event, PolicyDecided) and event.decision == "deny":
        return _POLICY_HINTS.get(event.reason_code)
    if isinstance(event, ToolCompleted) and event.error_code == "approval_rejected":
        return (
            "Review the proposed action before approving it. For headless runs, "
            "choose `--approval-policy` explicitly; its default is `reject`."
        )
    if isinstance(event, Notice) and event.level == "error":
        return error_hint(event.message)
    if isinstance(event, RunFinished) and event.gate_reason == "verification_unavailable":
        return _RECIPE_HINT
    return None


def needs_attention(event: ApplicationEvent) -> bool:
    """只在任务完成、失败/停止或等待回答时通知；主动取消保持安静。"""
    return isinstance(event, UserInputRequested) or (
        isinstance(event, RunFinished)
        and event.status in {"succeeded", "failed", "stopped", "effect_unknown"}
    )


def bell_enabled() -> bool:
    """允许用户通过环境变量关闭终端通知。"""
    return os.environ.get("HAVEN_BELL", "1").strip().lower() not in {"0", "false", "off", "no"}
