"""HeadlessApprover: the automated approval policy for non-interactive runs."""

from haven.application.approvals import HeadlessApprover
from haven.domain.approval import ApprovalRequest
from haven.domain.enums import ApprovalDecision, RiskLevel
from haven.domain.ids import ToolCallId


def request(tool_name: str) -> ApprovalRequest:
    return ApprovalRequest(
        approval_id="a1",
        run_id="r1",
        call_id=ToolCallId("c1"),
        tool_name=tool_name,
        summary="",
        risk=RiskLevel.MEDIUM,
        request_digest="d",
        preview="",
    )


async def test_reject_denies_everything() -> None:
    approver = HeadlessApprover("reject")
    assert await approver.respond(request("repo.edit")) is ApprovalDecision.REJECTED
    assert await approver.respond(request("repo.check")) is ApprovalDecision.REJECTED


async def test_trusted_recipe_approves_only_checks() -> None:
    approver = HeadlessApprover("trusted_recipe")
    assert await approver.respond(request("repo.check")) is ApprovalDecision.APPROVED
    assert await approver.respond(request("repo.edit")) is ApprovalDecision.REJECTED
    assert await approver.respond(request("repo.apply_patch")) is ApprovalDecision.REJECTED


async def test_all_approves_everything() -> None:
    approver = HeadlessApprover("all")
    assert await approver.respond(request("repo.edit")) is ApprovalDecision.APPROVED
    assert await approver.respond(request("repo.apply_patch")) is ApprovalDecision.APPROVED
