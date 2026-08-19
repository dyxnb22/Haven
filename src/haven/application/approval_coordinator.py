"""精确审批的持久化、展示、单次消费和 standing grant。"""

from haven.application.approval_cards import ApprovalCardRenderer, ToolPreview
from haven.application.approvals import ApprovalResponder
from haven.application.emitter import EventEmitter
from haven.application.registry import ToolRegistry
from haven.application.state import RunContext
from haven.contracts.events import ApprovalDecided, ApprovalRequested, Notice
from haven.contracts.model import ToolCallProposal
from haven.contracts.tools import RepoCheckArgs, ToolArgs
from haven.domain.approval import ApprovalRequest, compute_approval_digest
from haven.domain.digest import sha256_text
from haven.domain.enums import ApprovalDecision, PermissionMode, RunStatus
from haven.domain.ids import ToolCallId, new_approval_id
from haven.domain.policy import ToolFacts, evaluate_policy
from haven.ports.session import SessionStorePort
from haven.ports.workspace import WorkspacePort


class ApprovalCoordinator:
    """创建审批请求、等待用户决定并消费一次性审批凭证。"""

    def __init__(
        self,
        *,
        workspace: WorkspacePort,
        store: SessionStorePort,
        emitter: EventEmitter,
        approvals: ApprovalResponder,
        registry: ToolRegistry,
        renderer: ApprovalCardRenderer,
        mode: PermissionMode,
    ) -> None:
        self._workspace = workspace
        self._store = store
        self._emitter = emitter
        self._approvals = approvals
        self._registry = registry
        self._renderer = renderer
        self._mode = mode

    async def ask(
        self,
        ctx: RunContext,
        call: ToolCallProposal,
        args: ToolArgs,
        canonical_args: str,
        preview: ToolPreview,
        facts: ToolFacts,
    ) -> tuple[bool, str | None, str]:
        """计算绑定摘要、请求审批并消费一次性凭证，返回是否可执行及失败原因。"""
        summary, preview_text = self._renderer.render(call.tool_name, args, preview)
        digest = compute_approval_digest(
            workspace_digest=self._workspace.workspace_digest,
            tool_name=call.tool_name,
            tool_version=self._registry.version,
            canonical_args_json=canonical_args,
            preimage_digest=facts.preimage_digest,
            preview_digest=sha256_text(preview_text) if preview_text else None,
        )

        if isinstance(args, RepoCheckArgs) and digest in ctx.standing_check_grants:
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
        if not await self._store.consume_approval(approval_id, digest):
            ctx.move_to(RunStatus.RUNNING_MODEL)
            return False, None, "approval could not be consumed (stale or reused)"
        if isinstance(args, RepoCheckArgs):
            ctx.standing_check_grants.add(digest)
        ctx.move_to(RunStatus.EXECUTING_TOOL)
        return True, str(approval_id), ""
