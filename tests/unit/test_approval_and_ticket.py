import asyncio

from haven.application.approvals import QueueApprovalBroker
from haven.domain import (
    ApprovalDecision,
    ApprovalId,
    ApprovalRecord,
    RiskLevel,
    RunId,
    ToolCallId,
    compute_approval_digest,
    mint_ticket,
)
from haven.domain.approval import ApprovalRequest
from haven.domain.ticket import ExecutionTicket


def make_digest(**overrides: str | None) -> str:
    kwargs: dict[str, str | None] = {
        "workspace_digest": "ws1",
        "tool_name": "repo.edit",
        "tool_version": "1",
        "canonical_args_json": '{"path":"a.py"}',
        "preimage_digest": "pre1",
        "preview_digest": "diff1",
    }
    kwargs.update(overrides)
    return compute_approval_digest(**kwargs)  # type: ignore[arg-type]


def test_digest_is_deterministic() -> None:
    assert make_digest() == make_digest()


def test_digest_changes_when_any_fact_changes() -> None:
    base = make_digest()
    assert make_digest(canonical_args_json='{"path":"b.py"}') != base
    assert make_digest(preimage_digest="pre2") != base
    assert make_digest(preview_digest="diff2") != base
    assert make_digest(workspace_digest="ws2") != base
    # 工具实现升级后，旧审批不得沿用：版本进摘要就是为了让跨版本恢复重新询问。
    assert make_digest(tool_version="2") != base


def test_approval_record_validity() -> None:
    digest = make_digest()
    record = ApprovalRecord(
        approval_id=ApprovalId("apr-1"),
        request_digest=digest,
        decision=ApprovalDecision.APPROVED,
    )
    assert record.is_valid_for(digest)
    # 绑定操作的任何漂移都会使审批失效
    assert not record.is_valid_for(make_digest(preimage_digest="changed"))


def test_rejected_or_consumed_approval_is_invalid() -> None:
    digest = make_digest()
    rejected = ApprovalRecord(
        approval_id=ApprovalId("apr-1"),
        request_digest=digest,
        decision=ApprovalDecision.REJECTED,
    )
    assert not rejected.is_valid_for(digest)

    consumed = ApprovalRecord(
        approval_id=ApprovalId("apr-2"),
        request_digest=digest,
        decision=ApprovalDecision.APPROVED,
        consumed=True,
    )
    assert not consumed.is_valid_for(digest)


def _ticket(**overrides: object) -> ExecutionTicket:
    fields: dict[str, object] = {
        "call_id": ToolCallId("c1"),
        "tool_name": "repo.edit",
        "tool_version": "1",
        "canonical_args_json": '{"path":"a.py"}',
        "workspace_digest": "ws1",
        "preimage_digest": "pre1",
        "approval_id": None,
    }
    fields.update(overrides)
    return mint_ticket(**fields)  # type: ignore[arg-type]


def test_ticket_digest_binds_every_field() -> None:
    """票据摘要必须绑定全部七个字段。

    这个测试此前名叫 `..._binds_all_fields`，却只变了一个 `preimage_digest`——
    把其余五个字段从摘要里删掉，1580 条测试依然全绿（变异测试实测）。票据是
    执行器唯一接受的凭证，少绑定任何一个字段都意味着一次调用可以被偷换成另一次。
    """
    base = _ticket()
    assert _ticket().ticket_digest == base.ticket_digest
    for field, value in (
        ("call_id", ToolCallId("c2")),
        ("tool_name", "repo.create"),
        ("tool_version", "2"),
        ("canonical_args_json", '{"path":"b.py"}'),
        ("workspace_digest", "ws2"),
        ("preimage_digest", "pre2"),
        ("approval_id", ApprovalId("apr-1")),
    ):
        assert _ticket(**{field: value}).ticket_digest != base.ticket_digest, (
            f"ticket digest does not bind {field}"
        )


def test_the_ticket_digest_is_reproducible() -> None:
    """同一组输入永远得到同一个摘要——否则执行器无法校验它收到的票据。"""
    assert _ticket().ticket_digest == _ticket().ticket_digest


async def test_queue_approval_broker_lifecycle() -> None:
    broker = QueueApprovalBroker()
    assert broker.pending_ids == ()
    assert not broker.resolve("nonexistent", ApprovalDecision.APPROVED)

    req = ApprovalRequest(
        approval_id=ApprovalId("app-1"),
        run_id=RunId("run-1"),
        call_id=ToolCallId("c1"),
        tool_name="repo.edit",
        summary="fix bug",
        risk=RiskLevel.MEDIUM,
        request_digest="req-digest",
        preview="diff text",
    )
    task = asyncio.create_task(broker.respond(req))
    await asyncio.sleep(0.01)

    assert "app-1" in broker.pending_ids
    assert len(broker.pending_ids) == 1
    assert not broker.resolve("other-id", ApprovalDecision.APPROVED)
    assert broker.resolve("app-1", ApprovalDecision.APPROVED)
    assert not broker.resolve("app-1", ApprovalDecision.APPROVED)

    decision = await task
    assert decision is ApprovalDecision.APPROVED
    assert broker.pending_ids == ()
