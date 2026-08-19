"""执行票据：执行器唯一接受的凭证。

模型的原始 JSON 永远不会到达执行器。流水线会先校验并授权，然后为绑定到精确
规范化操作的内容铸造票据。
"""

from __future__ import annotations

from dataclasses import dataclass

from haven.domain.digest import digest_of
from haven.domain.ids import ApprovalId, ToolCallId


@dataclass(frozen=True, slots=True)
class ExecutionTicket:
    """绑定工具名称、参数摘要和审批结果的单次执行凭证。"""

    #: 提议中的工具调用标识。
    call_id: ToolCallId
    #: 流水线接受的已注册工具形态。
    tool_name: str
    #: 纳入摘要计算的工具契约版本。
    tool_version: str
    #: 严格校验后的规范 JSON 参数。
    canonical_args_json: str
    #: 绑定到本次执行的工作区状态。
    workspace_digest: str
    #: 适用于写操作时绑定的前像摘要。
    preimage_digest: str | None
    #: 已消费的审批标识；策略直接允许的调用为 None。
    approval_id: ApprovalId | None
    #: 所有票据字段的摘要；执行器会记录该值。
    ticket_digest: str


def mint_ticket(
    *,
    call_id: ToolCallId,
    tool_name: str,
    tool_version: str,
    canonical_args_json: str,
    workspace_digest: str,
    preimage_digest: str | None,
    approval_id: ApprovalId | None,
) -> ExecutionTicket:
    """将调用、参数、工作区和审批绑定到不可伪造的执行票据摘要。"""
    ticket_digest = digest_of(
        {
            "call_id": call_id,
            "tool": tool_name,
            "version": tool_version,
            "args": canonical_args_json,
            "workspace": workspace_digest,
            "preimage": preimage_digest,
            "approval": approval_id,
        }
    )
    return ExecutionTicket(
        call_id=call_id,
        tool_name=tool_name,
        tool_version=tool_version,
        canonical_args_json=canonical_args_json,
        workspace_digest=workspace_digest,
        preimage_digest=preimage_digest,
        approval_id=approval_id,
        ticket_digest=ticket_digest,
    )
