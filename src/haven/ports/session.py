"""Session store port: transactional persistence for runs, events, approvals,
executions, checkpoints, and artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from haven.contracts.checkpoint import CheckpointV1
from haven.contracts.events import ApplicationEvent, EventEnvelope
from haven.domain.enums import ApprovalDecision, EffectState, RunStatus


@dataclass(frozen=True, slots=True)
class RunRecord:
    run_id: str
    workspace: str
    workspace_digest: str
    goal: str
    mode: str
    status: RunStatus
    stop_reason: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class ExecutionRecord:
    call_id: str
    run_id: str
    ticket_digest: str
    tool_name: str
    effect_state: EffectState
    preimage_digest: str
    #: For writes this is recorded at STARTED time as the *expected* postimage
    #: (the preview computes it before any byte lands), then confirmed with the
    #: actual digest on completion. Recording it up front is what lets recovery
    #: classify a crash in the written-but-not-yet-journaled window.
    postimage_digest: str
    path: str
    #: repo.move only: the destination, so recovery can inspect both ends.
    dest_path: str = ""


class SessionStorePort(Protocol):
    async def create_run(
        self, run_id: str, workspace: str, workspace_digest: str, goal: str, mode: str
    ) -> None: ...

    async def update_run_status(self, run_id: str, status: RunStatus, stop_reason: str) -> None: ...

    async def get_run(self, run_id: str) -> RunRecord | None: ...

    async def list_runs(self, limit: int) -> list[RunRecord]: ...

    async def append_event(self, run_id: str, event: ApplicationEvent) -> EventEnvelope: ...

    async def load_events(self, run_id: str) -> list[EventEnvelope]: ...

    async def save_checkpoint(self, checkpoint: CheckpointV1) -> None: ...

    async def load_checkpoint(self, run_id: str) -> CheckpointV1 | None: ...

    async def record_approval(self, approval_id: str, run_id: str, request_digest: str) -> None: ...

    async def decide_approval(self, approval_id: str, decision: ApprovalDecision) -> None: ...

    async def consume_approval(self, approval_id: str, request_digest: str) -> bool: ...

    async def record_execution(self, record: ExecutionRecord) -> None: ...

    async def update_execution_state(
        self, call_id: str, effect_state: EffectState, postimage_digest: str = ""
    ) -> None: ...

    async def load_executions(self, run_id: str) -> list[ExecutionRecord]: ...

    async def put_artifact(self, content: bytes) -> str: ...

    async def get_artifact(self, digest: str) -> bytes | None: ...

    # -- maintenance (haven gc) ------------------------------------------------

    async def delete_run(self, run_id: str) -> None:
        """Remove one run and everything recorded under it: events,
        checkpoints, approvals, and execution journal rows. Artifacts are
        content-addressed and possibly shared, so they are swept separately
        against the set still referenced by surviving checkpoints."""
        ...

    async def list_artifacts(self) -> list[str]: ...

    async def delete_artifact(self, digest: str) -> None: ...

    async def close(self) -> None: ...
