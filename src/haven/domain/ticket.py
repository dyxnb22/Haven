"""Execution tickets: the only currency the executor accepts.

Raw model JSON never reaches the executor. The pipeline validates, authorizes,
and then mints a ticket bound to the exact normalized action.
"""

from __future__ import annotations

from dataclasses import dataclass

from haven.domain.digest import digest_of
from haven.domain.ids import ApprovalId, ToolCallId


@dataclass(frozen=True, slots=True)
class ExecutionTicket:
    call_id: ToolCallId
    tool_name: str
    tool_version: str
    canonical_args_json: str
    workspace_digest: str
    preimage_digest: str | None
    approval_id: ApprovalId | None
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
