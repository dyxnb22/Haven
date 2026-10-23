"""整个系统共享的核心枚举。"""

from __future__ import annotations

from enum import StrEnum


class RunStatus(StrEnum):
    CREATED = "created"
    RUNNING_MODEL = "running_model"
    VALIDATING_TOOL = "validating_tool"
    WAITING_APPROVAL = "waiting_approval"
    EXECUTING_TOOL = "executing_tool"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    STOPPED = "stopped"
    CANCELLED = "cancelled"
    EFFECT_UNKNOWN = "effect_unknown"


#: 运行仍能继续推进的状态。
ACTIVE_STATUSES = frozenset(
    {
        RunStatus.CREATED,
        RunStatus.RUNNING_MODEL,
        RunStatus.VALIDATING_TOOL,
        RunStatus.WAITING_APPROVAL,
        RunStatus.EXECUTING_TOOL,
        RunStatus.VERIFYING,
    }
)


class StopReason(StrEnum):
    FINAL_ANSWER = "final_answer"
    EVIDENCE_SATISFIED = "evidence_satisfied"
    EVIDENCE_MISSING = "evidence_missing"
    VERIFICATION_UNAVAILABLE = "verification_unavailable"
    STEP_BUDGET_EXHAUSTED = "step_budget_exhausted"
    TOOL_BUDGET_EXHAUSTED = "tool_budget_exhausted"
    TOKEN_BUDGET_EXHAUSTED = "token_budget_exhausted"
    COST_BUDGET_EXHAUSTED = "cost_budget_exhausted"
    WALL_TIME_EXHAUSTED = "wall_time_exhausted"
    NO_PROGRESS = "no_progress"
    CANCELLED = "cancelled"
    PROVIDER_ERROR = "provider_error"
    TOOL_ERROR = "tool_error"
    EFFECT_UNKNOWN = "effect_unknown"


class PermissionMode(StrEnum):
    """权限模式：区分写入权限的硬性边界（read_only 下策略层硬性 DENY 一切修改）。"""

    INTERACTIVE = "interactive"
    READ_ONLY = "read_only"


class ApprovalMode(StrEnum):
    """分级审批模式：控制当策略返回 ASK 时，如何对操作做出批准决断。

    - ``ASK``（严格审查）：每次副作用均需人工审批卡片确认。
    - ``SMART``（智能分级）：低/中风险操作自动放行，
      仅高风险操作（删除、移动、大补丁、Shell）弹窗确认。
    - ``FULL``（工作区自主）：工作区内只要策略允许 ASK，全部自动放行；DENY 仍不可逾越。
    """

    ASK = "ask"
    SMART = "smart"
    FULL = "full"


class PolicyDecision(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class RiskLevel(StrEnum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ToolStatus(StrEnum):
    OK = "ok"
    ERROR = "error"


class ToolErrorCode(StrEnum):
    UNKNOWN_TOOL = "unknown_tool"
    INVALID_ARGUMENTS = "invalid_arguments"
    NOT_FOUND = "not_found"
    DENIED = "denied"
    APPROVAL_REJECTED = "approval_rejected"
    STALE_PREIMAGE = "stale_preimage"
    AMBIGUOUS_MATCH = "ambiguous_match"
    TIMEOUT = "timeout"
    TRUNCATED = "truncated"
    SYNTAX_ERROR = "syntax_error"
    INTERNAL = "internal"
    #: 进程修改了 `.git` / `.haven` / `.haven.toml`——这是内核无法强制保护的
    #: 边界（使用 Landlock 或没有沙箱后端时）。工具调用会失败，因此该违规
    #: 是硬性结果，而不是一条附注（ADR 0018）。
    PROTECTED_PATH_TAMPERED = "protected_path_tampered"


class EffectState(StrEnum):
    """执行日志记录的副作用生命周期。"""

    STARTED = "started"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    EFFECT_UNKNOWN = "effect_unknown"
    RECONCILED_CONFIRMED = "reconciled_confirmed"
    RECONCILED_NOT_RUN = "reconciled_not_run"
    ABANDONED = "abandoned"


class ApprovalDecision(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"
