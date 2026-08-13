"""The single tool execution channel.

Every model-proposed action passes, in order:
Registry -> Schema validation -> Workspace facts -> Deterministic policy
-> Exact approval (when asked) -> Execution ticket -> Executor
-> ToolResult + Evidence + Trace.

There is no other path from a model proposal to a side effect.
"""

from __future__ import annotations

import json
import shlex
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from haven.application.approvals import ApprovalResponder
from haven.application.emitter import EventEmitter
from haven.application.registry import ToolRegistry, ValidationFailure
from haven.application.state import RunContext
from haven.contracts.events import (
    ApprovalDecided,
    ApprovalRequested,
    DiffPreview,
    EvidenceRecorded,
    ExecutionStarted,
    Notice,
    PlanStepView,
    PlanUpdated,
    PolicyDecided,
    ToolCompleted,
    ToolProposed,
)
from haven.contracts.model import ToolCallProposal
from haven.contracts.tools import (
    PatchCreateOp,
    PatchDeleteOp,
    PatchEditOp,
    PatchMoveOp,
    RecipeSpec,
    RepoApplyPatchArgs,
    RepoCheckArgs,
    RepoCreateArgs,
    RepoDeleteArgs,
    RepoEditArgs,
    RepoExecArgs,
    RepoListArgs,
    RepoMoveArgs,
    RepoReadArgs,
    RepoSearchArgs,
    TaskPlanArgs,
    ToolArgs,
    ToolResult,
)
from haven.domain.approval import ApprovalRequest, compute_approval_digest
from haven.domain.digest import canonical_json, sha256_text
from haven.domain.enums import (
    ApprovalDecision,
    EffectState,
    PermissionMode,
    PolicyDecision,
    RunStatus,
    ToolErrorCode,
    ToolStatus,
)
from haven.domain.evidence import CheckEvidence, DiffEvidence, EditEvidence
from haven.domain.exec_policy import ExecClass, classify_argv
from haven.domain.ids import ApprovalId, ToolCallId, new_approval_id
from haven.domain.policy import ToolFacts, evaluate_policy
from haven.domain.ticket import mint_ticket
from haven.ports.executor import ExecSpec, ExecutorPort
from haven.ports.sandbox import (
    SandboxLauncher,
    SandboxSpec,
    default_private_roots,
    default_readable_roots,
)
from haven.ports.session import ExecutionRecord, SessionStorePort
from haven.ports.workspace import (
    EditPreview,
    PatchOpSpec,
    PatchPreview,
    PatchRollbackError,
    WorkspaceError,
    WorkspacePort,
    WorkspaceSnapshot,
)

#: What one tool proposal previews as: a single-file diff, a whole patch, or
#: nothing (read-only tools).
ToolPreview = EditPreview | PatchPreview | None

#: Per-tool handler shapes. Both tables are keyed by registered tool name and
#: built in `ToolPipeline.__init__`; a unit test pins that every tool in
#: `ARGS_MODELS` has exactly one handler in each, so adding a tool without
#: wiring it is a loud test failure instead of a silent fallthrough.
FactsHandler = Callable[
    ["RunContext", ToolCallProposal, ToolArgs],
    Awaitable[tuple[ToolFacts, ToolPreview]],
]
ExecuteHandler = Callable[
    ["RunContext", ToolCallProposal, ToolArgs, str, ToolPreview],
    Awaitable["ToolExecution"],
]

MODEL_PAYLOAD_CHARS = 8_000
PREVIEW_CHARS = 4_000

_ERROR_CODES: dict[str, ToolErrorCode] = {
    "denied": ToolErrorCode.DENIED,
    "not_found": ToolErrorCode.NOT_FOUND,
    "invalid_arguments": ToolErrorCode.INVALID_ARGUMENTS,
    "stale_preimage": ToolErrorCode.STALE_PREIMAGE,
    "ambiguous_match": ToolErrorCode.AMBIGUOUS_MATCH,
    "timeout": ToolErrorCode.TIMEOUT,
    "internal": ToolErrorCode.INTERNAL,
}


@dataclass(frozen=True, slots=True)
class ToolExecution:
    """What one pipeline pass returns to the run loop: the structured result
    for the model, plus the one flag the loop must act on - `effect_unknown`
    stops the run so recovery can classify the interrupted side effect."""

    result: ToolResult
    effect_unknown: bool = False


