"""证据账本和 Evidence Gate。

模型自己的话永远不足以证明成功。当运行写入文件后，成功还必须具备差异，并且
在最后一次写入之后记录到通过的验证结果。
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
    #: 如果代理无论继续做多少工作都无法满足门禁，则为 True。
    #: 循环必须停止，不能继续发送 nudge，否则会在无法取胜的状态下耗尽
    #: 全部预算。
    terminal: bool = False


def evaluate_evidence_gate(
    ledger: EvidenceLedger,
    diff_text: str = "",
    *,
    verification_available: bool = True,
) -> GateResult:
    """由程序决定最终答案是否可以计为成功。

    `diff_text` 是本次运行累计的差异；传入后会检查其中是否有明显危险的内容
    （见 `domain.review`）。运行既必须产生证据，也不能写入明显错误的内容。

    `verification_available` 表示是否注册了任何检查配方。如果运行写入了文件，
    但没有可用配方，门禁就永远无法满足——代理无法凭空创造验证器——因此这是
    终止性失败，而不是可以重试的失败。
    """
    if not ledger.has_edits:
        return GateResult(True, "no_writes", "Run made no file changes; final answer accepted.")

    if not verification_available:
        return GateResult(
            False,
            "verification_unavailable",
            "Files were changed but no check recipe is registered, so the change "
            "cannot be verified. Register a recipe in .haven.toml, or re-run the "
            "task in read-only mode if it did not need changes.",
            terminal=True,
        )

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
