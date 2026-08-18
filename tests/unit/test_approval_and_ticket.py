from haven.domain import (
    ApprovalDecision,
    ApprovalId,
    ApprovalRecord,
    ToolCallId,
    compute_approval_digest,
    mint_ticket,
)


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


def test_ticket_digest_binds_all_fields() -> None:
    ticket_a = mint_ticket(
        call_id=ToolCallId("c1"),
        tool_name="repo.edit",
        tool_version="1",
        canonical_args_json='{"path":"a.py"}',
        workspace_digest="ws1",
        preimage_digest="pre1",
        approval_id=None,
    )
    ticket_b = mint_ticket(
        call_id=ToolCallId("c1"),
        tool_name="repo.edit",
        tool_version="1",
        canonical_args_json='{"path":"a.py"}',
        workspace_digest="ws1",
        preimage_digest="pre2",  # 不同的 preimage
        approval_id=None,
    )
    assert ticket_a.ticket_digest != ticket_b.ticket_digest
