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
    approval_id: ApprovalId
    run_id: RunId
    call_id: ToolCallId
    tool_name: str
    summary: str
    risk: RiskLevel
    request_digest: str
    preview: str


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    approval_id: ApprovalId
    request_digest: str
    decision: ApprovalDecision
    consumed: bool = False

    def is_valid_for(self, digest: str) -> bool:
        """只有审批通过、尚未消费且摘要仍与即将执行的操作匹配时，记录才授权执行。"""
        return (
            self.decision is ApprovalDecision.APPROVED
            and not self.consumed
            and self.request_digest == digest
        )
