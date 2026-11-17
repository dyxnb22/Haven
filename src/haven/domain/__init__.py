"""Haven 领域层：纯逻辑，不执行 I/O，也不导入框架。

这里的所有内容都是确定性函数或不可变值对象，因此每个与安全相关的决策都能
单独进行单元测试：

    policy.py       谁可以做什么（副作用的唯一权威）
    approval.py     绑定摘要、一次性使用的审批记录
    ticket.py       执行票据——执行器唯一接受的凭证
    evidence.py     Evidence Gate：什么才算运行成功
    budget.py       硬性上限（步骤/工具/时间/token/成本）
    exec_policy.py  针对提议命令行的审批摩擦等级
    review.py       对本次运行写入行的确定性扫描
    stuck.py        无进展（调用和结果相同）检测
    transitions.py  RunStatus 状态机
    discovery.py    根据仓库自身文件提出验证配方
    digest.py/ids.py/enums.py/pricing.py   共享基础能力
"""

from haven.domain.approval import ApprovalRecord, ApprovalRequest, compute_approval_digest
from haven.domain.budget import Budget, BudgetUsage, check_accumulated_budget, check_budget
from haven.domain.digest import canonical_json, digest_of, sha256_bytes, sha256_text
from haven.domain.enums import (
    ACTIVE_STATUSES,
    AgentMode,
    ApprovalDecision,
    ApprovalMode,
    EffectState,
    PermissionMode,
    PlanStatus,
    PolicyDecision,
    RiskLevel,
    RunStatus,
    StopReason,
    ToolErrorCode,
    ToolStatus,
)
from haven.domain.evidence import (
    CheckEvidence,
    DiffEvidence,
    EditEvidence,
    EvidenceLedger,
    GateResult,
    evaluate_evidence_gate,
)
from haven.domain.exec_policy import ExecClass, classify_argv, split_pipeline
from haven.domain.ids import (
    ApprovalId,
    RunId,
    ToolCallId,
    new_approval_id,
    new_run_id,
)
from haven.domain.policy import (
    EFFECT_TOOLS,
    EXEC_TOOLS,
    KNOWN_TOOLS,
    READ_ONLY_TOOLS,
    STATE_TOOLS,
    PolicyOutcome,
    ToolFacts,
    evaluate_policy,
)
from haven.domain.pricing import Pricing
from haven.domain.review import ReviewFinding, review_diff
from haven.domain.stuck import StuckLoopDetector, call_fingerprint
from haven.domain.syntax_gate import (
    SyntaxDiagnostic,
    format_syntax_error,
    validate_syntax,
)
from haven.domain.ticket import ExecutionTicket, mint_ticket
from haven.domain.transitions import InvalidTransitionError, transition

__all__ = [
    "ACTIVE_STATUSES",
    "EFFECT_TOOLS",
    "EXEC_TOOLS",
    "KNOWN_TOOLS",
    "READ_ONLY_TOOLS",
    "STATE_TOOLS",
    "AgentMode",
    "ApprovalDecision",
    "ApprovalId",
    "ApprovalMode",
    "ApprovalRecord",
    "PlanStatus",
    "ApprovalRequest",
    "Budget",
    "BudgetUsage",
    "CheckEvidence",
    "DiffEvidence",
    "EditEvidence",
    "EffectState",
    "EvidenceLedger",
    "ExecClass",
    "ExecutionTicket",
    "GateResult",
    "InvalidTransitionError",
    "PermissionMode",
    "PolicyDecision",
    "PolicyOutcome",
    "Pricing",
    "ReviewFinding",
    "RiskLevel",
    "RunId",
    "RunStatus",
    "StopReason",
    "StuckLoopDetector",
    "SyntaxDiagnostic",
    "ToolCallId",
    "ToolErrorCode",
    "ToolFacts",
    "ToolStatus",
    "call_fingerprint",
    "canonical_json",
    "check_budget",
    "check_accumulated_budget",
    "classify_argv",
    "compute_approval_digest",
    "digest_of",
    "evaluate_evidence_gate",
    "evaluate_policy",
    "format_syntax_error",
    "mint_ticket",
    "new_approval_id",
    "new_run_id",
    "review_diff",
    "sha256_bytes",
    "sha256_text",
    "split_pipeline",
    "transition",
    "validate_syntax",
]