class ToolPipeline:
    """One instance per RunService; `execute` runs one proposal end to end.

    The numbered comments inside `execute` mirror the stage order in the
    module docstring (1-2 registry/schema, 3 facts, 4 policy, 5 approval,
    6 ticket, 7 execute + evidence). At stage 7, read tools execute without
    an execution journal entry (no effect to recover); write tools journal
    STARTED -> CONFIRMED/FAILED around the actual I/O so a crash between the
    two is classifiable; `repo.exec` / `repo.check` additionally snapshot the
    tree before/after to attribute process writes (ADR 0012) and fail the
    call on protected-path tamper (ADR 0018).
    """

    def __init__(
        self,
        *,
        workspace: WorkspacePort,
        executor: ExecutorPort,
        store: SessionStorePort,
        emitter: EventEmitter,
        approvals: ApprovalResponder,
        registry: ToolRegistry,
        recipes: dict[str, RecipeSpec],
        mode: PermissionMode,
        launcher: SandboxLauncher | None = None,
        scratch_dir: Path | None = None,
    ) -> None:
        self._workspace = workspace
        self._executor = executor
        self._store = store
        self._emitter = emitter
        self._approvals = approvals
        self._registry = registry
        self._recipes = recipes
        self._mode = mode
        self._launcher = launcher
        self._scratch_dir = scratch_dir or Path(tempfile.gettempdir()) / "haven-scratch"
        # One row per tool, both phases. The unit test
        # test_every_registered_tool_is_fully_wired pins these tables against
        # ARGS_MODELS, so "add a tool" is: args model + policy class + one row
        # here per phase — and forgetting a row fails the suite, not the run.
        self._facts_handlers: dict[str, FactsHandler] = {
            "repo.list": self._facts_path_read,
            "repo.search": self._facts_path_read,
            "repo.read": self._facts_path_read,
            "repo.edit": self._facts_edit,
            "repo.create": self._facts_create,
            "repo.delete": self._facts_delete,
            "repo.move": self._facts_move,
            "repo.apply_patch": self._facts_patch,
            "repo.exec": self._facts_exec,
            "repo.check": self._facts_check,
            "repo.diff": self._facts_stateless,
            "task.plan": self._facts_stateless,
        }
        self._execute_handlers: dict[str, ExecuteHandler] = {
            "repo.list": self._execute_list,
            "repo.search": self._execute_search,
            "repo.read": self._execute_read,
            "repo.edit": self._execute_write_adapter,
            "repo.create": self._execute_write_adapter,
            "repo.delete": self._execute_delete_adapter,
            "repo.move": self._execute_move_adapter,
            "repo.apply_patch": self._execute_patch_adapter,
            "repo.exec": self._execute_exec_adapter,
            "repo.check": self._execute_check_adapter,
            "repo.diff": self._execute_diff,
            "task.plan": self._execute_plan,
        }

    async def execute(self, ctx: RunContext, step: int, call: ToolCallProposal) -> ToolExecution:
        started = time.monotonic()
        await self._emitter.emit(
            ctx.run_id,
            ToolProposed(
                run_id=ctx.run_id,
                step=step,
                call_id=call.call_id,
                tool_name=call.tool_name,
                args_summary=_clip(call.arguments_json, 200),
            ),
        )

        # 1-2. Registry lookup + strict schema validation.
        validated = self._registry.validate(call.tool_name, call.arguments_json)
        if isinstance(validated, ValidationFailure):
            code = (
                ToolErrorCode.UNKNOWN_TOOL
                if validated.code == "unknown_tool"
                else ToolErrorCode.INVALID_ARGUMENTS
            )
            return await self._finish(ctx, call, _error(call, code, validated.message), started)

        # 3. Program-collected workspace facts (never model-controlled).
        try:
            facts, preview = await self._collect_facts(ctx, call, validated)
        except WorkspaceError as exc:
            return await self._finish(ctx, call, _error(call, _map_ws_code(exc), str(exc)), started)

        # 4. Deterministic policy.
        outcome = evaluate_policy(self._mode, facts)
        await self._emitter.emit(
            ctx.run_id,
            PolicyDecided(
                run_id=ctx.run_id,
                call_id=call.call_id,
                decision=outcome.decision.value,
                reason_code=outcome.reason_code,
                risk=outcome.risk.value,
            ),
        )
        if outcome.decision is PolicyDecision.DENY:
            return await self._finish(
                ctx,
                call,
                _error(call, ToolErrorCode.DENIED, f"denied by policy: {outcome.reason_code}"),
                started,
            )

        # 5. Exact approval when policy says ASK.
        approval_id: str | None = None
        canonical_args = canonical_json(validated.model_dump())
        preimage = facts.preimage_digest
        if outcome.decision is PolicyDecision.ASK:
            approved, approval_id, message = await self._ask_approval(
                ctx, call, validated, canonical_args, preview, facts
            )
            if not approved:
                return await self._finish(
                    ctx,
                    call,
                    _error(call, ToolErrorCode.APPROVAL_REJECTED, message),
                    started,
                )
            # Re-verify the preimage after the human decision (TOCTOU guard).
            # Every tool that pins a file's content at approval — edit, delete,
            # the source of a move, and every file of a patch — is re-checked
            # against what is on disk now, so a change between approval and
            # execution fails closed.
            guarded_path: str | None = None
            if isinstance(validated, RepoEditArgs | RepoDeleteArgs):
                guarded_path = validated.path
            elif isinstance(validated, RepoMoveArgs):
                guarded_path = validated.src
            if guarded_path is not None:
                current = self._workspace.path_facts(guarded_path)
                if current.digest != preimage:
                    return await self._finish(
                        ctx,
                        call,
                        _error(
                            call,
                            ToolErrorCode.STALE_PREIMAGE,
                            "file changed between approval and execution",
                        ),
                        started,
                    )
            if isinstance(preview, PatchPreview):
                stale = [
                    path
                    for path, digest in preview.preimages.items()
                    if self._workspace.path_facts(path).digest != digest
                ] + [
                    effect.path
                    for effect in preview.effects
                    if effect.tool_shape == "repo.create"
                    and self._workspace.path_facts(effect.path).exists
                ]
                if stale:
                    return await self._finish(
                        ctx,
                        call,
                        _error(
                            call,
                            ToolErrorCode.STALE_PREIMAGE,
                            "file(s) changed between approval and execution: "
                            + ", ".join(sorted(stale)),
                        ),
                        started,
                    )

        # 6. Mint the execution ticket; raw model JSON stops here.
        if ctx.status is not RunStatus.EXECUTING_TOOL:
            ctx.move_to(RunStatus.EXECUTING_TOOL)
        ticket = mint_ticket(
            call_id=ToolCallId(call.call_id),
            tool_name=call.tool_name,
            tool_version=self._registry.version,
            canonical_args_json=canonical_args,
            workspace_digest=self._workspace.workspace_digest,
            preimage_digest=preimage,
            approval_id=ApprovalId(approval_id) if approval_id is not None else None,
        )
        await self._emitter.emit(
            ctx.run_id,
            ExecutionStarted(
                run_id=ctx.run_id,
                call_id=call.call_id,
                tool_name=call.tool_name,
                ticket_digest=ticket.ticket_digest,
                sandbox_backend=self._launcher.backend if self._launcher is not None else "",
            ),
        )

        # 7. Execute and confirm facts. Any workspace failure becomes a
        # structured ToolResult: the invariant is that a tool call never raises
        # into the agent loop, so one bad path cannot abort a whole run.
        try:
            execution = await self._run_ticketed(
                ctx, call, validated, ticket.ticket_digest, preview
            )
        except WorkspaceError as exc:
            return await self._finish(ctx, call, _error(call, _map_ws_code(exc), str(exc)), started)
        return await self._finish(ctx, call, execution.result, started, execution.effect_unknown)

    # -- facts -----------------------------------------------------------------

    async def _collect_facts(
        self, ctx: RunContext, call: ToolCallProposal, args: ToolArgs
    ) -> tuple[ToolFacts, ToolPreview]:
        """Dispatch to the per-tool facts handler.

        The registry already validated `call.tool_name` against ARGS_MODELS,
        and the wiring test pins the table to the same key set, so a miss here
        is impossible by construction; the fallback exists only to fail soft
        (a bare fact, which policy then denies for any effect tool).
        """
        handler = self._facts_handlers.get(call.tool_name)
        if handler is None:  # pragma: no cover - pinned impossible by the wiring test
            return ToolFacts(tool_name=call.tool_name), None
        return await handler(ctx, call, args)

    async def _facts_path_read(
        self, ctx: RunContext, call: ToolCallProposal, args: ToolArgs
    ) -> tuple[ToolFacts, ToolPreview]:
        assert isinstance(args, RepoListArgs | RepoSearchArgs | RepoReadArgs)
        facts = self._workspace.path_facts(args.path)
        return (
            ToolFacts(
                tool_name=call.tool_name,
                within_workspace=facts.within_workspace,
                touches_protected_path=facts.is_protected,
                path=facts.normalized,
            ),
            None,
        )

    async def _facts_edit(
        self, ctx: RunContext, call: ToolCallProposal, args: ToolArgs
    ) -> tuple[ToolFacts, ToolPreview]:
        assert isinstance(args, RepoEditArgs)
        facts = self._workspace.path_facts(args.path)
        if not facts.within_workspace or facts.is_protected:
            return (
                ToolFacts(
                    tool_name=call.tool_name,
                    within_workspace=facts.within_workspace,
                    touches_protected_path=facts.is_protected,
                    path=facts.normalized,
                ),
                None,
            )
        recorded = ctx.files_read.get(facts.normalized)
        if recorded is None:
            raise WorkspaceError(
                "invalid_arguments",
                f"read {facts.normalized!r} with repo.read before editing it",
            )
        if facts.digest != recorded:
            raise WorkspaceError(
                "stale_preimage",
                f"{facts.normalized!r} changed since it was last read; read it again",
            )
        preview = await self._workspace.preview_edit(
            args.path,
            args.old_string,
            args.new_string,
            occurrence=args.occurrence,
            replace_all=args.replace_all,
        )
        return (
            ToolFacts(
                tool_name=call.tool_name,
                within_workspace=True,
                touches_protected_path=False,
                preimage_digest=preview.preimage_digest,
                path=facts.normalized,
            ),
            preview,
        )

    async def _facts_create(
        self, ctx: RunContext, call: ToolCallProposal, args: ToolArgs
    ) -> tuple[ToolFacts, ToolPreview]:
        assert isinstance(args, RepoCreateArgs)
        facts = self._workspace.path_facts(args.path)
        if not facts.within_workspace or facts.is_protected:
            return (
                ToolFacts(
                    tool_name=call.tool_name,
                    within_workspace=facts.within_workspace,
                    touches_protected_path=facts.is_protected,
                    path=facts.normalized,
                ),
                None,
            )
        # Raises if the path already exists, so creation can never silently
        # overwrite a file the agent has not read.
        preview = await self._workspace.preview_create(args.path, args.content)
        return (
            ToolFacts(
                tool_name=call.tool_name,
                within_workspace=True,
                touches_protected_path=False,
                preimage_digest=None,
                path=facts.normalized,
            ),
            preview,
        )

    async def _facts_delete(
        self, ctx: RunContext, call: ToolCallProposal, args: ToolArgs
    ) -> tuple[ToolFacts, ToolPreview]:
        assert isinstance(args, RepoDeleteArgs)
        facts = self._workspace.path_facts(args.path)
        if not facts.within_workspace or facts.is_protected:
            return (
                ToolFacts(
                    tool_name=call.tool_name,
                    within_workspace=facts.within_workspace,
                    touches_protected_path=facts.is_protected,
                    path=facts.normalized,
                ),
                None,
            )
        # Raises not_found if the file is absent; the pipeline turns that
        # into a structured result. The human sees the content in the
        # preview, so a prior read is not required — the preimage is pinned.
        preview = await self._workspace.preview_delete(args.path)
        return (
            ToolFacts(
                tool_name=call.tool_name,
                within_workspace=True,
                touches_protected_path=False,
                preimage_digest=preview.preimage_digest,
                path=facts.normalized,
            ),
            preview,
        )

    async def _facts_move(
        self, ctx: RunContext, call: ToolCallProposal, args: ToolArgs
    ) -> tuple[ToolFacts, ToolPreview]:
        assert isinstance(args, RepoMoveArgs)
        src_facts = self._workspace.path_facts(args.src)
        dest_facts = self._workspace.path_facts(args.dest)
        within = src_facts.within_workspace and dest_facts.within_workspace
        protected = src_facts.is_protected or dest_facts.is_protected
        if not within or protected:
            return (
                ToolFacts(
                    tool_name=call.tool_name,
                    within_workspace=within,
                    touches_protected_path=protected,
                    path=src_facts.normalized,
                ),
                None,
            )
        removal, addition = await self._workspace.preview_move(args.src, args.dest)
        combined = EditPreview(
            path=f"{removal.path} -> {addition.path}",
            diff=removal.diff + addition.diff,
            preimage_digest=removal.preimage_digest,
            postimage_digest=addition.postimage_digest,
            insertions=addition.insertions,
            deletions=removal.deletions,
        )
        return (
            ToolFacts(
                tool_name=call.tool_name,
                within_workspace=True,
                touches_protected_path=False,
                preimage_digest=removal.preimage_digest,
                path=src_facts.normalized,
            ),
            combined,
        )

    async def _facts_patch(
        self, ctx: RunContext, call: ToolCallProposal, args: ToolArgs
    ) -> tuple[ToolFacts, ToolPreview]:
        assert isinstance(args, RepoApplyPatchArgs)
        # Hard facts first: every path of every operation must be inside
        # the workspace and unprotected, or policy hard-denies before any
        # preview work happens.
        op_paths: list[str] = []
        for op in args.operations:
            if op.kind == "move":
                op_paths += [op.src, op.dest]
            else:
                op_paths.append(op.path)
        all_facts = [self._workspace.path_facts(p) for p in op_paths]
        within = all(f.within_workspace for f in all_facts)
        protected = any(f.is_protected for f in all_facts)
        if not within or protected:
            return (
                ToolFacts(
                    tool_name=call.tool_name,
                    within_workspace=within,
                    touches_protected_path=protected,
                ),
                None,
            )
        plan = await self._workspace.preview_patch(
            tuple(_to_patch_spec(op) for op in args.operations), ctx.files_read
        )
        # The approval binds the aggregate of every touched file's pin:
        # one digest over the canonical {path: preimage} map, so any file
        # drifting invalidates the whole approval.
        aggregate = sha256_text(canonical_json(dict(sorted(plan.preimages.items()))))
        return (
            ToolFacts(
                tool_name=call.tool_name,
                within_workspace=True,
                touches_protected_path=False,
                preimage_digest=aggregate,
            ),
            plan,
        )

    async def _facts_exec(
        self, ctx: RunContext, call: ToolCallProposal, args: ToolArgs
    ) -> tuple[ToolFacts, ToolPreview]:
        assert isinstance(args, RepoExecArgs)
        facts = self._workspace.path_facts(args.cwd)
        return (
            ToolFacts(
                tool_name=call.tool_name,
                within_workspace=facts.within_workspace,
                touches_protected_path=facts.is_protected,
                exec_class=classify_argv(args.argv).value,
                sandbox_available=self._launcher is not None and self._launcher.available(),
                path=facts.normalized,
            ),
            None,
        )

    async def _facts_check(
        self, ctx: RunContext, call: ToolCallProposal, args: ToolArgs
    ) -> tuple[ToolFacts, ToolPreview]:
        assert isinstance(args, RepoCheckArgs)
        return (
            ToolFacts(
                tool_name=call.tool_name,
                recipe_registered=args.recipe_id in self._recipes,
            ),
            None,
        )

    async def _facts_stateless(
        self, ctx: RunContext, call: ToolCallProposal, args: ToolArgs
    ) -> tuple[ToolFacts, ToolPreview]:
        # repo.diff (read-only, no path arguments) and task.plan (touches only
        # run state): nothing on disk to pin.
        return ToolFacts(tool_name=call.tool_name), None

    # -- approval -----------------------------------------------------------------

    async def _ask_approval(
        self,
        ctx: RunContext,
        call: ToolCallProposal,
        args: ToolArgs,
        canonical_args: str,
        preview: ToolPreview,
        facts: ToolFacts,
    ) -> tuple[bool, str | None, str]:
        preview_text = ""
        summary = ""
        if isinstance(args, RepoApplyPatchArgs) and isinstance(preview, PatchPreview):
            preview_text = _clip(preview.diff, PREVIEW_CHARS)
            intent = f": {args.summary}" if args.summary else ""
            summary = (
                f"apply patch: {len(args.operations)} operation(s) across "
                f"{len(preview.effects)} file(s) "
                f"(+{preview.insertions} -{preview.deletions}){intent}"
            )
        elif isinstance(args, RepoEditArgs) and isinstance(preview, EditPreview):
            preview_text = _clip(preview.diff, PREVIEW_CHARS)
            intent = f": {args.summary}" if args.summary else ""
            scope = ""
            if args.replace_all:
                scope = " [all occurrences]"
            elif args.occurrence is not None:
                scope = f" [occurrence {args.occurrence}]"
            summary = (
                f"edit {preview.path} (+{preview.insertions} -{preview.deletions}){scope}{intent}"
            )
        elif isinstance(args, RepoCreateArgs) and isinstance(preview, EditPreview):
            preview_text = _clip(preview.diff, PREVIEW_CHARS)
            intent = f": {args.summary}" if args.summary else ""
            summary = f"create {preview.path} ({preview.insertions} new line(s)){intent}"
        elif isinstance(args, RepoDeleteArgs) and isinstance(preview, EditPreview):
            preview_text = _clip(preview.diff, PREVIEW_CHARS)
            intent = f": {args.summary}" if args.summary else ""
            summary = f"delete {preview.path} ({preview.deletions} line(s)){intent}"
        elif isinstance(args, RepoMoveArgs) and isinstance(preview, EditPreview):
            preview_text = _clip(preview.diff, PREVIEW_CHARS)
            intent = f": {args.summary}" if args.summary else ""
            summary = f"move {preview.path}{intent}"
        elif isinstance(args, RepoExecArgs):
            lines = [f"$ {shlex.join(args.argv)}", self._describe_sandbox()]
            if classify_argv(args.argv) is ExecClass.SHELL_PASSTHROUGH:
                lines.append(
                    "WARNING: this interprets an arbitrary script, so the command "
                    "above does not describe everything it may do."
                )
            preview_text = "\n".join(lines)
            intent = f": {args.summary}" if args.summary else ""
            summary = f"run {shlex.join(args.argv)} in {args.cwd}{intent}"
        elif isinstance(args, RepoCheckArgs):
            recipe = self._recipes[args.recipe_id]
            preview_text = "$ " + " ".join(recipe.argv)
            summary = (
                f"run check recipe {args.recipe_id!r} "
                "(approving also covers identical re-runs for the rest of this run)"
            )

        digest = compute_approval_digest(
            workspace_digest=self._workspace.workspace_digest,
            tool_name=call.tool_name,
            tool_version=self._registry.version,
            canonical_args_json=canonical_args,
            preimage_digest=facts.preimage_digest,
            preview_digest=sha256_text(preview_text) if preview_text else None,
        )

        if isinstance(args, RepoCheckArgs) and digest in ctx.standing_check_grants:
            # Standing grant (ADR 0025): the human already approved this
            # byte-identical check in this run (same recipe id and argv, same
            # workspace, same tool version — the digest pins all of it). Mint
            # and consume a fresh single-use approval so the journal still
            # carries one approval row per execution, announce the grant, and
            # skip the modal. Only repo.check is ever eligible: it runs a
            # user-registered recipe and its repeat is the verify loop's
            # normal shape; writes and exec always re-ask.
            approval_id = new_approval_id()
            await self._store.record_approval(approval_id, ctx.run_id, digest)
            await self._store.decide_approval(approval_id, ApprovalDecision.APPROVED)
            await self._emitter.emit(
                ctx.run_id,
                Notice(
                    run_id=ctx.run_id,
                    level="info",
                    message=(
                        f"standing approval: check recipe {args.recipe_id!r} was "
                        "approved earlier in this run; re-running without asking"
                    ),
                ),
            )
            await self._emitter.emit(
                ctx.run_id,
                ApprovalDecided(
                    run_id=ctx.run_id,
                    approval_id=approval_id,
                    decision=ApprovalDecision.APPROVED.value,
                ),
            )
            if not await self._store.consume_approval(approval_id, digest):
                ctx.move_to(RunStatus.RUNNING_MODEL)
                return False, None, "approval could not be consumed (stale or reused)"
            ctx.move_to(RunStatus.EXECUTING_TOOL)
            return True, str(approval_id), ""

        approval_id = new_approval_id()
        await self._store.record_approval(approval_id, ctx.run_id, digest)
        request = ApprovalRequest(
            approval_id=approval_id,
            run_id=ctx.run_id,
            call_id=ToolCallId(call.call_id),
            tool_name=call.tool_name,
            summary=summary,
            risk=evaluate_policy(self._mode, facts).risk,
            request_digest=digest,
            preview=preview_text,
        )
        ctx.move_to(RunStatus.WAITING_APPROVAL)
        await self._emitter.emit(
            ctx.run_id,
            ApprovalRequested(
                run_id=ctx.run_id,
                call_id=call.call_id,
                approval_id=approval_id,
                tool_name=call.tool_name,
                summary=summary,
                preview=preview_text,
                risk=request.risk.value,
                request_digest=digest,
            ),
        )

        decision = await self._approvals.respond(request)

        await self._store.decide_approval(approval_id, decision)
        await self._emitter.emit(
            ctx.run_id,
            ApprovalDecided(run_id=ctx.run_id, approval_id=approval_id, decision=decision.value),
        )
        if decision is not ApprovalDecision.APPROVED:
            ctx.move_to(RunStatus.RUNNING_MODEL)
            return False, None, "the user rejected this action"

        # Single consumption, digest-bound: a second consumption or a drifted
        # digest fails closed.
        if not await self._store.consume_approval(approval_id, digest):
            ctx.move_to(RunStatus.RUNNING_MODEL)
            return False, None, "approval could not be consumed (stale or reused)"
        if isinstance(args, RepoCheckArgs):
            # The first human approval of this exact check arms the run-scoped
            # standing grant announced on the card (ADR 0025). A rejection
            # never arms anything — this line is only reached on approval.
            ctx.standing_check_grants.add(digest)
        ctx.move_to(RunStatus.EXECUTING_TOOL)
        return True, str(approval_id), ""

    # -- execution -----------------------------------------------------------------

    async def _run_ticketed(
        self,
        ctx: RunContext,
        call: ToolCallProposal,
        args: ToolArgs,
        ticket_digest: str,
        preview: ToolPreview,
    ) -> ToolExecution:
        """Dispatch to the per-tool execute handler (same key set as facts)."""
        handler = self._execute_handlers.get(call.tool_name)
        if handler is None:  # pragma: no cover - pinned impossible by the wiring test
            return ToolExecution(
                _error(call, ToolErrorCode.UNKNOWN_TOOL, f"no executor for {call.tool_name!r}")
            )
        return await handler(ctx, call, args, ticket_digest, preview)

    async def _execute_list(
        self,
        ctx: RunContext,
        call: ToolCallProposal,
        args: ToolArgs,
        ticket_digest: str,
        preview: ToolPreview,
    ) -> ToolExecution:
        assert isinstance(args, RepoListArgs)
        listing = await self._workspace.list_dir(args.path, args.max_entries)
        return ToolExecution(
            _ok(
                call,
                {
                    "path": listing.path,
                    "entries": [
                        {"name": e.name, "dir": e.is_dir, "size": e.size_bytes}
                        for e in listing.entries
                    ],
                },
                truncated=listing.truncated,
            )
        )

    async def _execute_search(
        self,
        ctx: RunContext,
        call: ToolCallProposal,
        args: ToolArgs,
        ticket_digest: str,
        preview: ToolPreview,
    ) -> ToolExecution:
        assert isinstance(args, RepoSearchArgs)
        found = await self._workspace.search(args.pattern, args.path, args.max_results)
        return ToolExecution(
            _ok(
                call,
                {
                    "matches": [
                        {"path": m.path, "line": m.line_number, "text": m.line}
                        for m in found.matches
                    ],
                    "files_scanned": found.files_scanned,
                },
                truncated=found.truncated,
            )
        )

    async def _execute_read(
        self,
        ctx: RunContext,
        call: ToolCallProposal,
        args: ToolArgs,
        ticket_digest: str,
        preview: ToolPreview,
    ) -> ToolExecution:
        assert isinstance(args, RepoReadArgs)
        read = await self._workspace.read_file(args.path, args.start_line, args.max_lines)
        ctx.files_read[read.path] = read.digest
        return ToolExecution(
            _ok(
                call,
                {
                    "path": read.path,
                    "start_line": read.start_line,
                    "end_line": read.end_line,
                    "total_lines": read.total_lines,
                    "content": read.content,
                },
                truncated=read.truncated,
            )
        )

    async def _execute_write_adapter(
        self,
        ctx: RunContext,
        call: ToolCallProposal,
        args: ToolArgs,
        ticket_digest: str,
        preview: ToolPreview,
    ) -> ToolExecution:
        assert isinstance(args, RepoEditArgs | RepoCreateArgs)
        assert not isinstance(preview, PatchPreview)
        return await self._execute_write(ctx, call, args, ticket_digest, preview)

    async def _execute_delete_adapter(
        self,
        ctx: RunContext,
        call: ToolCallProposal,
        args: ToolArgs,
        ticket_digest: str,
        preview: ToolPreview,
    ) -> ToolExecution:
        assert isinstance(args, RepoDeleteArgs)
        assert not isinstance(preview, PatchPreview)
        return await self._execute_delete(ctx, call, args, ticket_digest, preview)

    async def _execute_move_adapter(
        self,
        ctx: RunContext,
        call: ToolCallProposal,
        args: ToolArgs,
        ticket_digest: str,
        preview: ToolPreview,
    ) -> ToolExecution:
        assert isinstance(args, RepoMoveArgs)
        assert not isinstance(preview, PatchPreview)
        return await self._execute_move(ctx, call, args, ticket_digest, preview)

    async def _execute_patch_adapter(
        self,
        ctx: RunContext,
        call: ToolCallProposal,
        args: ToolArgs,
        ticket_digest: str,
        preview: ToolPreview,
    ) -> ToolExecution:
        assert isinstance(args, RepoApplyPatchArgs)
        assert isinstance(preview, PatchPreview)  # facts collection built it
        return await self._execute_patch(ctx, call, ticket_digest, preview)

    async def _execute_exec_adapter(
        self,
        ctx: RunContext,
        call: ToolCallProposal,
        args: ToolArgs,
        ticket_digest: str,
        preview: ToolPreview,
    ) -> ToolExecution:
        assert isinstance(args, RepoExecArgs)
        return await self._execute_exec(ctx, call, args, ticket_digest)

    async def _execute_check_adapter(
        self,
        ctx: RunContext,
        call: ToolCallProposal,
        args: ToolArgs,
        ticket_digest: str,
        preview: ToolPreview,
    ) -> ToolExecution:
        assert isinstance(args, RepoCheckArgs)
        return await self._execute_check(ctx, call, args, ticket_digest)

    async def _execute_plan(
        self,
        ctx: RunContext,
        call: ToolCallProposal,
        args: ToolArgs,
        ticket_digest: str,
        preview: ToolPreview,
    ) -> ToolExecution:
        assert isinstance(args, TaskPlanArgs)
        ctx.plan = tuple(args.steps)
        await self._emitter.emit(
            ctx.run_id,
            PlanUpdated(
                run_id=ctx.run_id,
                steps=tuple(
                    PlanStepView(title=_clip(step.title, 120), status=step.status)
                    for step in ctx.plan
                ),
            ),
        )
        done = sum(1 for step in ctx.plan if step.status == "done")
        return ToolExecution(_ok(call, {"steps": len(ctx.plan), "done": done, "recorded": True}))

    async def _execute_diff(
        self,
        ctx: RunContext,
        call: ToolCallProposal,
        args: ToolArgs,
        ticket_digest: str,
        preview: ToolPreview,
    ) -> ToolExecution:
        run_diff = await self._workspace.run_diff()
        envelope = await self._emitter.emit(
            ctx.run_id,
            DiffPreview(
                run_id=ctx.run_id,
                files_changed=len(run_diff.files),
                insertions=run_diff.insertions,
                deletions=run_diff.deletions,
                preview=_clip(run_diff.diff, PREVIEW_CHARS),
            ),
        )
        ctx.ledger = ctx.ledger.with_diff(
            DiffEvidence(
                seq=envelope.seq,
                files_changed=len(run_diff.files),
                insertions=run_diff.insertions,
                deletions=run_diff.deletions,
                diff_digest=sha256_text(run_diff.diff),
            )
        )
        await self._emitter.emit(
            ctx.run_id,
            EvidenceRecorded(
                run_id=ctx.run_id,
                evidence_kind="diff",
                summary=(
                    f"{len(run_diff.files)} file(s), +{run_diff.insertions} -{run_diff.deletions}"
                ),
            ),
        )
        return ToolExecution(
            _ok(
                call,
                {
                    "files": list(run_diff.files),
                    "insertions": run_diff.insertions,
                    "deletions": run_diff.deletions,
                    "diff": _clip(run_diff.diff, MODEL_PAYLOAD_CHARS),
                },
                truncated=run_diff.truncated,
            )
        )

    async def _execute_write(
        self,
        ctx: RunContext,
        call: ToolCallProposal,
        args: RepoEditArgs | RepoCreateArgs,
        ticket_digest: str,
        preview: EditPreview | None,
    ) -> ToolExecution:
        assert preview is not None  # facts collection always builds it for writes
        # The preview's postimage is recorded *before* any byte lands: if the
        # process dies inside the write, recovery can classify "file already
        # matches the expected postimage" as confirmed instead of unknown.
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
            return ToolExecution(_error(call, _map_ws_code(exc), str(exc)))
        except BaseException:
            # Crash or cancellation mid-write: the effect state is unknown and
            # must never be silently replayed.
            await self._store.update_execution_state(call.call_id, EffectState.EFFECT_UNKNOWN)
            raise

        await self._store.update_execution_state(
            call.call_id, EffectState.CONFIRMED, outcome.postimage_digest
        )
        # The agent now knows this file's exact contents, so a later edit of it
        # is legitimately preimage-bound without another read.
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
            _ok(
                call,
                {
                    "path": outcome.path,
                    "applied": True,
                    "postimage_digest": outcome.postimage_digest,
                    "diff": _clip(preview.diff, MODEL_PAYLOAD_CHARS),
                },
            )
        )

    async def _execute_delete(
        self,
        ctx: RunContext,
        call: ToolCallProposal,
        args: RepoDeleteArgs,
        ticket_digest: str,
        preview: EditPreview | None,
    ) -> ToolExecution:
        # The approval-bound preimage, not a fresh read: apply_delete compares
        # the file on disk against this, so a change since approval fails closed.
        assert preview is not None  # facts collection always builds it for a delete
        preimage = preview.preimage_digest
        await self._store.record_execution(
            ExecutionRecord(
                call_id=call.call_id,
                run_id=ctx.run_id,
                ticket_digest=ticket_digest,
                tool_name=call.tool_name,
                effect_state=EffectState.STARTED,
                preimage_digest=preimage,
                postimage_digest="",
                path=args.path,
            )
        )
        try:
            outcome = await self._workspace.apply_delete(args.path, preimage)
        except WorkspaceError as exc:
            await self._store.update_execution_state(call.call_id, EffectState.FAILED)
            return ToolExecution(_error(call, _map_ws_code(exc), str(exc)))
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
        return ToolExecution(_ok(call, {"path": outcome.path, "deleted": True}))

    async def _execute_move(
        self,
        ctx: RunContext,
        call: ToolCallProposal,
        args: RepoMoveArgs,
        ticket_digest: str,
        preview: EditPreview | None,
    ) -> ToolExecution:
        # The approval-bound preimage of the source, so apply_move fails closed
        # if the source changed between approval and execution.
        assert preview is not None  # facts collection always builds it for a move
        preimage = preview.preimage_digest
        # dest_path lets recovery inspect both ends of an interrupted move: the
        # content is unchanged by a move, so src/dest presence plus the preimage
        # digest classifies every crash point except the copy-then-crash gap.
        await self._store.record_execution(
            ExecutionRecord(
                call_id=call.call_id,
                run_id=ctx.run_id,
                ticket_digest=ticket_digest,
                tool_name=call.tool_name,
                effect_state=EffectState.STARTED,
                preimage_digest=preimage,
                postimage_digest="",
                path=args.src,
                dest_path=args.dest,
            )
        )
        try:
            removal, addition = await self._workspace.apply_move(args.src, args.dest, preimage)
        except WorkspaceError as exc:
            await self._store.update_execution_state(call.call_id, EffectState.FAILED)
            return ToolExecution(_error(call, _map_ws_code(exc), str(exc)))
        except BaseException:
            await self._store.update_execution_state(call.call_id, EffectState.EFFECT_UNKNOWN)
            raise

        await self._store.update_execution_state(
            call.call_id, EffectState.CONFIRMED, addition.postimage_digest
        )
        ctx.files_read.pop(removal.path, None)
        # The agent now knows the destination's contents (they are the source's).
        ctx.files_read[addition.path] = addition.postimage_digest
        envelope = await self._emitter.emit(
            ctx.run_id,
            EvidenceRecorded(
                run_id=ctx.run_id,
                evidence_kind="edit",
                summary=f"moved {removal.path} -> {addition.path}",
            ),
        )
        # Both halves are the run's changes: the removal and the addition.
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
        return ToolExecution(_ok(call, {"src": removal.path, "dest": addition.path, "moved": True}))

    async def _execute_patch(
        self,
        ctx: RunContext,
        call: ToolCallProposal,
        ticket_digest: str,
        plan: PatchPreview,
    ) -> ToolExecution:
        # The patch is journaled as its constituent file effects — each shaped
        # like a single-op tool with its expected postimage — so an interrupted
        # patch is classifiable file-by-file by the existing recovery rules.
        for index, effect in enumerate(plan.effects):
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
            for index in range(len(plan.effects)):
                await self._store.update_execution_state(f"{call.call_id}#{index}", state)

        try:
            outcomes = await self._workspace.apply_patch(plan)
        except WorkspaceError as exc:
            # Includes "failed and rolled back cleanly": the tree is unchanged,
            # so the effect is a plain failure, not an unknown.
            await mark_all(EffectState.FAILED)
            return ToolExecution(_error(call, _map_ws_code(exc), str(exc)))
        except PatchRollbackError as exc:
            # Partial state that deterministic code could not undo: surface as
            # an unknown effect so the run stops and recovery blocks resume
            # until a human reconciles each journaled sub-effect.
            await mark_all(EffectState.EFFECT_UNKNOWN)
            return ToolExecution(
                _error(call, ToolErrorCode.INTERNAL, str(exc)), effect_unknown=True
            )
        except BaseException:
            await mark_all(EffectState.EFFECT_UNKNOWN)
            raise

        by_path = {outcome.path: outcome for outcome in outcomes}
        for index, effect in enumerate(plan.effects):
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
                summary=(f"patch: {len(outcomes)} file(s), +{plan.insertions} -{plan.deletions}"),
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
                # The agent knows this file's exact content now.
                ctx.files_read[outcome.path] = outcome.postimage_digest
            else:
                ctx.files_read.pop(outcome.path, None)

        return ToolExecution(
            _ok(
                call,
                {
                    "applied": True,
                    "files_changed": len(outcomes),
                    "insertions": plan.insertions,
                    "deletions": plan.deletions,
                    "files": [outcome.path for outcome in outcomes],
                    "diff": _clip(plan.diff, MODEL_PAYLOAD_CHARS),
                },
            )
        )

    def _sandbox_spec(self) -> SandboxSpec:
        # Model-proposed exec is read-only on the workspace: only the scratch
        # dir is writable. Real source changes must go through the audited
        # edit/create/delete/move tools, and this closes the Linux hole where
        # Landlock cannot carve `.git` out of a writable workspace — exec cannot
        # write the workspace at all (ADR 0017).
        return SandboxSpec(
            workspace_root=self._workspace.root,
            scratch_dir=self._scratch_dir,
            writable=False,
            allow_network=False,
            private_roots=default_private_roots(),
            extra_readable_roots=default_readable_roots(),
        )

    def _describe_sandbox(self) -> str:
        if self._launcher is None:
            return "sandbox: unavailable"
        return self._launcher.describe(self._sandbox_spec())

    async def _execute_exec(
        self, ctx: RunContext, call: ToolCallProposal, args: RepoExecArgs, ticket_digest: str
    ) -> ToolExecution:
        assert self._launcher is not None  # policy denies exec without a launcher
        await self._store.record_execution(
            ExecutionRecord(
                call_id=call.call_id,
                run_id=ctx.run_id,
                ticket_digest=ticket_digest,
                tool_name=call.tool_name,
                effect_state=EffectState.STARTED,
                preimage_digest="",
                postimage_digest="",
                path=args.cwd,
            )
        )
        spec = ExecSpec(
            argv=args.argv,
            cwd=self._workspace.root / args.cwd,
            timeout_seconds=args.timeout_seconds,
            sandbox=self._sandbox_spec(),
        )
        before = self._workspace.capture_snapshot()
        try:
            outcome = await self._executor.run_exec(spec)
        except BaseException:
            await self._store.update_execution_state(call.call_id, EffectState.EFFECT_UNKNOWN)
            raise

        # A nonzero exit is a completed execution, not an unknown effect.
        await self._store.update_execution_state(call.call_id, EffectState.CONFIRMED)
        # Any file the command changed is attributed to it as edit evidence, so
        # a write through exec cannot escape the Evidence Gate (ADR 0012).
        tampered = await self._record_process_writes(
            ctx, call.tool_name, before, self._workspace.capture_snapshot()
        )
        if tampered:
            return ToolExecution(
                _error(
                    call,
                    ToolErrorCode.PROTECTED_PATH_TAMPERED,
                    "the command modified protected path(s) "
                    f"{', '.join(tampered)}; this is a boundary violation",
                )
            )
        if outcome.timed_out:
            return ToolExecution(
                _error(
                    call, ToolErrorCode.TIMEOUT, f"command timed out after {args.timeout_seconds}s"
                )
            )
        # No evidence is recorded here on purpose: only a registered check
        # recipe can satisfy the Evidence Gate.
        return ToolExecution(
            _ok(
                call,
                {
                    "exit_code": outcome.exit_code,
                    "duration_ms": outcome.duration_ms,
                    "stdout_tail": _clip(outcome.stdout_tail, 4000),
                    "stderr_tail": _clip(outcome.stderr_tail, 2000),
                    "sandbox": self._launcher.backend,
                },
                truncated=outcome.truncated,
            )
        )

    async def _record_process_writes(
        self,
        ctx: RunContext,
        tool_name: str,
        before: WorkspaceSnapshot,
        after: WorkspaceSnapshot,
    ) -> list[str]:
        """Attribute any workspace change a process caused to the ledger.

        Only edit/create used to write evidence, so a file changed by a process
        was invisible to the Evidence Gate (ADR 0012). Here a before/after
        snapshot is diffed and every change becomes edit evidence, so a run that
        mutates the tree through any tool is held to the same evidence standard.

        Returns the protected paths the process changed, so the caller can fail
        the tool call outright (ADR 0018) — a control-plane mutation must be a
        hard outcome, not an annotation.
        """
        # A protected path changing during a process is a tamper the OS sandbox
        # could not prevent (Landlock cannot protect `.git` in a writable
        # workspace). It is surfaced as an error so it is attributable in the
        # audit trail rather than silent — the invisibility half of the hole.
        tampered = sorted(
            name
            for name in before.protected_digests.keys() | after.protected_digests.keys()
            if before.protected_digests.get(name) != after.protected_digests.get(name)
        )
        for name in tampered:
            await self._emitter.emit(
                ctx.run_id,
                Notice(
                    run_id=ctx.run_id,
                    level="error",
                    message=f"{tool_name} modified a protected path ({name}); this must not happen",
                ),
            )

        changes = _detect_changes(before, after)
        if not changes:
            return tampered
        summary = f"{len(changes)} file(s) changed by {tool_name}: " + ", ".join(
            change.path for change in changes[:5]
        )
        envelope = await self._emitter.emit(
            ctx.run_id,
            EvidenceRecorded(run_id=ctx.run_id, evidence_kind="edit", summary=_clip(summary, 200)),
        )
        for change in changes:
            self._workspace.register_run_original(change.path, change.before_content)
            ctx.ledger = ctx.ledger.with_edit(
                EditEvidence(
                    seq=envelope.seq,
                    path=change.path,
                    preimage_digest=change.preimage_digest,
                    postimage_digest=change.postimage_digest,
                )
            )
        return tampered

    async def _execute_check(
        self, ctx: RunContext, call: ToolCallProposal, args: RepoCheckArgs, ticket_digest: str
    ) -> ToolExecution:
        recipe = self._recipes[args.recipe_id]
        await self._store.record_execution(
            ExecutionRecord(
                call_id=call.call_id,
                run_id=ctx.run_id,
                ticket_digest=ticket_digest,
                tool_name=call.tool_name,
                effect_state=EffectState.STARTED,
                preimage_digest="",
                postimage_digest="",
                path="",
            )
        )
        before = self._workspace.capture_snapshot()
        try:
            outcome = await self._executor.run_recipe(recipe, self._workspace.root)
        except BaseException:
            await self._store.update_execution_state(call.call_id, EffectState.EFFECT_UNKNOWN)
            raise

        await self._store.update_execution_state(call.call_id, EffectState.CONFIRMED)
        # A check that mutates the tree (e.g. a formatter) is recorded like any
        # other write, before the check evidence, so the gate sees it.
        tampered = await self._record_process_writes(
            ctx, call.tool_name, before, self._workspace.capture_snapshot()
        )
        if tampered:
            # A check that rewrote the control plane is not a verification: no
            # check evidence is recorded, so this run cannot use it to satisfy
            # the Evidence Gate, and the call itself fails (ADR 0018).
            return ToolExecution(
                _error(
                    call,
                    ToolErrorCode.PROTECTED_PATH_TAMPERED,
                    f"recipe {recipe.id!r} modified protected path(s) "
                    f"{', '.join(tampered)}; the check does not count as verification",
                )
            )
        envelope = await self._emitter.emit(
            ctx.run_id,
            EvidenceRecorded(
                run_id=ctx.run_id,
                evidence_kind="check",
                summary=(
                    f"{recipe.id}: exit={outcome.exit_code} in {outcome.duration_ms}ms"
                    + (" (timed out)" if outcome.timed_out else "")
                ),
            ),
        )
        ctx.ledger = ctx.ledger.with_check(
            CheckEvidence(
                seq=envelope.seq,
                recipe_id=recipe.id,
                exit_code=outcome.exit_code,
                duration_ms=outcome.duration_ms,
                truncated=outcome.truncated,
            )
        )
        if outcome.timed_out:
            return ToolExecution(
                _error(
                    call,
                    ToolErrorCode.TIMEOUT,
                    f"recipe {recipe.id!r} timed out after {recipe.timeout_seconds}s",
                )
            )
        return ToolExecution(
            _ok(
                call,
                {
                    "recipe_id": recipe.id,
                    "exit_code": outcome.exit_code,
                    "duration_ms": outcome.duration_ms,
                    "stdout_tail": _clip(outcome.stdout_tail, 4000),
                    "stderr_tail": _clip(outcome.stderr_tail, 2000),
                },
                truncated=outcome.truncated,
            )
        )

    # -- shared -----------------------------------------------------------------

    async def _finish(
        self,
        ctx: RunContext,
        call: ToolCallProposal,
        result: ToolResult,
        started: float,
        effect_unknown: bool = False,
    ) -> ToolExecution:
        duration_ms = int((time.monotonic() - started) * 1000)
        result = result.model_copy(update={"duration_ms": duration_ms})
        await self._emitter.emit(
            ctx.run_id,
            ToolCompleted(
                run_id=ctx.run_id,
                call_id=call.call_id,
                tool_name=call.tool_name,
                status=result.status.value,
                error_code=result.error_code.value if result.error_code else "",
                summary=_clip(result.message or _summarize_payload(result), 200),
                truncated=result.truncated,
                duration_ms=duration_ms,
            ),
        )
        return ToolExecution(result=result, effect_unknown=effect_unknown)


