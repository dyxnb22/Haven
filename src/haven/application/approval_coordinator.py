"""精确审批的持久化、展示、单次消费和 standing grant。"""

from collections.abc import Mapping

from haven.application.approval_cards import ApprovalCardRenderer, ToolPreview
from haven.application.approvals import ApprovalResponder
from haven.application.emitter import EventEmitter
from haven.application.registry import ToolRegistry
from haven.application.state import RunContext
from haven.contracts.events import ApprovalDecided, ApprovalRequested, Notice
from haven.contracts.model import ToolCallProposal
from haven.contracts.tools import ToolArgs
from haven.domain.approval import ApprovalRequest, compute_approval_digest
from haven.domain.digest import sha256_text
from haven.domain.enums import ApprovalDecision, ApprovalMode, PermissionMode, RiskLevel, RunStatus
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
        approval_mode: ApprovalMode = ApprovalMode.ASK,
    ) -> None:
        self._workspace = workspace
        self._store = store
        self._emitter = emitter
        self._approvals = approvals
        self._registry = registry
        self._renderer = renderer
        self._mode = mode
        self._approval_mode = approval_mode

    @property
    def approval_mode(self) -> ApprovalMode:
        """当前的审批分级模式（ask / smart / full）。"""
        return self._approval_mode

    def set_approval_mode(self, mode: ApprovalMode) -> None:
        """动态更新当前的审批分级模式。"""
        self._approval_mode = mode

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
        # 授权键把"这次调用是什么"和"哪些技能请求了授权"绑在一起：换一组技能，
        # 同样的调用就不再命中同一个授权（ADR 0032）。
        grant_key = standing_grant_key(digest, ctx.granted_tools)
        # 两条互不相干的免审来源：技能请求的授权（选中即生效），以及 ADR 0025 的
        # 既有机制（人类批准过的字节级相同调用）。命中技能的调用不会弹卡，但它
        # 的摘要仍会写进 journal，因此说明要在渲染之后补上。
        requester = ctx.granted_tools.get(call.tool_name) or ctx.standing_grants.get(grant_key)
        summary = _with_grant_hint(summary, requester)
        if requester is not None:
            ctx.standing_grants[grant_key] = requester
            approval_id = new_approval_id()
            await self._store.record_approval(approval_id, ctx.run_id, digest)
            await self._store.decide_approval(approval_id, ApprovalDecision.APPROVED)
            await self._emitter.emit(
                ctx.run_id,
                Notice(
                    run_id=ctx.run_id,
                    level="info",
                    message=f"standing approval: {call.tool_name} {_grant_detail(requester)}",
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

        # 检查分级审批模式（ADR 0033）
        risk = evaluate_policy(self._mode, facts).risk
        auto_approved = False
        auto_reason = ""
        if self._approval_mode is ApprovalMode.FULL:
            auto_approved = True
            auto_reason = (
                f"approval mode 'full': auto-approving {call.tool_name} (risk={risk.value})"
            )
        elif self._approval_mode is ApprovalMode.SMART and risk in (
            RiskLevel.NONE,
            RiskLevel.LOW,
            RiskLevel.MEDIUM,
        ):
            auto_approved = True
            auto_reason = (
                f"approval mode 'smart': auto-approving low/medium-risk {call.tool_name} "
                f"(risk={risk.value})"
            )

        if auto_approved:
            approval_id = new_approval_id()
            await self._store.record_approval(approval_id, ctx.run_id, digest)
            await self._store.decide_approval(approval_id, ApprovalDecision.APPROVED)
            await self._emitter.emit(
                ctx.run_id,
                Notice(
                    run_id=ctx.run_id,
                    level="info",
                    message=auto_reason,
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
            requester = _arming_requester(ctx, call.tool_name)
            if requester is not None:
                ctx.standing_grants[grant_key] = requester
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
            risk=risk,
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
        # 人类批准了这一次具体调用。它是否成为本次运行的常驻授权，取决于授权
        # 来自哪里：`repo.check` 永远授权（ADR 0025，与技能无关），其他工具需要
        # 被选中技能显式请求（ADR 0032）。
        requester = _arming_requester(ctx, call.tool_name)
        if requester is not None:
            ctx.standing_grants[grant_key] = requester
        ctx.move_to(RunStatus.EXECUTING_TOOL)
        return True, str(approval_id), ""


#: ADR 0025 的常驻授权不来自任何技能。用这个哨兵值标记，以免把策略说成技能。
CHECK_POLICY_GRANTOR = "the identical-check policy"


def _arming_requester(ctx: RunContext, tool_name: str) -> str | None:
    """一次人类批准是否应成为常驻授权，以及是哪个技能请求的。

    只在两个地方返回非 None：`repo.check`（ADR 0025 的既有行为，与技能无关），
    以及被选中技能显式列出 `allowed-tools` 的工具（ADR 0032）。返回 None 表示
    这次批准只覆盖它自己——这是"技能文本无法自行扩大权限"在代码里的落点。
    """
    if tool_name in ctx.granted_tools:
        return ctx.granted_tools[tool_name]
    if tool_name == "repo.check":
        return CHECK_POLICY_GRANTOR
    return None


def _grant_hint(requester: str | None) -> str:
    """审批卡上关于常驻授权的说明；没有技能请求该工具时为空。"""
    if requester is None or requester == CHECK_POLICY_GRANTOR:
        # ADR 0025 的 `repo.check` 卡片自己已经说明了这一点，不必重复。
        return ""
    return f"standing grant requested by skill {requester!r}"


def _with_grant_hint(summary: str, requester: str | None) -> str:
    """把授权说明追加到审批摘要上；没有摘要或没有技能请求时原样返回。"""
    hint = _grant_hint(requester)
    return f"{summary} ({hint})" if hint and summary else summary


def _grant_detail(requester: str) -> str:
    """授权生效时写入 journal 的那半句话。"""
    if requester == CHECK_POLICY_GRANTOR:
        return "was approved earlier in this run; re-running without asking"
    return (
        f"was approved earlier in this run at the request of skill {requester!r}; "
        "re-running without asking"
    )


def standing_grant_key(digest: str, granted_tools: Mapping[str, str]) -> str:
    """把一次审批摘要和当时的授权工具集组合成授权键。

    授权集参与键的计算，因此换一组技能就换一套授权：同一个调用在一组技能下
    免审，在另一组下不会莫名其妙地继续免审。
    """
    return sha256_text(f"{digest}|{'|'.join(sorted(granted_tools))}")
