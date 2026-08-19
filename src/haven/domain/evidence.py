"""证据账本和 Evidence Gate。

模型自己的话永远不足以证明成功。当运行写入文件后，成功还必须具备差异，并且
在最后一次写入之后记录到通过的验证结果。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from haven.domain.review import describe, review_diff


@dataclass(frozen=True, slots=True)
class EditEvidence:
    """一次文件写入前后的摘要，用于证明实际变更内容。"""

    #: 记录该写入时的事件序号。
    seq: int
    #: 发生变化的规范化工作区相对路径。
    path: str
    #: 写入前一刻的文件摘要。
    preimage_digest: str
    #: 写入后一刻的文件摘要。
    postimage_digest: str


@dataclass(frozen=True, slots=True)
class CheckEvidence:
    """一次验证配方的结果，包含其发生顺序和输出是否完整。"""

    #: 该检查完成时的事件序号。
    seq: int
    #: 已注册的 recipe 标识。
    recipe_id: str
    #: 子进程退出状态。
    exit_code: int
    #: 检查耗时，单位为毫秒。
    duration_ms: int
    #: 捕获的输出是否被截断。
    truncated: bool


@dataclass(frozen=True, slots=True)
class DiffEvidence:
    """一次差异检查的摘要，说明变更规模及其内容指纹。"""

    #: 记录该差异时的事件序号。
    seq: int
    #: 存在净变化的文件数量。
    files_changed: int
    #: 新增行数。
    insertions: int
    #: 删除行数。
    deletions: int
    #: 完整差异构件的摘要。
    diff_digest: str


@dataclass(frozen=True, slots=True)
class EvidenceLedger:
    """按事件序号累积的编辑、检查和差异证据。"""

    #: 按事件顺序排列的文件写入证据。
    edits: tuple[EditEvidence, ...] = field(default_factory=tuple)
    #: 按事件顺序排列的验证证据。
    checks: tuple[CheckEvidence, ...] = field(default_factory=tuple)
    #: 按事件顺序排列的差异证据。
    diffs: tuple[DiffEvidence, ...] = field(default_factory=tuple)

    def with_edit(self, edit: EditEvidence) -> EvidenceLedger:
        """追加一条文件编辑证据并返回新的不可变账本。"""
        return replace(self, edits=(*self.edits, edit))

    def with_check(self, check: CheckEvidence) -> EvidenceLedger:
        """追加一条检查证据并返回新的不可变账本。"""
        return replace(self, checks=(*self.checks, check))

    def with_diff(self, diff: DiffEvidence) -> EvidenceLedger:
        """追加一条差异证据并返回新的不可变账本。"""
        return replace(self, diffs=(*self.diffs, diff))

    @property
    def has_edits(self) -> bool:
        """是否至少记录过一次文件编辑。"""
        return bool(self.edits)

    @property
    def last_edit_seq(self) -> int:
        """返回最近编辑证据的事件序号；没有编辑时为 ``-1``。"""
        return max((e.seq for e in self.edits), default=-1)


@dataclass(frozen=True, slots=True)
class GateResult:
    """Evidence Gate 的判定结果；`terminal` 表示继续尝试也不可能成功。"""

    #: 当前是否满足证据要求。
    passed: bool
    #: 最终运行结果使用的稳定机器可读原因。
    reason_code: str
    #: 适合 UI 展示的人类可读说明。
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
    latest_diff = max(fresh_diffs, key=lambda evidence: evidence.seq)
    if latest_diff.files_changed == 0:
        return GateResult(
            False,
            "missing_diff",
            "Files were edited, but the latest diff has no net workspace changes.",
        )

    fresh_checks = [c for c in ledger.checks if c.seq > last_write]
    if not fresh_checks:
        return GateResult(
            False,
            "missing_check",
            "Files were edited but no verification ran after the last write.",
        )

    latest_by_recipe: dict[str, CheckEvidence] = {}
    for check in sorted(fresh_checks, key=lambda evidence: evidence.seq):
        latest_by_recipe[check.recipe_id] = check
    failing = [check for check in latest_by_recipe.values() if check.exit_code != 0]
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
