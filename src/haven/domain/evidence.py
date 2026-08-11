"""Evidence ledger and the Evidence Gate.

The model's own words are never sufficient proof of success. When a run has
written files, success additionally requires a diff and a passing verification
recorded *after* the last write.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from haven.domain.review import describe, review_diff


@dataclass(frozen=True, slots=True)
class EditEvidence:
    seq: int
    path: str
    preimage_digest: str
    postimage_digest: str


@dataclass(frozen=True, slots=True)
class CheckEvidence:
    seq: int
    recipe_id: str
    exit_code: int
    duration_ms: int
    truncated: bool


@dataclass(frozen=True, slots=True)
class DiffEvidence:
    seq: int
    files_changed: int
    insertions: int
    deletions: int
    diff_digest: str


@dataclass(frozen=True, slots=True)
class EvidenceLedger:
    edits: tuple[EditEvidence, ...] = field(default_factory=tuple)
    checks: tuple[CheckEvidence, ...] = field(default_factory=tuple)
    diffs: tuple[DiffEvidence, ...] = field(default_factory=tuple)

    def with_edit(self, edit: EditEvidence) -> EvidenceLedger:
        return replace(self, edits=(*self.edits, edit))

    def with_check(self, check: CheckEvidence) -> EvidenceLedger:
        return replace(self, checks=(*self.checks, check))

    def with_diff(self, diff: DiffEvidence) -> EvidenceLedger:
        return replace(self, diffs=(*self.diffs, diff))

    @property
    def has_edits(self) -> bool:
        return bool(self.edits)

    @property
    def last_edit_seq(self) -> int:
        return max((e.seq for e in self.edits), default=-1)


@dataclass(frozen=True, slots=True)
class GateResult:
    passed: bool
    reason_code: str
    detail: str


def evaluate_evidence_gate(ledger: EvidenceLedger, diff_text: str = "") -> GateResult:
    """Program decision on whether a final answer may count as success.

    `diff_text` is the run's accumulated diff; when supplied it is reviewed for
    obviously dangerous content (see `domain.review`). A run must both produce
    evidence and not have written something plainly wrong.
    """
    if not ledger.has_edits:
        return GateResult(True, "no_writes", "Run made no file changes; final answer accepted.")

    last_write = ledger.last_edit_seq

    fresh_diffs = [d for d in ledger.diffs if d.seq > last_write]
    if not fresh_diffs:
        return GateResult(
            False,
            "missing_diff",
            "Files were edited but no diff was recorded after the last write.",
        )

    fresh_checks = [c for c in ledger.checks if c.seq > last_write]
    if not fresh_checks:
        return GateResult(
            False,
            "missing_check",
            "Files were edited but no verification ran after the last write.",
        )

    failing = [c for c in fresh_checks if c.exit_code != 0]
    if failing:
        return GateResult(
            False,
            "check_failed",
            f"Latest verification failed (recipe={failing[-1].recipe_id}, "
            f"exit_code={failing[-1].exit_code}).",
        )

    if diff_text:
        findings = review_diff(diff_text)
        if findings:
            return GateResult(
                False,
                "review_failed",
                f"The change contains problems that must be fixed: {describe(findings)}.",
            )

    return GateResult(True, "evidence_satisfied", "Diff and passing verification after last write.")
