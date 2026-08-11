from haven.domain import (
    CheckEvidence,
    DiffEvidence,
    EditEvidence,
    EvidenceLedger,
    evaluate_evidence_gate,
)


def edit(seq: int) -> EditEvidence:
    return EditEvidence(seq=seq, path="src/a.py", preimage_digest="pre", postimage_digest="post")


def check(seq: int, exit_code: int = 0) -> CheckEvidence:
    return CheckEvidence(
        seq=seq, recipe_id="pytest", exit_code=exit_code, duration_ms=100, truncated=False
    )


def diff(seq: int) -> DiffEvidence:
    return DiffEvidence(seq=seq, files_changed=1, insertions=2, deletions=1, diff_digest="d")


def test_no_writes_passes() -> None:
    result = evaluate_evidence_gate(EvidenceLedger())
    assert result.passed
    assert result.reason_code == "no_writes"


def test_write_without_diff_fails() -> None:
    ledger = EvidenceLedger().with_edit(edit(1)).with_check(check(2))
    result = evaluate_evidence_gate(ledger)
    assert not result.passed
    assert result.reason_code == "missing_diff"


def test_write_without_check_fails() -> None:
    ledger = EvidenceLedger().with_edit(edit(1)).with_diff(diff(2))
    result = evaluate_evidence_gate(ledger)
    assert not result.passed
    assert result.reason_code == "missing_check"


def test_stale_check_before_last_write_does_not_count() -> None:
    ledger = (
        EvidenceLedger()
        .with_check(check(1))
        .with_diff(diff(2))
        .with_edit(edit(3))  # write after the check invalidates it
        .with_diff(diff(4))
    )
    result = evaluate_evidence_gate(ledger)
    assert not result.passed
    assert result.reason_code == "missing_check"


def test_failing_check_fails_gate() -> None:
    ledger = EvidenceLedger().with_edit(edit(1)).with_diff(diff(2)).with_check(check(3, 1))
    result = evaluate_evidence_gate(ledger)
    assert not result.passed
    assert result.reason_code == "check_failed"


def test_full_evidence_passes() -> None:
    ledger = EvidenceLedger().with_edit(edit(1)).with_diff(diff(2)).with_check(check(3))
    result = evaluate_evidence_gate(ledger)
    assert result.passed
    assert result.reason_code == "evidence_satisfied"
