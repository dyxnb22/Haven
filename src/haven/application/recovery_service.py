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
from haven.ports.session import ExecutionRecord, SessionStorePort
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


@dataclass(frozen=True, slots=True)
class RewindReport:
    """The outcome of a user-level undo of one run's file changes."""

    run_id: str
    rewound: bool = False
    restored: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()


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
            finding = self._classify(record)
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

    def _classify(self, record: ExecutionRecord) -> EffectFinding:
        call_id = record.call_id
        tool_name = record.tool_name
        path = record.path
        preimage = record.preimage_digest
        postimage = record.postimage_digest
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
        if tool_name == "repo.create" and path:
            # No preimage exists for a create; absence is the proof it never
            # ran. The expected postimage is journaled at STARTED time, so a
            # present file that matches it is proven complete.
            facts = self._workspace.path_facts(path)
            if facts.digest is None:
                return EffectFinding(
                    call_id,
                    tool_name,
                    path,
                    "not_run",
                    "file does not exist; the create never happened",
                )
            if postimage and facts.digest == postimage:
                return EffectFinding(
                    call_id,
                    tool_name,
                    path,
                    "confirmed",
                    "file matches the expected postimage; the create completed",
                )
            return EffectFinding(
                call_id,
                tool_name,
                path,
                "unknown",
                "a file exists but does not match the expected postimage",
            )
        if tool_name == "repo.delete" and path:
            facts = self._workspace.path_facts(path)
            if facts.digest is None:
                return EffectFinding(
                    call_id,
                    tool_name,
                    path,
                    "confirmed",
                    "file is gone; the delete completed",
                )
            if facts.digest == preimage:
                return EffectFinding(
                    call_id,
                    tool_name,
                    path,
                    "not_run",
                    "file still matches the approved preimage; the delete never happened",
                )
            return EffectFinding(
                call_id,
                tool_name,
                path,
                "unknown",
                "file exists but does not match the approved preimage",
            )
        if tool_name == "repo.move" and path and record.dest_path:
            # A move never changes content, so the approved preimage identifies
            # the file at either end. Only the copy-landed-but-source-remains
            # window is genuinely ambiguous (completing it would be a replay).
            src = self._workspace.path_facts(path)
            dest = self._workspace.path_facts(record.dest_path)
            if src.digest == preimage and dest.digest is None:
                return EffectFinding(
                    call_id,
                    tool_name,
                    path,
                    "not_run",
                    "source intact and destination absent; the move never happened",
                )
            if src.digest is None and dest.digest == preimage:
                return EffectFinding(
                    call_id,
                    tool_name,
                    path,
                    "confirmed",
                    "destination holds the approved content and source is gone; the move completed",
                )
            if src.digest == preimage and dest.digest == preimage:
                return EffectFinding(
                    call_id,
                    tool_name,
                    path,
                    "unknown",
                    "destination was written but the source was not removed; "
                    "reconcile explicitly (auto-completing would replay the unlink)",
                )
            return EffectFinding(
                call_id,
                tool_name,
                path,
                "unknown",
                "neither end matches the approved preimage cleanly",
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

    async def rewind(self, run_id: str) -> RewindReport:
        """User-level undo: restore every file this run changed to its
        pre-run content, refusing wherever disk state is not provably the
        run's own output.

        Safety rule per path: the file on disk must still match the run's
        last recorded digest for it (the postimage of its final edit). A file
        that changed since — an external edit, a later run — blocks that path
        instead of being clobbered; rewind is compensation, never blind
        replay (the same stance reconcile takes).
        """
        checkpoint = await self._store.load_checkpoint(run_id)
        if checkpoint is None:
            return RewindReport(run_id=run_id, blockers=("no checkpoint recorded for this run",))
        if checkpoint.workspace_digest != self._workspace.workspace_digest:
            return RewindReport(
                run_id=run_id,
                blockers=("workspace identity changed since the run; refusing to rewind",),
            )

        # The run's final word on each path: the postimage of its last edit
        # ("" = the run deleted it). Paths created by the run are those whose
        # first edit had no preimage.
        final_digest: dict[str, str] = {}
        first_preimage: dict[str, str] = {}
        for edit in checkpoint.evidence.edits:
            final_digest[edit.path] = edit.postimage_digest
            first_preimage.setdefault(edit.path, edit.preimage_digest)

        blocked: list[str] = []
        planned: list[tuple[str, str | None]] = []  # (path, restore content | None=delete)
        for path, artifact_digest in sorted(checkpoint.original_artifacts.items()):
            expected = final_digest.get(path)
            facts = self._workspace.path_facts(path)
            if expected == "":
                # The run deleted it; it must still be absent.
                if facts.digest is not None:
                    blocked.append(f"{path}: reappeared since the run deleted it")
                    continue
            elif expected is None or facts.digest != expected:
                blocked.append(f"{path}: changed since this run; refusing to overwrite")
                continue
            if first_preimage.get(path) == "":
                planned.append((path, None))  # created by the run -> remove
            else:
                artifact = await self._store.get_artifact(artifact_digest)
                if artifact is None:
                    blocked.append(f"{path}: original content is not in the artifact store")
                    continue
                planned.append((path, artifact.decode("utf-8")))

        if blocked:
            return RewindReport(run_id=run_id, blockers=tuple(blocked))

        restored: list[str] = []
        deleted: list[str] = []
        for path, content in planned:
            target = self._workspace.root / path
            if content is None:
                target.unlink(missing_ok=True)
                deleted.append(path)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                restored.append(path)
        return RewindReport(
            run_id=run_id, restored=tuple(restored), deleted=tuple(deleted), rewound=True
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
