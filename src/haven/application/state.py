"""Mutable per-run working state owned by the application layer.

This is *State*: what the run knows. It is not the Context (what the model
sees this turn), not the Trace (what the journal recorded), and not the
ModelResult (what the model just returned).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from haven.contracts.model import ModelMessage
from haven.contracts.tools import PlanStep
from haven.domain.budget import Budget, BudgetUsage
from haven.domain.enums import PermissionMode, RunStatus
from haven.domain.evidence import EvidenceLedger
from haven.domain.ids import RunId
from haven.domain.transitions import transition


@dataclass(slots=True)
class RunContext:
    run_id: RunId
    goal: str
    mode: PermissionMode
    budget: Budget
    usage: BudgetUsage = field(default_factory=BudgetUsage)
    status: RunStatus = RunStatus.CREATED
    transcript: list[ModelMessage] = field(default_factory=list)
    ledger: EvidenceLedger = field(default_factory=EvidenceLedger)
    #: normalized path -> content digest at the last read in this run
    files_read: dict[str, str] = field(default_factory=dict)
    #: The agent's current plan. Lives in State, not the transcript, so it is
    #: re-rendered into Context every turn and cannot be truncated away.
    plan: tuple[PlanStep, ...] = ()
    nudges: int = 0
    last_seq: int = 0

    def move_to(self, target: RunStatus) -> None:
        self.status = transition(self.status, target)
