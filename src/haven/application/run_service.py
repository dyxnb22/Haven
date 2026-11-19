"""RunService：负责一次运行的有界代理循环。

循环形态为 Model -> Tool(s) -> Observation -> Model……直到程序决定停止。每次停止都
只有一个原因；要判定成功，还必须通过证据门禁。

循环的一轮（见 `_drive`）按以下顺序执行：

    1. 预算检查              - 步数/工具调用/墙上时间/token/成本上限
                              （domain/budget.py）；超出任一上限就停止运行。
    2. 投递 steering         - 通过 `steer()` 排队的用户输入在此边界变成普通 user 消息，
                              绝不会插入流式输出中途（ADR 0020）。
    3. 构建上下文            - ContextBuilder 选择头部/历史/尾部，超预算时执行压缩，
                              并将分段报告到轨迹（`context.built`）。
    4. 模型流                - 提供商事件实时重新发送给 UI；流中途断开时由
                              ModelStreamer 以有界方式重试。
    5a. 工具调用             - 逐个交给 ToolPipeline；结果追加到 transcript；三次相同
                              调用加结果构成卡循环时停止运行。
    5b. 没有工具调用         - 文本成为候选最终答案；`finish_reason == "length"` 时执行
                              有界续写，而不是直接接受答案。
    6. 检查点                - 为崩溃恢复保存持久化快照。
    7. 完成                  - `_finish` 应用证据门禁：编辑过文件的运行只有在最后一次
                              写入之后记录了 diff 和通过的 check 时才成功，否则以停止
                              原因失败。

多轮会话：`continue_run` 恢复带检查点的 transcript 并追加后续请求（fork 表示从任意
较早的运行 ID 继续，见 ADR 0015/0020）；`rewind`（RecoveryService）是对已完成运行
文件的用户级撤销。
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from functools import wraps
from pathlib import Path
from typing import Concatenate

from haven.application.answer_resolution import (
    MAX_EMPTY_REPLIES as MAX_EMPTY_REPLIES,
)
from haven.application.answer_resolution import (
    MAX_EVIDENCE_NUDGES as MAX_EVIDENCE_NUDGES,
)
from haven.application.approvals import ApprovalResponder
from haven.application.emitter import EventEmitter
from haven.application.handoff import latest_handoff
from haven.application.mcp_client import MCPClientManager
from haven.application.model_catalog import ModelCatalog
from haven.application.model_router import ModelRouter, RunnableModel
from haven.application.model_stream import (
    MODEL_RETRY_MAX_DELAY,
    ModelStreamer,
)
from haven.application.model_stream import retry_delay as _retry_delay
from haven.application.profiles import profile_for
from haven.application.project_memory import ProjectMemoryService
from haven.application.registry import ToolRegistry
from haven.application.run_actor import RunActor
from haven.application.run_persistence import (
    CheckpointManager,
    RunFinalizer,
    RunOutcome,
)
from haven.application.run_telemetry import RunTelemetry
from haven.application.skill_grants import grant_notice, grant_table
from haven.application.skills import Skill, SkillCatalog, resolve_selected_skills
from haven.application.state import RunContext
from haven.application.tool_pipeline import ToolPipeline
from haven.application.verification import select_verification, validate_verification
from haven.contracts.capability import build_capability_snapshot
from haven.contracts.checkpoint import CheckpointV1
from haven.contracts.events import (
    DiffPreview,
    Notice,
    PlanStepView,
    PlanUpdated,
    RunCreated,
    UserInputAnswered,
    UserInputRequested,
)
from haven.contracts.guidance import GuidanceSnapshot
from haven.contracts.mcp import MCPServerConfig
from haven.contracts.model import ModelMessage
from haven.contracts.model_call import ModelSelection
from haven.contracts.project_memory import NoteAction
from haven.contracts.tools import RecipeSpec, ToolResult
from haven.contracts.user_input import UserInputResponse
from haven.domain.budget import Budget
from haven.domain.enums import (
    AgentMode,
    ApprovalMode,
    PermissionMode,
    PlanStatus,
    RunStatus,
    StopReason,
    ToolStatus,
)
from haven.domain.ids import RunId, new_run_id
from haven.domain.pricing import Pricing
from haven.ports.executor import ExecutorPort
from haven.ports.sandbox import SandboxLauncher
from haven.ports.session import SessionStorePort
from haven.ports.workspace import WorkspacePort

#: 在真实网络中，提供商的临时故障很常见，因此因一次故障丢掉整个运行不应是
#: 默认行为。实测：8 次在线运行中有 3 次在收到任何 token 前遇到 ConnectError，
#: 31 个真实仓库案例中另有 2 个后来因一次适配器仍判定为不可重试的错误而失败——
#: 这个循环的效果取决于该分类是否准确。
__all__ = [
    "MODEL_RETRY_MAX_DELAY",
    "RunActor",
    "RunOutcome",
    "RunService",
    "_retry_delay",
    "build_run_context_from_checkpoint",
]


def _uses_project_guidance[**RunArgs](
    method: Callable[Concatenate[RunService, RunArgs], Awaitable[RunOutcome]],
) -> Callable[Concatenate[RunService, RunArgs], Awaitable[RunOutcome]]:
    """把启动、存储等待和收尾一并算作忙碌，异常或取消时也归还占用。"""

    @wraps(method)
    async def guarded(
        service: RunService, /, *args: RunArgs.args, **kwargs: RunArgs.kwargs
    ) -> RunOutcome:
        service._require_guidance_ready()
        service._guidance_users += 1
        try:
            return await method(service, *args, **kwargs)
        finally:
            service._guidance_users -= 1

    return guarded


class RunService:
    """驱动一次有预算、有审计、可恢复的 Model → Tool → Observation 循环。"""

    def __init__(
        self,
        *,
        model: RunnableModel,
        workspace: WorkspacePort,
        executor: ExecutorPort,
        store: SessionStorePort,
        emitter: EventEmitter,
        approvals: ApprovalResponder,
        recipes: dict[str, RecipeSpec],
        mode: PermissionMode,
        budget: Budget,
        pricing: Pricing | None = None,
        pricing_is_override: bool | None = None,
        git_branch: str = "",
        git_commit: str = "",
        project_guidance: str = "",
        skills: SkillCatalog | None = None,
        launcher: SandboxLauncher | None = None,
        context_chars_override: int = 0,
        supports_prefix_continuation: bool | None = None,
        approval_mode: ApprovalMode = ApprovalMode.ASK,
        mcp_servers: tuple[MCPServerConfig, ...] = (),
        guidance_snapshot: GuidanceSnapshot | None = None,
        guidance_loader: Callable[[], Awaitable[GuidanceSnapshot]] | None = None,
        project_memory: ProjectMemoryService | None = None,
        verification_groups: dict[str, tuple[str, ...]] | None = None,
        model_catalog: ModelCatalog | None = None,
    ) -> None:
        self._model = model
        self._model_catalog = model_catalog
        self._profile_for = model_catalog.profile_for if model_catalog else profile_for
        self._streamer = ModelStreamer(emitter)
        self._workspace = workspace
        self._executor = executor
        self._store = store
        self._emitter = emitter
        self._approvals = approvals
        self._mode = mode
        self._budget = budget
        # 每个模型的默认值；未知模型继承 Haven 的历史行为，而不是使用根据
        # 相似名称猜出的数字。
        default_profile = self._profile_for(model.model_name)
        # 评估/A-B 对 profile 上下文预算的覆盖值（0 = 使用 profile）。这样压缩
        # A-B 可以通过缩小预算来强制提前压缩，而无需虚构一个模型。
        self._context_chars = context_chars_override
        # 原生前缀续写既是模型能力，也是 endpoint 事实：DeepSeek 只在 beta base
        # URL 接受 `prefix: true`。组合根解析两者后将结论传到这里；None 表示
        # “没有部署层面的判断”，因此由 profile 自己的标志决定。
        self._supports_prefix_override = supports_prefix_continuation
        # 已配置的费率优先，否则回退到模型公布的费率卡。报告实际使用模型的
        # 已记录价格优于报告 $0.00，但它是公布值而不是发票金额——见 profile
        # 中带日期的注释。
        self._pricing = pricing if pricing is not None else default_profile.pricing
        self._pricing_is_override = (
            pricing is not None if pricing_is_override is None else pricing_is_override
        )
        self._checkpoints = CheckpointManager(store, workspace, emitter)
        self._finalizer = RunFinalizer(
            store, emitter, self._checkpoints, cost_known=self._pricing.is_known
        )
        self._git_branch = git_branch
        self._git_commit = git_commit
        if guidance_snapshot is not None and guidance_snapshot.text != project_guidance:
            raise ValueError("guidance snapshot must match the loaded project guidance")
        self._guidance_snapshot = guidance_snapshot or GuidanceSnapshot(text=project_guidance)
        self._guidance_loader = guidance_loader
        self._guidance_reloading = False
        self._guidance_users = 0
        self._project_memory = project_memory
        self._skills = skills if skills is not None else SkillCatalog()
        # 启动时发现的技能。目录进每一次上下文，正文只在用户显式选中后进
        # （ADR 0031）；这里只存发现结果，不做选择——选择属于每一次 run。
        self._recipes = recipes
        self._verification_groups = dict(verification_groups or {})
        self._mcp_servers = mcp_servers
        self._mcp_manager = MCPClientManager(mcp_servers)
        self._registry = ToolRegistry()
        self._launcher = launcher
        self._actors: dict[str, RunActor] = {}
        self._tasks: dict[str, asyncio.Task[RunOutcome]] = {}
        self._empty_steer_queue: deque[str] = deque()
        self._active_run_id: str | None = None
        self._paused_run_ids: set[str] = set()
        self._pending_reviews: set[str] = set()
        # 每个运行一个独占临时目录，运行结束时删除。它使必须写入某处的沙箱工具
        # 不需要获得工作区之外的写权限。
        self._scratch_dir = Path(tempfile.mkdtemp(prefix="haven-scratch-"))
        self._scratch_ready = True
        self._pipeline = ToolPipeline(
            workspace=workspace,
            executor=executor,
            store=store,
            emitter=emitter,
            approvals=approvals,
            registry=self._registry,
            recipes=recipes,
            mode=mode,
            launcher=launcher,
            scratch_dir=self._scratch_dir,
            skills=self._skills,
            approval_mode=approval_mode,
            mcp_manager=self._mcp_manager,
        )

    @property
    def guidance_snapshot(self) -> GuidanceSnapshot:
        """返回实际传给新运行的项目指引快照，不重新读取工作区。"""
        return self._guidance_snapshot

    @property
    def verification_groups(self) -> dict[str, tuple[str, ...]]:
        """配置的可选检查组；调用者不能修改服务内的目录。"""
        return dict(self._verification_groups)

    def _select_model(self, preset: str | None) -> ModelSelection | None:
        if self._model_catalog is not None:
            return self._model_catalog.select(preset)
        if preset:
            raise ValueError("model presets require a configured Provider")
        return None

    def _restore_model(self, saved: ModelSelection | None) -> ModelSelection | None:
        if self._model_catalog is not None:
            return self._model_catalog.restore(saved)
        if saved is not None:
            raise ValueError("this run requires its configured Provider to resume")
        return None

    def _model_for(self, ctx: RunContext) -> RunnableModel:
        if self._model_catalog is not None and ctx.model_selection is not None:
            return self._model_catalog.bind(ctx.model_selection)
        return self._model

    @property
    def is_reloading_guidance(self) -> bool:
        return self._guidance_reloading

    async def reload_guidance(self) -> GuidanceSnapshot:
        """仅在空闲时替换下一次运行的项目资料，不重新发现权限或技能。"""
        if self._guidance_loader is None:
            raise ValueError("project guidance reload is unavailable in this session")
        return await self._replace_guidance(self._guidance_loader)

    async def update_project_notes(self, action: NoteAction, value: str = "") -> GuidanceSnapshot:
        """笔记只接受用户界面传来的显式操作，不注册为模型工具。"""
        memory = self._project_memory
        if memory is None:
            raise ValueError("project notes are unavailable in this session")

        async def updated() -> GuidanceSnapshot:
            notes = await memory.update(action, value)
            return self._guidance_snapshot.model_copy(update={"notes": notes})

        return await self._replace_guidance(updated)

    async def _replace_guidance(
        self, loader: Callable[[], Awaitable[GuidanceSnapshot]]
    ) -> GuidanceSnapshot:
        self._require_guidance_ready()
        self._require_guidance_idle()
        self._guidance_reloading = True
        try:
            snapshot = await loader()
            self._require_guidance_idle()
            self._guidance_snapshot = snapshot
            return snapshot
        finally:
            self._guidance_reloading = False

    def _require_guidance_ready(self) -> None:
        if self._guidance_reloading:
            raise ValueError("project guidance is reloading; wait for it to finish")

    def _require_guidance_idle(self) -> None:
        if self._guidance_users or self._actors or self._tasks or self._active_run_id is not None:
            raise ValueError("a run is active; wait for it to finish before reloading guidance")
        if self._paused_run_ids or self._pending_reviews:
            raise ValueError("finish the pending question or review before reloading guidance")

    @property
    def _steer_queue(self) -> deque[str]:
        """向后兼容属性：返回当前活跃 actor 的转向队列。"""
        if self._active_run_id is not None:
            actor = self._actors.get(self._active_run_id)
            if actor is not None:
                return actor.steer_queue
        return self._empty_steer_queue

    @property
    def approval_mode(self) -> ApprovalMode:
        """当前的审批分级模式（ask / smart / full）。"""
        return self._pipeline.approval_mode

    def set_approval_mode(self, mode: ApprovalMode) -> None:
        """动态更新当前的审批分级模式。"""
        self._pipeline.set_approval_mode(mode)
        for actor in self._actors.values():
            actor.set_approval_mode(mode)

    # -- 入口 -----------------------------------------------------------------

    @_uses_project_guidance
    async def run(
        self,
        goal: str,
        *,
        skill_names: Sequence[str] = (),
        approval_mode: ApprovalMode | None = None,
        agent_mode: AgentMode = AgentMode.AGENT,
        require_review: bool = False,
        check_group: str | None = None,
        model_preset: str | None = None,
    ) -> RunOutcome:
        """创建运行并执行主循环，最终结果由证据门禁裁定。

        技能在这里解析，而不是在调用方：选中一个不在目录里的名字会让这次运行
        拒绝启动，而不是静默少一段上下文（ADR 0031）。
        """
        self._require_resolved_session()
        guidance = self._guidance_snapshot
        model_selection = self._select_model(model_preset)
        agent_mode = AgentMode(agent_mode)
        if approval_mode is not None:
            self.set_approval_mode(approval_mode)
        goal = goal.strip()
        if not (3 <= len(goal) <= 4000):
            raise ValueError("goal must be between 3 and 4000 characters")
        selected = resolve_selected_skills(self._skills.skills, skill_names)
        verification = select_verification(check_group, self._verification_groups, self._recipes)
        validate_verification(verification, self._recipes, self._mode, agent_mode)
        if not self._actors:
            self._workspace.restore_originals({})
        run_id = new_run_id()
        await self._initialize_mcp(run_id)
        snapshot = build_capability_snapshot(self._mcp_manager.tools if self._mcp_manager else ())

        ctx = RunContext(
            run_id=run_id,
            goal=goal,
            mode=PermissionMode.READ_ONLY if agent_mode is AgentMode.PLAN else self._mode,
            budget=self._budget,
            agent_mode=agent_mode,
            require_review=require_review,
            plan_status=PlanStatus.DRAFTING if agent_mode is AgentMode.PLAN else None,
            catalog_digest=snapshot.catalog_digest,
            capabilities=snapshot,
            guidance_snapshot=guidance,
            verification=verification,
            model_selection=model_selection,
            **grant_table(selected),
        )
        await self._announce_run(ctx, goal)
        await self._announce_grants(ctx, selected)
        return await self._drive(ctx, selected_skills=selected)

    async def continue_run(
        self,
        previous_run_id: str,
        follow_up: str,
        *,
        skill_names: Sequence[str] = (),
        approval_mode: ApprovalMode | None = None,
        check_group: str | None = None,
        model_preset: str | None = None,
    ) -> RunOutcome:
        """延续历史会话；规划会话中的普通追问仍然只读。"""
        return await self._continue_run(
            previous_run_id,
            follow_up,
            skill_names=skill_names,
            approval_mode=approval_mode,
            check_group=check_group,
            model_preset=model_preset,
        )

    async def implement_plan(
        self,
        previous_run_id: str,
        *,
        skill_names: Sequence[str] = (),
        approval_mode: ApprovalMode | None = None,
        check_group: str | None = None,
        model_preset: str | None = None,
    ) -> RunOutcome:
        """用户显式确认待执行计划；新运行仍须通过正常审批和证据门禁。"""
        return await self._continue_run(
            previous_run_id,
            "Implement the approved plan. Update task.plan as you complete each step.",
            skill_names=skill_names,
            approval_mode=approval_mode,
            implement=True,
            check_group=check_group,
            model_preset=model_preset,
        )

    @_uses_project_guidance
    async def _continue_run(
        self,
        previous_run_id: str,
        follow_up: str,
        *,
        skill_names: Sequence[str],
        approval_mode: ApprovalMode | None,
        implement: bool = False,
        check_group: str | None = None,
        model_preset: str | None = None,
    ) -> RunOutcome:
        """启动继承此前运行 transcript 的后续轮次。

        之前的对话会被带入，使模型拥有上下文，但这是一次全新的 Run：新的 ID、新的
        预算和新的证据账本，因此后续轮次的成功只根据其自身编辑来判断。会话的头部目标
        保持稳定（有利于提示缓存），后续内容作为 user 消息接入。
        """
        self._require_resolved_session()
        guidance = self._guidance_snapshot
        model_selection = self._select_model(model_preset)
        if approval_mode is not None:
            self.set_approval_mode(approval_mode)
        follow_up = follow_up.strip()
        if not (3 <= len(follow_up) <= 4000):
            raise ValueError("follow-up must be between 3 and 4000 characters")
        selected = resolve_selected_skills(self._skills.skills, skill_names)

        checkpoint = await self._store.load_checkpoint(previous_run_id)
        if checkpoint is None:
            raise ValueError(f"no checkpoint for run {previous_run_id!r}; cannot continue it")
        if checkpoint.pending_input is not None:
            raise ValueError("the run needs input; answer its pending question before continuing")
        if (
            checkpoint.status == RunStatus.SUCCEEDED.value
            and checkpoint.require_review
            and (
                checkpoint.review is None
                or not (checkpoint.review.applied or checkpoint.review.discarded)
            )
            and checkpoint.evidence.edits
        ):
            raise ValueError("review the isolated changes before continuing this session")
        if checkpoint.parent_run_id:
            raise ValueError("delegated runs cannot be continued as user sessions")
        if implement:
            if self._mode is PermissionMode.READ_ONLY:
                raise ValueError("implementing a plan requires a writable session")
            if (
                checkpoint.agent_mode is not AgentMode.PLAN
                or checkpoint.plan_status is not PlanStatus.READY
                or checkpoint.status != RunStatus.SUCCEEDED.value
                or not checkpoint.plan
            ):
                raise ValueError("the run has no ready plan; complete planning before implementing")
            if any(step.status == "blocked" for step in checkpoint.plan):
                raise ValueError("resolve blocked plan steps before implementing")

        # 拒绝将某次运行的 transcript 和文件状态嫁接到另一个仓库：恢复流程
        # 会执行此检查，后续轮次也必须执行。
        if checkpoint.workspace_digest != self._workspace.workspace_digest:
            raise ValueError(
                "workspace identity changed since the run being continued; "
                "continue it from the same workspace"
            )

        agent_mode = AgentMode.AGENT if implement else checkpoint.agent_mode
        # None 继承上一轮的固定要求；空字符串仅代表用户显式清除新轮次的要求。
        verification = (
            checkpoint.verification
            if check_group is None
            else select_verification(check_group, self._verification_groups, self._recipes)
        )
        validate_verification(verification, self._recipes, self._mode, agent_mode)

        run_id = new_run_id()
        await self._initialize_mcp(run_id)
        current_snapshot = build_capability_snapshot(
            self._mcp_manager.tools if self._mcp_manager else ()
        )
        if (
            checkpoint.catalog_digest
            and checkpoint.catalog_digest != current_snapshot.catalog_digest
        ):
            saved_d = checkpoint.catalog_digest[:12]
            curr_d = current_snapshot.catalog_digest[:12]
            raise ValueError(
                f"capability catalog changed since checkpoint (saved {saved_d}, "
                f"current {curr_d}); start a new run instead"
            )

        # 后续轮次是新的 turn：它的运行 diff 必须只显示自身的改动，因此要重置
        # 上一轮的运行范围原始内容（在新进程中无害，但在会话复用的长期 TUI
        # 工作区中是必需的）。
        self._workspace.restore_originals({})

        transcript = list(checkpoint.messages)
        transcript.append(ModelMessage(role="user", content=f"Follow-up request: {follow_up}"))
        ctx = RunContext(
            run_id=run_id,
            goal=checkpoint.goal,
            mode=PermissionMode.READ_ONLY if agent_mode is AgentMode.PLAN else self._mode,
            budget=self._budget,
            transcript=transcript,
            plan=checkpoint.plan,
            require_review=checkpoint.require_review,
            agent_mode=agent_mode,
            plan_status=(
                PlanStatus.DRAFTING
                if agent_mode is AgentMode.PLAN
                else PlanStatus.EXECUTING
                if checkpoint.plan
                else None
            ),
            files_read=dict(checkpoint.files_read),
            catalog_digest=current_snapshot.catalog_digest,
            capabilities=current_snapshot,
            guidance_snapshot=guidance,
            verification=verification,
            model_selection=model_selection,
            **grant_table(selected),
        )
        ctx.handoff = await latest_handoff(self._store, previous_run_id, checkpoint.handoff)
        await self._announce_run(ctx, checkpoint.goal, parent_run_id=previous_run_id)
        await self._announce_grants(ctx, selected)
        await self._emitter.emit(
            ctx.run_id,
            Notice(run_id=ctx.run_id, level="info", message=f"continuing session: {follow_up}"),
        )
        return await self._drive(ctx, selected_skills=selected)

    async def _announce_grants(self, ctx: RunContext, selected: tuple[Skill, ...]) -> None:
        """把被选中技能请求的常驻授权写进 journal（ADR 0032）。

        授权必须在运行开始前被说出来：一个刚 clone 的仓库给自己授予写权限，这件事
        必须出现在轨迹里，而不是只发生在策略内部。没有技能请求授权时什么都不写。
        """
        notice = grant_notice(selected)
        if notice:
            await self._emitter.emit(
                ctx.run_id, Notice(run_id=ctx.run_id, level="warning", message=notice)
            )

    async def _announce_run(self, ctx: RunContext, goal: str, *, parent_run_id: str = "") -> None:
        await self._store.create_run(
            ctx.run_id,
            str(self._workspace.root),
            self._workspace.workspace_digest,
            goal,
            ctx.mode.value,
        )
        await self._emitter.emit(
            ctx.run_id,
            RunCreated(
                run_id=ctx.run_id,
                workspace=str(self._workspace.root),
                workspace_digest=self._workspace.workspace_digest,
                goal=goal,
                mode=ctx.mode.value,
                agent_mode=ctx.agent_mode.value,
                model_name=self._model_for(ctx).model_name,
                model_preset=ctx.model_selection.preset_name if ctx.model_selection else "",
                git_branch=self._git_branch,
                git_commit=self._git_commit,
                max_steps=self._budget.max_steps,
                sandbox_backend=(self._launcher.backend if self._launcher is not None else "none"),
                parent_run_id=parent_run_id,
                catalog_digest=ctx.catalog_digest,
                verification=ctx.verification,
            ),
        )
        if ctx.plan:
            await self._emitter.emit(
                ctx.run_id,
                PlanUpdated(
                    run_id=ctx.run_id,
                    steps=tuple(
                        PlanStepView.model_validate(step.model_dump()) for step in ctx.plan
                    ),
                    plan_status=ctx.plan_status.value if ctx.plan_status else "",
                ),
            )

    @_uses_project_guidance
    async def resume(self, ctx: RunContext) -> RunOutcome:
        """继续由 RecoveryService 构建的已恢复运行上下文。

        恢复的运行重新发现技能目录，但不恢复被选中的正文：选择没有进 checkpoint，
        因此这里只能得到目录。这是 v1 明确的取舍——把选择写进 checkpoint 会让
        磁盘上的技能改动与已检查点头部不一致，而那正是检查点要避免的（ADR 0031）。
        """
        self._require_guidance_ready()
        if ctx.parent_run_id:
            raise ValueError(
                "delegated runs are managed by their parent and cannot be resumed alone"
            )
        validate_verification(ctx.verification, self._recipes, self._mode, ctx.agent_mode)
        if ctx.guidance_snapshot is None:
            ctx.guidance_snapshot = self._guidance_snapshot
            await self._emitter.emit(
                ctx.run_id,
                Notice(
                    run_id=ctx.run_id,
                    level="warning",
                    message=(
                        "legacy checkpoint has no saved project guidance; "
                        "using the current loaded snapshot"
                    ),
                ),
            )
        if ctx.pending_input is not None:
            self._paused_run_ids.add(ctx.run_id)
            outcome = await self._finalizer.pause_for_input(ctx)
            await self._emitter.emit(
                ctx.run_id, UserInputRequested(run_id=ctx.run_id, request=ctx.pending_input)
            )
            return outcome
        ctx.model_selection = self._restore_model(ctx.model_selection)
        await self._initialize_mcp(ctx.run_id)
        current_snapshot = build_capability_snapshot(
            self._mcp_manager.tools if self._mcp_manager else ()
        )
        if ctx.catalog_digest and ctx.catalog_digest != current_snapshot.catalog_digest:
            saved_d = ctx.catalog_digest[:12]
            curr_d = current_snapshot.catalog_digest[:12]
            raise ValueError(
                f"capability catalog changed since checkpoint (saved {saved_d}, "
                f"current {curr_d}); start a new run instead"
            )
        ctx.catalog_digest = current_snapshot.catalog_digest
        ctx.capabilities = current_snapshot
        if self._mode is PermissionMode.READ_ONLY or ctx.agent_mode is AgentMode.PLAN:
            ctx.mode = PermissionMode.READ_ONLY

        await self._emitter.emit(
            ctx.run_id,
            Notice(run_id=ctx.run_id, level="info", message="run resumed from checkpoint"),
        )
        return await self._drive(ctx)

    @property
    def has_pending_input(self) -> bool:
        """暂停的工作区仍需要保留，不能在关闭会话时清理。"""
        return bool(self._paused_run_ids)

    @property
    def has_pending_review(self) -> bool:
        return bool(self._pending_reviews)

    def mark_reviewed(self, run_id: str) -> None:
        self._pending_reviews.discard(run_id)

    def _require_resolved_session(self) -> None:
        self._require_guidance_ready()
        if self._pending_reviews:
            raise ValueError("review the isolated changes before starting another run")
        if self._paused_run_ids:
            raise ValueError(
                "the run needs input; answer or cancel its pending question "
                "before starting another run"
            )

    @_uses_project_guidance
    async def answer_run(self, run_id: str, response: UserInputResponse) -> RunOutcome:
        """验证回答并恢复同一次运行；回答不能提升检查点的权限。"""
        from haven.application.recovery_service import RecoveryService

        if run_id in self._actors:
            raise ValueError("the run is still active; wait until its question is ready")
        checkpoint = await self._store.load_checkpoint(run_id)
        if checkpoint is None or checkpoint.pending_input is None:
            raise ValueError("the run has no pending question")
        self._paused_run_ids.add(run_id)
        checkpoint.pending_input.validate_response(response)
        model_selection = checkpoint.model_selection
        if response.action != "cancel":
            model_selection = self._restore_model(model_selection)
            validate_verification(
                checkpoint.verification, self._recipes, self._mode, checkpoint.agent_mode
            )
            await self._initialize_mcp(run_id)
            current = build_capability_snapshot(self._mcp_manager.tools)
            if checkpoint.catalog_digest and checkpoint.catalog_digest != current.catalog_digest:
                raise ValueError("capability catalog changed; cannot answer this saved run")
        recovery = RecoveryService(self._store, self._workspace)
        report = await recovery.inspect(run_id)
        if not report.can_resume:
            raise ValueError("cannot resume: " + "; ".join(report.blockers))
        ctx = await recovery.build_context(checkpoint)
        ctx.model_selection = model_selection
        if self._mode is PermissionMode.READ_ONLY or ctx.agent_mode is AgentMode.PLAN:
            ctx.mode = PermissionMode.READ_ONLY
        result = ToolResult(
            call_id=checkpoint.pending_input.call_id,
            tool_name="user.ask",
            status=ToolStatus.OK,
            payload=response.model_dump(mode="json"),
        )
        ctx.transcript.append(
            ModelMessage(
                role="tool",
                tool_call_id=result.call_id,
                content=f'<tool_output tool="user.ask">\n{result.to_model_text()}\n</tool_output>',
            )
        )
        ctx.pending_input = None
        if response.action == "cancel":
            await self._emitter.emit(
                ctx.run_id, UserInputAnswered(run_id=run_id, response=response)
            )
            outcome = await self._finish(ctx, RunStatus.CANCELLED, StopReason.CANCELLED, "")
            self._paused_run_ids.discard(run_id)
            return outcome
        ctx.move_to(RunStatus.RUNNING_MODEL)
        # 先保存回答，再允许模型运行。中途退出时，答案也不会丢失或被重复消费。
        await self._checkpoints.save(ctx)
        await self._store.update_run_status(run_id, ctx.status, "")
        await self._emitter.emit(ctx.run_id, UserInputAnswered(run_id=run_id, response=response))
        return await self.resume(ctx)

    @property
    def active_run_id(self) -> str | None:
        """返回当前正在驱动的运行 ID；没有运行时返回 None。"""
        return self._active_run_id

    async def get_diff(self, run_id: str = "") -> str:
        """活动运行显示当前差异，历史运行显示当时保存的 diff 构件。"""
        if run_id and run_id != self._active_run_id:
            checkpoint = await self._store.load_checkpoint(run_id)
            if checkpoint and checkpoint.evidence.diffs:
                data = await self._store.get_artifact(checkpoint.evidence.diffs[-1].diff_digest)
                if data is not None:
                    return data.decode("utf-8", errors="replace")
            # 构件拿不到（或 checkpoint 里没有 diff 记录）时回退到事件流。这一段必须和
            # 上面的 if 平级：原先被错缩进在里面，于是"没有带 diff 的 checkpoint"这个
            # 常见情况下它永远是死代码——`haven://runs/{id}/diff` 因此恒返回空。
            for envelope in reversed(await self._store.load_events(run_id)):
                if isinstance(envelope.event, DiffPreview):
                    return envelope.event.preview
            return ""
        return (await self._workspace.run_diff()).diff

    @property
    def workspace_digest(self) -> str:
        return self._workspace.workspace_digest

    async def _initialize_mcp(self, run_id: str) -> None:
        """暂停会关闭外部进程；再次执行前重新发现并验证冻结目录。"""
        if self._mcp_servers and not self._mcp_manager.tools:
            tools = await self._mcp_manager.initialize()
            self._registry.register_mcp_tools(tools)
            for name, reason in self._mcp_manager.failures:
                await self._emitter.emit(
                    run_id,
                    Notice(
                        run_id=run_id,
                        level="warning",
                        message=(
                            f"MCP server {name!r} could not be started "
                            f"({reason}); its tools are unavailable this run"
                        ),
                    ),
                )

    async def close(self) -> None:
        """释放后台进程与尚未被运行生命周期删除的临时目录；重复调用保持安全。"""
        try:
            for actor in list(self._actors.values()):
                await actor.close()
            self._actors.clear()
            await self._pipeline.close()
        finally:
            try:
                await self._mcp_manager.close_all()
            finally:
                try:
                    if self._model_catalog is not None:
                        await self._model_catalog.close()
                    elif callable(closer := getattr(self._model, "aclose", None)):
                        await closer()
                finally:
                    if self._active_run_id is None and self._scratch_ready:
                        shutil.rmtree(self._scratch_dir, ignore_errors=True)
                        self._scratch_ready = False

    async def steer(self, text: str, *, run_id: str | None = None) -> bool:
        """为活跃运行排队用户输入，并在下一轮边界投递。

        当前模型调用和正在执行的工具都不会被打断；文本会在下一次模型请求前变成 user
        消息。没有活跃运行时返回 False（调用方应改为启动运行或后续轮次）。排队文本
        会立即写入日志，因此可以跨越崩溃保留。
        """
        target_id = run_id or self._active_run_id
        if target_id is None:
            return False
        actor = self._actors.get(target_id)
        if actor is not None:
            return await actor.steer(text)
        return False

    async def cancel(self, run_id: str) -> bool:
        """尝试取消指定运行的异步执行任务。"""
        task = self._tasks.get(run_id)
        if task is not None and not task.done():
            task.cancel()
            return True
        checkpoint = await self._store.load_checkpoint(run_id)
        if checkpoint is not None and checkpoint.pending_input is not None:
            await self.answer_run(
                run_id,
                UserInputResponse(request_id=checkpoint.pending_input.request_id, action="cancel"),
            )
            return True
        return False

    # -- 循环 ------------------------------------------------------------------

    def _create_actor(
        self,
        ctx: RunContext,
        *,
        selected_skills: tuple[Skill, ...] = (),
        scratch_dir: Path | None = None,
    ) -> RunActor:
        """为单次运行构建自治隔离的 RunActor 实例。"""
        model = self._model_for(ctx)
        profile = self._profile_for(model.model_name)
        supports_prefix = self._supports_prefix_override
        if self._model_catalog is not None and ctx.model_selection is not None:
            supports_prefix = self._model_catalog.supports_prefix(ctx.model_selection)
        pricing = self._pricing if self._pricing_is_override else profile.pricing
        price_known = pricing.is_known
        if isinstance(model, ModelRouter) and not self._pricing_is_override:
            price_known = all(
                self._profile_for(port.model_name).pricing.is_known
                for port in (model.fast_model, model.deep_model)
            )
        return RunActor(
            ctx=ctx,
            model=model,
            streamer=self._streamer,
            workspace=self._workspace,
            executor=self._executor,
            store=self._store,
            emitter=self._emitter,
            approvals=self._approvals,
            recipes=self._recipes,
            mode=ctx.mode,
            profile=profile,
            context_chars=self._context_chars or profile.max_context_chars,
            supports_prefix=profile.supports_assistant_prefix
            if supports_prefix is None
            else supports_prefix,
            pricing=pricing,
            pricing_is_override=self._pricing_is_override,
            context_is_override=bool(self._context_chars),
            resolve_profile=self._profile_for,
            telemetry=RunTelemetry(self._emitter, pricing),
            checkpoints=self._checkpoints,
            finalizer=RunFinalizer(
                self._store, self._emitter, self._checkpoints, cost_known=price_known
            ),
            skills=self._skills,
            selected_skills=selected_skills,
            launcher=self._launcher,
            approval_mode=self.approval_mode,
            mcp_manager=self._mcp_manager,
            registry=self._registry,
            project_guidance=(ctx.guidance_snapshot or self._guidance_snapshot).text,
            scratch_dir=scratch_dir,
            mcp_servers=self._mcp_servers,
        )

    async def _drive(
        self, ctx: RunContext, *, selected_skills: tuple[Skill, ...] = ()
    ) -> RunOutcome:
        """轮次循环，委托给自治 RunActor 隔离执行。"""
        if ctx.guidance_snapshot is None:
            ctx.guidance_snapshot = self._guidance_snapshot
        # 冻结编辑窗口后再读显式修订；不重写证据或权限。
        previous = await self._store.get_run(ctx.run_id)
        await self._store.update_run_status(ctx.run_id, RunStatus.RUNNING_MODEL, "")
        try:
            ctx.handoff = await latest_handoff(self._store, ctx.run_id, ctx.handoff)
        except BaseException:
            if previous is not None:
                await self._store.update_run_status(
                    ctx.run_id, previous.status, previous.stop_reason
                )
            raise
        if not self._scratch_ready or not self._scratch_dir.exists():
            self._scratch_dir = Path(tempfile.mkdtemp(prefix=f"haven-scratch-{ctx.run_id[:8]}-"))
            self._scratch_ready = True

        actor = self._create_actor(
            ctx, selected_skills=selected_skills, scratch_dir=self._scratch_dir
        )
        self._actors[ctx.run_id] = actor
        self._active_run_id = ctx.run_id
        await self._pipeline.replace_scratch_dir(self._scratch_dir)
        # 记的是**运行自己的** task，不是调用方的。`_drive` 是被 `run()` 调用的，
        # 而 `run()` 又可能被别的请求处理器 await——那时 `current_task()` 拿到的是那个
        # 请求的 task，`cancel()` 就会把取消请求自己干掉，而不是干掉这次运行。
        drive_task = asyncio.create_task(actor.drive())
        self._tasks[ctx.run_id] = drive_task
        try:
            outcome = await drive_task
        finally:
            self._tasks.pop(ctx.run_id, None)
            self._actors.pop(ctx.run_id, None)
            if self._active_run_id == ctx.run_id:
                self._active_run_id = None
            if self._scratch_ready and self._scratch_dir.exists():
                shutil.rmtree(self._scratch_dir, ignore_errors=True)
            self._scratch_ready = False
        if outcome.pending_input is not None:
            self._paused_run_ids.add(ctx.run_id)
            # 此时旧 Actor 已退出且检查点已保存，界面立即回答也不会与暂停写入竞争。
            await self._emitter.emit(
                ctx.run_id, UserInputRequested(run_id=ctx.run_id, request=outcome.pending_input)
            )
        else:
            self._paused_run_ids.discard(ctx.run_id)
        if ctx.require_review and ctx.ledger.has_edits and outcome.status is RunStatus.SUCCEEDED:
            self._pending_reviews.add(ctx.run_id)
        return outcome

    # -- 持久化 ------------------------------------------------------------------

    async def _checkpoint(self, ctx: RunContext) -> None:
        ctx.refresh_wall_time(time.monotonic())
        await self._checkpoints.save(ctx)

    async def _finish(
        self,
        ctx: RunContext,
        status: RunStatus,
        stop_reason: StopReason,
        final_text: str,
        gate_reason: str = "",
    ) -> RunOutcome:
        """唯一出口：清理后台任务、持久化终态、检查点并发出 run.finished。"""
        await self._pipeline.close()
        ctx.refresh_wall_time(time.monotonic())
        return await self._finalizer.finish(ctx, status, stop_reason, final_text, gate_reason)


def build_run_context_from_checkpoint(
    checkpoint: CheckpointV1,
) -> RunContext:
    """根据检查点重建工作状态（供恢复流程使用）。"""
    ctx = RunContext(
        run_id=RunId(checkpoint.run_id),
        goal=checkpoint.goal,
        mode=PermissionMode(checkpoint.mode),
        budget=checkpoint.budget.to_domain(),
        usage=checkpoint.usage.to_domain(),
        transcript=list(checkpoint.messages),
        ledger=checkpoint.evidence.to_domain(),
        files_read=dict(checkpoint.files_read),
        plan=checkpoint.plan,
        agent_mode=checkpoint.agent_mode,
        plan_status=checkpoint.plan_status,
        pending_input=checkpoint.pending_input,
        parent_run_id=checkpoint.parent_run_id,
        root_run_id=checkpoint.root_run_id,
        file_scope=checkpoint.file_scope,
        require_review=checkpoint.require_review,
        last_seq=checkpoint.last_seq,
        catalog_digest=checkpoint.catalog_digest,
        guidance_snapshot=checkpoint.guidance_snapshot,
        verification=checkpoint.verification,
        model_selection=checkpoint.model_selection,
        handoff=checkpoint.handoff,
    )
    # 恢复后的运行从模型轮次继续。
    ctx.status = RunStatus.WAITING_USER if checkpoint.pending_input else RunStatus.RUNNING_MODEL
    return ctx
