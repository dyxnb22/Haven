"""Core enums shared across the whole system."""

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


#: Statuses in which a run can still make progress.
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
    """Only two modes on purpose: there is no fully-autonomous write mode."""

    INTERACTIVE = "interactive"
    READ_ONLY = "read_only"


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
    INTERNAL = "internal"


class EffectState(StrEnum):
    """Lifecycle of a side effect as recorded in the execution journal."""

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
