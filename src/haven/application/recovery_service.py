"""恢复：继续中断的运行，但不重放有歧义的副作用。

检查点提供快速状态；执行日志提供副作用事实。已经开始但从未确认的编辑，会通过比较
文件当前摘要与记录的 preimage/postimage 来分类。任何无法证明的情况都属于 EFFECT_UNKNOWN，
需要人工明确调和——绝不自动重放。
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
    """用户级撤销一次运行文件变更的结果。"""

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

        # 对已开始但从未确认的副作用进行分类。
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
            # create 没有 preimage；目标不存在就是它从未执行的证明。预期
            # postimage 会在 STARTED 时写入日志，因此存在且匹配该内容的文件
            # 可以证明操作已完成。
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
            # move 不会改变内容，因此已批准的 preimage 可以识别两端的文件。
            # 只有“副本已落地但源文件仍在”的窗口真正存在歧义（完成它会变成
            # 重放操作）。
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
            # 进程（repo.check、repo.exec）可能已经运行，也可能没有；没有摘要
            # 能证明是哪一种情况，因此它们保持 unknown 并阻止恢复。
        return EffectFinding(
            call_id,
            tool_name,
            path,
            "unknown",
            "process may have run; confirm manually",
        )

    async def rewind(self, run_id: str) -> RewindReport:
        """用户级撤销：将本次运行修改过的每个文件恢复为运行前内容；凡是无法证明磁盘状态
        是该运行自身产物的地方都拒绝操作。

        每条路径的安全规则是：磁盘上的文件必须仍然匹配该运行最后记录的摘要（最后一次
        编辑的 postimage）。之后发生过变化的文件——无论是外部编辑还是后续运行——都会
        阻塞该路径，而不是被强行覆盖；rewind 是补偿操作，绝不是盲目重放（reconcile
        也遵循相同立场）。
        """
        checkpoint = await self._store.load_checkpoint(run_id)
        if checkpoint is None:
            return RewindReport(run_id=run_id, blockers=("no checkpoint recorded for this run",))
        if checkpoint.workspace_digest != self._workspace.workspace_digest:
            return RewindReport(
                run_id=run_id,
                blockers=("workspace identity changed since the run; refusing to rewind",),
            )

        # 本次运行对每条路径的最终结果：最后一次 edit 的 postimage
        # （"" 表示运行删除了它）。运行创建的路径，是第一次 edit 没有
        # preimage 的路径。
        final_digest: dict[str, str] = {}
        first_preimage: dict[str, str] = {}
        for edit in checkpoint.evidence.edits:
            final_digest[edit.path] = edit.postimage_digest
            first_preimage.setdefault(edit.path, edit.preimage_digest)

        blocked: list[str] = []
        planned: list[tuple[str, str | None]] = []  # （path，恢复内容 | None=delete）
        for path, artifact_digest in sorted(checkpoint.original_artifacts.items()):
            expected = final_digest.get(path)
            facts = self._workspace.path_facts(path)
            if expected == "":
                # 运行删除了它；此时它必须仍然不存在。
                if facts.digest is not None:
                    blocked.append(f"{path}: reappeared since the run deleted it")
                    continue
            elif expected is None or facts.digest != expected:
                blocked.append(f"{path}: changed since this run; refusing to overwrite")
                continue
            if first_preimage.get(path) == "":
                planned.append((path, None))  # 由运行创建 -> 删除
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
        """重建运行状态，并恢复运行级原始内容以便生成 diff。"""
        originals: dict[str, str] = {}
        for path, digest in checkpoint.original_artifacts.items():
            content = await self._store.get_artifact(digest)
            if content is not None:
                originals[path] = content.decode("utf-8")
        self._workspace.restore_originals(originals)
        return build_run_context_from_checkpoint(checkpoint)
