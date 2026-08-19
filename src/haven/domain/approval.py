"""精确、绑定摘要且只能使用一次的审批。

审批会绑定工作区、工具、规范化参数和文件前像。如果这些内容在审批与执行之间
发生任何变化，审批就会过期，执行必须失败并关闭。
"""

from __future__ import annotations

from dataclasses import dataclass

from haven.domain.digest import digest_of
from haven.domain.enums import ApprovalDecision, RiskLevel
from haven.domain.ids import ApprovalId, RunId, ToolCallId


def compute_approval_digest(
    *,
    workspace_digest: str,
    tool_name: str,
    tool_version: str,
    canonical_args_json: str,
    preimage_digest: str | None,
    preview_digest: str | None,
) -> str:
    """将一次审批固定到其授权的精确操作上的摘要。"""
    return digest_of(
        {
            "workspace": workspace_digest,
            "tool": tool_name,
            "version": tool_version,
            "args": canonical_args_json,
            "preimage": preimage_digest,
            "preview": preview_digest,
        }
    )


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """发送给用户确认的工具调用摘要，绑定精确请求摘要。"""

    #: 这次一次性审批请求的持久标识。
    approval_id: ApprovalId
    #: 拥有该请求的运行。
    run_id: RunId
    #: 提议的工具调用标识。
    call_id: ToolCallId
    #: 正在审批的工具形态。
    tool_name: str
    #: 人类可读的意图摘要。
    summary: str
    #: 确定性策略计算出的风险等级。
    risk: RiskLevel
    #: 将参数、工作区和预览绑定到请求上的摘要。
    request_digest: str
    #: 展示给用户的有界预览。
    preview: str


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    """审批请求及其当前决定的持久化记录。"""

    #: 与 ApprovalRequest 共享的持久标识。
    approval_id: ApprovalId
    #: 必须与执行请求匹配的摘要。
    request_digest: str
    #: 用户或自动审批决定。
    decision: ApprovalDecision
    #: 这次一次性审批是否已经铸造执行票据。
    consumed: bool = False

    def is_valid_for(self, digest: str) -> bool:
        """只有审批通过、尚未消费且摘要仍与即将执行的操作匹配时，记录才授权执行。"""
        return (
            self.decision is ApprovalDecision.APPROVED
            and not self.consumed
            and self.request_digest == digest
        )
