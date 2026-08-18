"""审计式文件变更工具及其恢复账本记录。"""

from __future__ import annotations

from haven.application.approval_cards import ToolPreview
from haven.application.emitter import EventEmitter
from haven.application.state import RunContext
from haven.application.tool_execution_types import (
    MODEL_PAYLOAD_CHARS,
    ExecuteHandler,
    ToolExecution,
    clip,
    error_result,
    map_workspace_error,
    ok_result,
)
from haven.contracts.events import EvidenceRecorded
from haven.contracts.model import ToolCallProposal
from haven.contracts.tools import (
    RepoApplyPatchArgs,
    RepoCreateArgs,
    RepoDeleteArgs,
    RepoEditArgs,
    RepoMoveArgs,
    ToolArgs,
)
from haven.domain.enums import EffectState, ToolErrorCode
from haven.domain.evidence import EditEvidence
from haven.ports.session import ExecutionRecord, SessionStorePort
from haven.ports.workspace import (
    EditPreview,
    PatchPreview,
    PatchRollbackError,
    WorkspaceError,
    WorkspacePort,
)


class MutationToolExecutor:
    """执行写、删、移动和事务补丁，并维护逐副作用恢复记录。"""

    def __init__(
        self, workspace: WorkspacePort, store: SessionStorePort, emitter: EventEmitter
    ) -> None:
        self._workspace = workspace
        self._store = store
        self._emitter = emitter
        self.handlers: dict[str, ExecuteHandler] = {
            "repo.edit": self.execute_write,
            "repo.create": self.execute_write,
            "repo.delete": self.execute_delete,
            "repo.move": self.execute_move,
            "repo.apply_patch": self.execute_patch,
        }

    async def execute_write(
        self,
        ctx: RunContext,
        call: ToolCallProposal,
        args: ToolArgs,
        ticket_digest: str,
        preview: ToolPreview,
    ) -> ToolExecution:
        assert isinstance(args, RepoEditArgs | RepoCreateArgs)
        assert isinstance(preview, EditPreview)
        await self._store.record_execution(
            ExecutionRecord(
                call_id=call.call_id,
                run_id=ctx.run_id,
                ticket_digest=ticket_digest,
                tool_name=call.tool_name,
                effect_state=EffectState.STARTED,
                preimage_digest=preview.preimage_digest,
                postimage_digest=preview.postimage_digest,
                path=preview.path,
            )
        )
        try:
            if isinstance(args, RepoCreateArgs):
                outcome = await self._workspace.apply_create(args.path, args.content)
            else:
                outcome = await self._workspace.apply_edit(
                    args.path,
                    args.old_string,
                    args.new_string,
                    preview.preimage_digest,
                    occurrence=args.occurrence,
                    replace_all=args.replace_all,
                )
        except WorkspaceError as exc:
            await self._store.update_execution_state(call.call_id, EffectState.FAILED)
            return ToolExecution(error_result(call, map_workspace_error(exc), str(exc)))
        except BaseException:
            await self._store.update_execution_state(call.call_id, EffectState.EFFECT_UNKNOWN)
            raise

        await self._store.update_execution_state(
            call.call_id, EffectState.CONFIRMED, outcome.postimage_digest
        )
        ctx.files_read[outcome.path] = outcome.postimage_digest
        verb = "created" if isinstance(args, RepoCreateArgs) else "edited"
        envelope = await self._emitter.emit(
            ctx.run_id,
            EvidenceRecorded(
                run_id=ctx.run_id,
                evidence_kind="edit",
                summary=f"{verb} {outcome.path}: postimage {outcome.postimage_digest[:12]}",
            ),
        )
        ctx.ledger = ctx.ledger.with_edit(
            EditEvidence(
                seq=envelope.seq,
                path=outcome.path,
                preimage_digest=outcome.preimage_digest,
                postimage_digest=outcome.postimage_digest,
            )
        )
        return ToolExecution(
            ok_result(
                call,
                {
                    "path": outcome.path,
                    "applied": True,
                    "postimage_digest": outcome.postimage_digest,
                    "diff": clip(preview.diff, MODEL_PAYLOAD_CHARS),
                },
            )
        )

    async def execute_delete(
        self,
        ctx: RunContext,
        call: ToolCallProposal,
        args: ToolArgs,
        ticket_digest: str,
        preview: ToolPreview,
    ) -> ToolExecution:
        assert isinstance(args, RepoDeleteArgs)
        assert isinstance(preview, EditPreview)
        await self._store.record_execution(
            ExecutionRecord(
                call_id=call.call_id,
                run_id=ctx.run_id,
                ticket_digest=ticket_digest,
                tool_name=call.tool_name,
                effect_state=EffectState.STARTED,
                preimage_digest=preview.preimage_digest,
                postimage_digest="",
                path=args.path,
            )
        )
        try:
            outcome = await self._workspace.apply_delete(args.path, preview.preimage_digest)
        except WorkspaceError as exc:
            await self._store.update_execution_state(call.call_id, EffectState.FAILED)
            return ToolExecution(error_result(call, map_workspace_error(exc), str(exc)))
        except BaseException:
            await self._store.update_execution_state(call.call_id, EffectState.EFFECT_UNKNOWN)
            raise

        await self._store.update_execution_state(call.call_id, EffectState.CONFIRMED)
        ctx.files_read.pop(outcome.path, None)
        envelope = await self._emitter.emit(
            ctx.run_id,
            EvidenceRecorded(
                run_id=ctx.run_id, evidence_kind="edit", summary=f"deleted {outcome.path}"
            ),
        )
        ctx.ledger = ctx.ledger.with_edit(
            EditEvidence(
                seq=envelope.seq,
                path=outcome.path,
                preimage_digest=outcome.preimage_digest,
                postimage_digest="",
            )
        )
        return ToolExecution(ok_result(call, {"path": outcome.path, "deleted": True}))

    async def execute_move(
        self,
        ctx: RunContext,
        call: ToolCallProposal,
        args: ToolArgs,
        ticket_digest: str,
        preview: ToolPreview,
    ) -> ToolExecution:
        assert isinstance(args, RepoMoveArgs)
        assert isinstance(preview, EditPreview)
        await self._store.record_execution(
            ExecutionRecord(
                call_id=call.call_id,
                run_id=ctx.run_id,
                ticket_digest=ticket_digest,
                tool_name=call.tool_name,
                effect_state=EffectState.STARTED,
                preimage_digest=preview.preimage_digest,
                postimage_digest="",
                path=args.src,
                dest_path=args.dest,
            )
        )
        try:
            removal, addition = await self._workspace.apply_move(
                args.src, args.dest, preview.preimage_digest
            )
        except WorkspaceError as exc:
            await self._store.update_execution_state(call.call_id, EffectState.FAILED)
            return ToolExecution(error_result(call, map_workspace_error(exc), str(exc)))
        except BaseException:
            await self._store.update_execution_state(call.call_id, EffectState.EFFECT_UNKNOWN)
            raise

        await self._store.update_execution_state(
            call.call_id, EffectState.CONFIRMED, addition.postimage_digest
        )
        ctx.files_read.pop(removal.path, None)
        ctx.files_read[addition.path] = addition.postimage_digest
        envelope = await self._emitter.emit(
            ctx.run_id,
            EvidenceRecorded(
                run_id=ctx.run_id,
                evidence_kind="edit",
                summary=f"moved {removal.path} -> {addition.path}",
            ),
        )
        ctx.ledger = ctx.ledger.with_edit(
            EditEvidence(
                seq=envelope.seq,
                path=removal.path,
                preimage_digest=removal.preimage_digest,
                postimage_digest="",
            )
        ).with_edit(
            EditEvidence(
                seq=envelope.seq,
                path=addition.path,
                preimage_digest="",
                postimage_digest=addition.postimage_digest,
            )
        )
        return ToolExecution(
            ok_result(call, {"src": removal.path, "dest": addition.path, "moved": True})
        )

    async def execute_patch(
        self,
        ctx: RunContext,
        call: ToolCallProposal,
        args: ToolArgs,
        ticket_digest: str,
        preview: ToolPreview,
    ) -> ToolExecution:
        assert isinstance(args, RepoApplyPatchArgs)
        assert isinstance(preview, PatchPreview)
        for index, effect in enumerate(preview.effects):
            await self._store.record_execution(
                ExecutionRecord(
                    call_id=f"{call.call_id}#{index}",
                    run_id=ctx.run_id,
                    ticket_digest=ticket_digest,
                    tool_name=effect.tool_shape,
                    effect_state=EffectState.STARTED,
                    preimage_digest=effect.preimage_digest,
                    postimage_digest=effect.expected_postimage,
                    path=effect.path,
                )
            )

        async def mark_all(state: EffectState) -> None:
            for index in range(len(preview.effects)):
                await self._store.update_execution_state(f"{call.call_id}#{index}", state)

        try:
            outcomes = await self._workspace.apply_patch(preview)
        except WorkspaceError as exc:
            await mark_all(EffectState.FAILED)
            return ToolExecution(error_result(call, map_workspace_error(exc), str(exc)))
        except PatchRollbackError as exc:
            await mark_all(EffectState.EFFECT_UNKNOWN)
            return ToolExecution(
                error_result(call, ToolErrorCode.INTERNAL, str(exc)), effect_unknown=True
            )
        except BaseException:
            await mark_all(EffectState.EFFECT_UNKNOWN)
            raise

        by_path = {outcome.path: outcome for outcome in outcomes}
        for index, effect in enumerate(preview.effects):
            await self._store.update_execution_state(
                f"{call.call_id}#{index}",
                EffectState.CONFIRMED,
                by_path[effect.path].postimage_digest,
            )

        envelope = await self._emitter.emit(
            ctx.run_id,
            EvidenceRecorded(
                run_id=ctx.run_id,
                evidence_kind="edit",
                summary=(
                    f"patch: {len(outcomes)} file(s), +{preview.insertions} -{preview.deletions}"
                ),
            ),
        )
        for outcome in outcomes:
            ctx.ledger = ctx.ledger.with_edit(
                EditEvidence(
                    seq=envelope.seq,
                    path=outcome.path,
                    preimage_digest=outcome.preimage_digest,
                    postimage_digest=outcome.postimage_digest,
                )
            )
            if outcome.postimage_digest:
                ctx.files_read[outcome.path] = outcome.postimage_digest
            else:
                ctx.files_read.pop(outcome.path, None)

        return ToolExecution(
            ok_result(
                call,
                {
                    "applied": True,
                    "files_changed": len(outcomes),
                    "insertions": preview.insertions,
                    "deletions": preview.deletions,
                    "files": [outcome.path for outcome in outcomes],
                    "diff": clip(preview.diff, MODEL_PAYLOAD_CHARS),
                },
            )
        )
