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
from haven.ports.session import ArtifactError, ExecutionRecord, SessionStorePort
from haven.ports.workspace import WorkspacePort

Classification = Literal["not_run", "confirmed", "unknown"]


@dataclass(frozen=True, slots=True)
class EffectFinding:
    """恢复检查对一项未确认副作用作出的分类。"""

    #: 正在调和的执行日志标识。
    call_id: str
    #: 与潜在副作用关联的工具形态。
    tool_name: str
    #: 已知时记录受影响的规范化路径。
    path: str
    #: 对副作用是否发生的确定性分类。
    classification: Classification
    #: 支持该分类的人类可读证据。
    detail: str


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    """恢复检查报告；包含阻塞原因和可疑副作用明细。"""

    #: 正在检查是否可以安全继续的运行。
    run_id: str
    #: 所有阻塞项都已清除、允许恢复时为 True。
    can_resume: bool
    #: 恢复前必须解决的条件。
    blockers: tuple[str, ...] = ()
    #: 不阻止恢复的过期读取或环境警告。
    warnings: tuple[str, ...] = ()
    #: 尚未确认的副作用及其分类。
    findings: tuple[EffectFinding, ...] = ()
    #: 用于构造恢复后 RunContext 的检查点。
    checkpoint: CheckpointV1 | None = None


@dataclass(frozen=True, slots=True)
class RewindReport:
    """用户级撤销一次运行文件变更的结果。"""

    #: 正在尝试撤销其文件副作用的运行。
    run_id: str
    #: 所有请求的撤销都成功完成时为 True。
    rewound: bool = False
    #: 已恢复为运行前内容的原有文件。
    restored: tuple[str, ...] = ()
    #: 由运行创建、并在 rewind 期间删除的文件。
    deleted: tuple[str, ...] = ()
    #: 无法安全撤销的路径。
    blockers: tuple[str, ...] = ()


class RecoveryService:
    """检查中断运行是否可安全继续，并提供明确的文件撤销操作。"""

    def __init__(self, store: SessionStorePort, workspace: WorkspacePort) -> None:
        self._store = store
        self._workspace = workspace

    async def inspect(self, run_id: str) -> RecoveryReport:
        """比较检查点、执行日志和当前文件，拒绝重放无法证明的副作用。"""
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

        first_preimage = {
            edit.path: edit.preimage_digest for edit in reversed(checkpoint.evidence.edits)
        }
        for path, digest in checkpoint.original_artifacts.items():
            # 新检查点用空摘要明确表示“运行前不存在”；旧检查点曾把这种情况
            # 保存为空内容构件，可由第一条编辑的空 preimage 无歧义识别。
            if not digest or first_preimage.get(path) == "":
                continue
            try:
                content = await self._store.get_artifact(digest)
                if content is None:
                    blockers.append(f"original artifact for {path} is missing")
                else:
                    content.decode("utf-8")
            except (ArtifactError, OSError, UnicodeDecodeError, ValueError) as exc:
                blockers.append(f"original artifact for {path} is unreadable: {exc}")

        # 对已开始但从未确认的副作用进行分类。
        for record in await self._store.load_executions(run_id):
            if record.effect_state not in (EffectState.STARTED, EffectState.EFFECT_UNKNOWN):
                continue
            finding = self._classify(record)
            findings.append(finding)
            if finding.classification == "not_run":
                await self._store.update_execution_state(
                    run_id, record.call_id, EffectState.RECONCILED_NOT_RUN
                )
            elif finding.classification == "confirmed":
                await self._store.update_execution_state(
                    run_id, record.call_id, EffectState.RECONCILED_CONFIRMED
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
            if not facts.exists:
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
            if not facts.exists:
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
            if src.is_file and src.digest == preimage and not dest.exists:
                return EffectFinding(
                    call_id,
                    tool_name,
                    path,
                    "not_run",
                    "source intact and destination absent; the move never happened",
                )
            if not src.exists and dest.is_file and dest.digest == preimage:
                return EffectFinding(
                    call_id,
                    tool_name,
                    path,
                    "confirmed",
                    "destination holds the approved content and source is gone; the move completed",
                )
            if src.is_file and dest.is_file and src.digest == preimage and dest.digest == preimage:
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
                if facts.exists:
                    blocked.append(f"{path}: reappeared since the run deleted it")
                    continue
            elif expected is None or facts.digest != expected:
                blocked.append(f"{path}: changed since this run; refusing to overwrite")
                continue
            if not artifact_digest or first_preimage.get(path) == "":
                planned.append((path, None))  # 由运行创建 -> 删除
            else:
                artifact = await self._store.get_artifact(artifact_digest)
                if artifact is None:
                    blocked.append(f"{path}: original content is not in the artifact store")
                    continue
                try:
                    planned.append((path, artifact.decode("utf-8")))
                except UnicodeDecodeError:
                    blocked.append(f"{path}: original artifact is not UTF-8 text")

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
        """接受人工对未知副作用的调和决定。"""
        records = await self._store.load_executions(run_id)
        if not any(record.call_id == call_id for record in records):
            raise ValueError(f"execution {call_id!r} does not belong to run {run_id!r}")
        if resolution == "abandon":
            await self._store.update_execution_state(run_id, call_id, EffectState.ABANDONED)
            await self._store.update_run_status(run_id, RunStatus.FAILED, "abandoned")
            return
        state = (
            EffectState.RECONCILED_CONFIRMED
            if resolution == "confirmed"
            else EffectState.RECONCILED_NOT_RUN
        )
        await self._store.update_execution_state(run_id, call_id, state)

    async def build_context(self, checkpoint: CheckpointV1) -> RunContext:
        """重建运行状态，并恢复运行级原始内容以便生成 diff。"""
        originals: dict[str, str | None] = {}
        first_preimage = {
            edit.path: edit.preimage_digest for edit in reversed(checkpoint.evidence.edits)
        }
        for path, digest in checkpoint.original_artifacts.items():
            if not digest or first_preimage.get(path) == "":
                originals[path] = None
                continue
            content = await self._store.get_artifact(digest)
            if content is None:
                raise ValueError(f"original artifact for {path!r} is missing")
            try:
                originals[path] = content.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError(f"original artifact for {path!r} is not UTF-8") from exc
        self._workspace.restore_originals(originals)
        return build_run_context_from_checkpoint(checkpoint)
