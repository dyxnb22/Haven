"""版本化的检查点模式。

检查点是用于快速恢复的快照；事件日志仍然是审计权威。模式版本或校验和不匹配
时，加载会失败并关闭。
"""

from __future__ import annotations

from pydantic import Field

from haven.contracts.base import StrictModel
from haven.contracts.model import ModelMessage
from haven.contracts.tools import PlanStep
from haven.domain.budget import Budget, BudgetUsage
from haven.domain.digest import sha256_text
from haven.domain.evidence import (
    CheckEvidence,
    DiffEvidence,
    EditEvidence,
    EvidenceLedger,
)

CHECKPOINT_SCHEMA_VERSION = 1


class BudgetSnapshot(StrictModel):
    max_steps: int
    max_tool_calls: int
    max_wall_time_seconds: float
    max_input_tokens: int
    max_output_tokens: int
    max_cost_usd: float

    @classmethod
    def from_domain(cls, budget: Budget) -> BudgetSnapshot:
        return cls(
            max_steps=budget.max_steps,
            max_tool_calls=budget.max_tool_calls,
            max_wall_time_seconds=budget.max_wall_time_seconds,
            max_input_tokens=budget.max_input_tokens,
            max_output_tokens=budget.max_output_tokens,
            max_cost_usd=budget.max_cost_usd,
        )

    def to_domain(self) -> Budget:
        return Budget(
            max_steps=self.max_steps,
            max_tool_calls=self.max_tool_calls,
            max_wall_time_seconds=self.max_wall_time_seconds,
            max_input_tokens=self.max_input_tokens,
            max_output_tokens=self.max_output_tokens,
            max_cost_usd=self.max_cost_usd,
        )


class UsageSnapshot(StrictModel):
    steps: int
    tool_calls: int
    wall_time_seconds: float
    input_tokens: int
    output_tokens: int
    cost_usd: float
    usage_estimated: bool
    cached_input_tokens: int = 0

    @classmethod
    def from_domain(cls, usage: BudgetUsage) -> UsageSnapshot:
        return cls(
            steps=usage.steps,
            tool_calls=usage.tool_calls,
            wall_time_seconds=usage.wall_time_seconds,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=usage.cost_usd,
            usage_estimated=usage.usage_estimated,
            cached_input_tokens=usage.cached_input_tokens,
        )

    def to_domain(self) -> BudgetUsage:
        return BudgetUsage(
            steps=self.steps,
            tool_calls=self.tool_calls,
            wall_time_seconds=self.wall_time_seconds,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cost_usd=self.cost_usd,
            usage_estimated=self.usage_estimated,
            cached_input_tokens=self.cached_input_tokens,
        )


class EditEvidenceSnapshot(StrictModel):
    seq: int
    path: str
    preimage_digest: str
    postimage_digest: str


class CheckEvidenceSnapshot(StrictModel):
    seq: int
    recipe_id: str
    exit_code: int
    duration_ms: int
    truncated: bool


class DiffEvidenceSnapshot(StrictModel):
    seq: int
    files_changed: int
    insertions: int
    deletions: int
    diff_digest: str


class EvidenceSnapshot(StrictModel):
    edits: tuple[EditEvidenceSnapshot, ...] = ()
    checks: tuple[CheckEvidenceSnapshot, ...] = ()
    diffs: tuple[DiffEvidenceSnapshot, ...] = ()

    @classmethod
    def from_domain(cls, ledger: EvidenceLedger) -> EvidenceSnapshot:
        return cls(
            edits=tuple(
                EditEvidenceSnapshot(
                    seq=e.seq,
                    path=e.path,
                    preimage_digest=e.preimage_digest,
                    postimage_digest=e.postimage_digest,
                )
                for e in ledger.edits
            ),
            checks=tuple(
                CheckEvidenceSnapshot(
                    seq=c.seq,
                    recipe_id=c.recipe_id,
                    exit_code=c.exit_code,
                    duration_ms=c.duration_ms,
                    truncated=c.truncated,
                )
                for c in ledger.checks
            ),
            diffs=tuple(
                DiffEvidenceSnapshot(
                    seq=d.seq,
                    files_changed=d.files_changed,
                    insertions=d.insertions,
                    deletions=d.deletions,
                    diff_digest=d.diff_digest,
                )
                for d in ledger.diffs
            ),
        )

    def to_domain(self) -> EvidenceLedger:
        return EvidenceLedger(
            edits=tuple(
                EditEvidence(
                    seq=e.seq,
                    path=e.path,
                    preimage_digest=e.preimage_digest,
                    postimage_digest=e.postimage_digest,
                )
                for e in self.edits
            ),
            checks=tuple(
                CheckEvidence(
                    seq=c.seq,
                    recipe_id=c.recipe_id,
                    exit_code=c.exit_code,
                    duration_ms=c.duration_ms,
                    truncated=c.truncated,
                )
                for c in self.checks
            ),
            diffs=tuple(
                DiffEvidence(
                    seq=d.seq,
                    files_changed=d.files_changed,
                    insertions=d.insertions,
                    deletions=d.deletions,
                    diff_digest=d.diff_digest,
                )
                for d in self.diffs
            ),
        )


class CheckpointV1(StrictModel):
    schema_version: int = CHECKPOINT_SCHEMA_VERSION
    run_id: str
    workspace_digest: str
    goal: str
    mode: str
    status: str
    last_seq: int
    budget: BudgetSnapshot
    usage: UsageSnapshot
    messages: tuple[ModelMessage, ...]
    evidence: EvidenceSnapshot = EvidenceSnapshot()
    files_read: dict[str, str] = Field(default_factory=dict)
    plan: tuple[PlanStep, ...] = ()
    original_artifacts: dict[str, str] = Field(
        default_factory=dict,
        description="path -> artifact digest of the file content before this run's first edit",
    )

    def checksum(self) -> str:
        return sha256_text(self.model_dump_json())
