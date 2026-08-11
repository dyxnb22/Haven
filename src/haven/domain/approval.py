"""Exact, digest-bound, single-use approvals.

An approval is bound to the workspace, the tool, the canonical arguments, and
the file preimage. If any of these change between approval and execution, the
approval is stale and execution must fail closed.
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
    """Digest that pins one approval to the exact action it authorizes."""
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
        """A record authorizes execution only if approved, unconsumed, and the
        digest still matches the action about to run."""
        return (
            self.decision is ApprovalDecision.APPROVED
            and not self.consumed
            and self.request_digest == digest
        )