@dataclass(frozen=True, slots=True)
class _ExternalChange:
    path: str
    preimage_digest: str
    postimage_digest: str
    before_content: str


def _detect_changes(before: WorkspaceSnapshot, after: WorkspaceSnapshot) -> list[_ExternalChange]:
    """Files whose digest appeared, disappeared, or moved between snapshots.

    Pure over the two digest maps, so the change set is a function of the
    snapshots alone. Deletion yields an empty postimage, creation an empty
    preimage — the convention the edit/create paths already use.
    """
    changed = sorted(
        path
        for path in before.digests.keys() | after.digests.keys()
        if before.digests.get(path) != after.digests.get(path)
    )
    return [
        _ExternalChange(
            path=path,
            preimage_digest=before.digests.get(path, ""),
            postimage_digest=after.digests.get(path, ""),
            before_content=before.contents.get(path, ""),
        )
        for path in changed
    ]


def _to_patch_spec(
    op: PatchEditOp | PatchCreateOp | PatchDeleteOp | PatchMoveOp,
) -> PatchOpSpec:
    """Contract operation -> port-neutral spec (the workspace never sees
    pydantic models)."""
    if isinstance(op, PatchEditOp):
        return PatchOpSpec(
            kind="edit",
            path=op.path,
            old=op.old_string,
            new=op.new_string,
            occurrence=op.occurrence,
            replace_all=op.replace_all,
        )
    if isinstance(op, PatchCreateOp):
        return PatchOpSpec(kind="create", path=op.path, content=op.content)
    if isinstance(op, PatchDeleteOp):
        return PatchOpSpec(kind="delete", path=op.path)
    return PatchOpSpec(kind="move", src=op.src, dest=op.dest)


def _ok(call: ToolCallProposal, payload: dict[str, object], truncated: bool = False) -> ToolResult:
    return ToolResult(
        call_id=call.call_id,
        tool_name=call.tool_name,
        status=ToolStatus.OK,
        payload=dict(payload),
        truncated=truncated,
    )


def _error(call: ToolCallProposal, code: ToolErrorCode, message: str) -> ToolResult:
    return ToolResult(
        call_id=call.call_id,
        tool_name=call.tool_name,
        status=ToolStatus.ERROR,
        error_code=code,
        message=message,
    )


def _map_ws_code(exc: WorkspaceError) -> ToolErrorCode:
    return _ERROR_CODES.get(exc.code, ToolErrorCode.INTERNAL)


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"... [truncated {len(text) - limit} chars]"


def _summarize_payload(result: ToolResult) -> str:
    if not result.payload:
        return result.status.value
    return _clip(json.dumps(result.payload, ensure_ascii=False, default=str), 200)
