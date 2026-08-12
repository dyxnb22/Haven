"""Recovery: resume interrupted runs without replaying ambiguous side effects.

The checkpoint gives fast state; the execution journal gives effect truth.
An edit that started but was never confirmed is classified by comparing the
file's current digest against the recorded preimage/postimage. Anything that
cannot be proven is EFFECT_UNKNOWN and requires explicit human reconciliation
— never an automatic replay.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from haven.application.run_service import build_run_context_from_checkpoint
from haven.application.state import RunContext
from haven.contracts.checkpoint import CheckpointV1
from haven.domain.enums import ACTIVE_STATUSES, EffectState, RunStatus
from haven.ports.session import SessionStorePort
from haven.ports.workspace import WorkspacePort

Classification = Literal["not_run", "confirmed", "unknown"]


@dataclass(frozen=True, slots=True)
class EffectFinding:
    call_id: str
    tool_name: str
    path: str
    classification: Classification
    detail: str


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    run_id: str
    can_resume: bool
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    findings: tuple[EffectFinding, ...] = ()
    checkpoint: CheckpointV1 | None = None


class RecoveryService:
    def __init__(self, store: SessionStorePort, workspace: WorkspacePort) -> None:
        self._store = store
        self._workspace = workspace

    async def inspect(self, run_id: str) -> RecoveryReport:
        blockers: list[str] = []
        warnings: list[str] = []
        findings: list[EffectFinding] = []

        run = await self._store.get_run(run_id)
        if run is None:
            return RecoveryReport(run_id=run_id, can_resume=False, blockers=("run not found",))
        if run.status not in ACTIVE_STATUSES and run.status is not RunStatus.EFFECT_UNKNOWN:
            blockers.append(f"run already finished with status {run.status.value!r}")

        checkpoint = await self._store.load_checkpoint(run_id)
        if checkpoint is None:
            blockers.append("no checkpoint recorded for this run")
            return RecoveryReport(run_id=run_id, can_resume=False, blockers=tuple(blockers))

        if checkpoint.workspace_digest != self._workspace.workspace_digest:
            blockers.append("workspace identity changed since the checkpoint; refusing to resume")

        # Classify effects that were started but never confirmed.
        for record in await self._store.load_executions(run_id):
            if record.effect_state not in (EffectState.STARTED, EffectState.EFFECT_UNKNOWN):
                continue
            finding = self._classify(
                record.call_id,
                record.tool_name,
                record.path,
                record.preimage_digest,
                record.postimage_digest,
            )
            findings.append(finding)
            if finding.classification == "not_run":
                await self._store.update_execution_state(
                    record.call_id, EffectState.RECONCILED_NOT_RUN
                )
            elif finding.classification == "confirmed":
                await self._store.update_execution_state(
                    record.call_id, EffectState.RECONCILED_CONFIRMED
                )
            else:
                blockers.append(
                    f"effect of {record.tool_name} (call {record.call_id}) is unknown; "
                    "reconcile it explicitly before resuming"
                )

        for path, digest in checkpoint.files_read.items():
            facts = self._workspace.path_facts(path)
            if facts.digest != digest:
                warnings.append(
                    f"{path} changed since the checkpoint; stale edits will fail closed"
                )

        return RecoveryReport(
            run_id=run_id,
            can_resume=not blockers,
            blockers=tuple(blockers),
            warnings=tuple(warnings),
            findings=tuple(findings),
            checkpoint=checkpoint,
        )

    def _classify(
        self, call_id: str, tool_name: str, path: str, preimage: str, postimage: str
    ) -> EffectFinding:
        if tool_name == "repo.edit" and path:
            facts = self._workspace.path_facts(path)
            if facts.digest is not None and facts.digest == preimage:
                return EffectFinding(
                    call_id,
                    tool_name,
                    path,
                    "not_run",
                    "file still matches the preimage; the edit never happened",
                )
            if postimage and facts.digest == postimage:
                return EffectFinding(
                    call_id,
                    tool_name,
                    path,
                    "confirmed",
                    "file matches the recorded postimage; the edit completed",
                )
            return EffectFinding(
                call_id,
                tool_name,
                path,
                "unknown",
                "file matches neither preimage nor postimage",
            )
        # Processes (repo.check, repo.exec) may or may not have run; there is no
        # digest to prove it either way, so they stay unknown and block resume.
        return EffectFinding(
            call_id,
            tool_name,
            path,
            "unknown",
            "process may have run; confirm manually",
        )

    async def reconcile(
        self, run_id: str, call_id: str, resolution: Literal["confirmed", "not_run", "abandon"]
    ) -> None:
        if resolution == "abandon":
            await self._store.update_execution_state(call_id, EffectState.ABANDONED)
            await self._store.update_run_status(run_id, RunStatus.FAILED, "abandoned")
            return
        state = (
            EffectState.RECONCILED_CONFIRMED
            if resolution == "confirmed"
            else EffectState.RECONCILED_NOT_RUN
        )
        await self._store.update_execution_state(call_id, state)

    async def build_context(self, checkpoint: CheckpointV1) -> RunContext:
        """Rebuild run state and restore run-scoped originals for diffing."""
        originals: dict[str, str] = {}
        for path, digest in checkpoint.original_artifacts.items():
            content = await self._store.get_artifact(digest)
            if content is not None:
                originals[path] = content.decode("utf-8")
        self._workspace.restore_originals(originals)
        return build_run_context_from_checkpoint(checkpoint)
