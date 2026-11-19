"""基于 Textual 的 Haven TUI。

TUI 仅负责界面：将用户意图转换为服务调用，并渲染共享应用事件流中的
PresenterState。它不会执行工具、决定策略，也不会直接与提供商通信。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import Protocol

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, RichLog, Static, TabbedContent, TabPane
from textual.worker import Worker

from haven.application.approvals import QueueApprovalBroker
from haven.application.recovery_service import RecoveryService
from haven.application.run_service import RunOutcome, RunService
from haven.application.skills import SkillCatalog
from haven.application.state import RunContext
from haven.config import ResolvedConfig
from haven.contracts.events import (
    TRANSIENT_KINDS,
    ApprovalRequested,
    ChildStarted,
    EventEnvelope,
    UserInputRequested,
)
from haven.contracts.handoff import HandoffNotes
from haven.contracts.project_memory import NoteAction
from haven.contracts.user_input import UserInputResponse
from haven.domain.enums import AgentMode, ApprovalDecision, ApprovalMode, PermissionMode, RunStatus
from haven.interfaces.feedback import bell_enabled, error_hint, event_hint, needs_attention
from haven.interfaces.model_display import model_call_text, model_spec_lines
from haven.interfaces.run_stats import run_stats_text
from haven.interfaces.tui.commands import HELP_TEXT, route_command
from haven.interfaces.tui.diff_review import DiffReviewScreen
from haven.interfaces.tui.handoff import HandoffAction, HandoffScreen
from haven.interfaces.tui.input_helpers import expand_mentions
from haven.interfaces.tui.memory import ProjectMemoryScreen
from haven.interfaces.tui.presenter import PresenterState, TimelineEntry, reduce, sanitize
from haven.interfaces.tui.process_output import ProcessOutputScreen
from haven.interfaces.tui.prompt import PromptInput
from haven.interfaces.tui.questions import QuestionScreen
from haven.interfaces.tui.rendering import panel_text, timeline_text, verification_text
from haven.interfaces.tui.session_actions import export_run_text, list_sessions_text, rewind_text
from haven.interfaces.tui.sessions import SessionPickerScreen, SessionSelection
from haven.ports.session import SessionStorePort

__all__ = ["HELP_TEXT", "ApprovalScreen", "HavenApp", "QueueSink"]


class SessionServices(Protocol):
    """TUI 实际使用的组合应用子集。

    有意采用结构化协议：生产环境传入 bootstrap 的 AppServices，测试传入轻量替身；
    这样 TUI 与组合根解耦，同时 mypy 仍会检查它访问的每个属性。成员都是只读属性，
    因此实现可以声明更具体的类型（例如 `store` 使用 AppServices 的
    SqliteSessionStore）。
    """

    @property
    def run_service(self) -> RunService:
        """返回驱动模型运行、继续和 steering 的应用服务。"""
        ...

    @property
    def recovery(self) -> RecoveryService:
        """返回检查点恢复和工作区调和服务。"""
        ...

    @property
    def store(self) -> SessionStorePort:
        """返回用于会话列表和报告导出的存储端口。"""
        ...

    @property
    def config(self) -> ResolvedConfig:
        """返回预算等已解析运行配置。"""
        ...

    @property
    def git_branch(self) -> str:
        """返回启动时记录的 Git 分支名。"""
        ...

    @property
    def model_name(self) -> str:
        """返回当前运行所配置的模型名。"""
        ...

    @property
    def lease_warning(self) -> str:
        """返回工作区降级为只读时需要展示的警告文本。"""
        ...

    @property
    def skills(self) -> SkillCatalog:
        """返回启动时发现的技能目录（含被拒绝的文件及原因）。"""
        ...

    async def reconcile_worktree_if_succeeded(self, outcome: RunOutcome) -> list[str]:
        """如果处于临时工作树隔离模式且运行成功，将变更合流回主工作区。"""
        ...

    async def close(self) -> None:
        """关闭组合根创建的存储、模型和租约资源。"""
        ...


ServicesBuilder = Callable[..., Awaitable[SessionServices]]


class QueueSink:
    """运行时到 UI 的有界桥接。压力过大时丢弃临时增量；权威事件则施加反压。"""

    def __init__(self, maxsize: int = 256) -> None:
        self.queue: asyncio.Queue[EventEnvelope] = asyncio.Queue(maxsize=maxsize)

    async def emit(self, envelope: EventEnvelope) -> None:
        """将事件放入 UI 队列；队列满时仅丢弃临时增量事件。"""
        if envelope.event.kind in TRANSIENT_KINDS and self.queue.full():
            return
        await self.queue.put(envelope)


class ApprovalScreen(ModalScreen[bool]):
    """绑定摘要的审批对话框：一次性准确展示即将执行的内容。"""

    BINDINGS = [
        Binding("a", "approve", "Approve"),
        Binding("r", "reject", "Reject"),
        Binding("escape", "reject", "Reject"),
    ]

    def __init__(self, request: ApprovalRequested) -> None:
        super().__init__()
        self._request = request

    def compose(self) -> ComposeResult:
        """声明审批摘要、预览、摘要指纹和批准/拒绝按钮布局。"""
        req = self._request
        with Vertical(id="approval-box"):
            yield Static(f"[b]Approval required[/b] — risk: {req.risk}", id="approval-title")
            yield Static(sanitize(req.summary, 300), id="approval-summary")
            with VerticalScroll(id="approval-preview-scroll"):
                yield Static(sanitize(req.preview, 4000) or "(no preview)", id="approval-preview")
            yield Static(
                "[dim]This approves ONLY this exact action (digest "
                f"{req.request_digest[:12]}…). Any change invalidates it.[/dim]",
                id="approval-digest",
            )
            with Vertical(id="approval-buttons"):
                yield Button("Approve (a)", id="approve", variant="success")
                yield Button("Reject (r)", id="reject", variant="error")

    @on(Button.Pressed, "#approve")
    def _on_approve(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#reject")
    def _on_reject(self) -> None:
        self.dismiss(False)

    def action_approve(self) -> None:
        """以批准结果关闭审批模态框。"""
        self.dismiss(True)

    def action_reject(self) -> None:
        """以拒绝结果关闭审批模态框。"""
        self.dismiss(False)


class HavenApp(App[None]):
    """Haven 的主 TUI 应用。

    数据单向流动：用户输入 -> 服务调用 -> 应用事件 -> presenter.reduce
    -> PresenterState -> widgets。应用不会直接修改运行状态，只渲染事件流所
    表示已经发生的事情。

    输入处理（`_on_submit`）：`/commands` 在本地分发；普通文本会启动一次运行、
    继续上一次运行、在 `/fork RUN_ID` 后创建分支，或者在运行活动时作为 steering
    排队，等待下一次轮次边界。`@path` 提及会展开成明确的说明交给代理。审批以
    `approval.requested` 事件到达，并通过连接到 QueueApprovalBroker 的模态框
    （`ApprovalScreen`）回答，因此人类决定与自动审批者使用同一通道。
    """

    TITLE = "Haven"
    CSS = """
    #header { height: 1; background: $primary-darken-2; color: $text; padding: 0 1; }
    #timeline { height: 1fr; border: solid $primary-darken-3; }
    #tabs { height: 45%; }
    #prompt { dock: bottom; }
    #status { dock: bottom; height: 1; background: $surface; color: $text-muted; padding: 0 1; }
    #approval-box {
        width: 80%; max-height: 80%; border: thick $warning; background: $surface;
        padding: 1 2;
    }
    #approval-preview-scroll { max-height: 16; border: solid $primary-darken-3; }
    #approval-buttons { height: auto; }
    ApprovalScreen { align: center middle; }
    """

    BINDINGS = [
        Binding("ctrl+c", "cancel_or_quit", "Cancel/Quit", priority=True),
        Binding("f1", "show_tab('tab-chat')", "Chat"),
        Binding("f2", "show_tab('tab-diff')", "Diff"),
        Binding("f3", "show_tab('tab-evidence')", "Evidence"),
        Binding("f4", "show_tab('tab-trace')", "Trace"),
    ]

    def __init__(
        self,
        workspace: Path,
        resume_run_id: str | None = None,
        services_builder: ServicesBuilder | None = None,
        approval_mode: ApprovalMode | str | None = None,
        worktree: bool | None = None,
        fast_model: str | None = None,
        deep_model: str | None = None,
    ) -> None:
        super().__init__()
        self._workspace = workspace
        # 使用用户打开的规范路径；隔离工作树每次生成新路径，不能作为历史键。
        self._input_history_workspace = str(workspace.resolve())
        self._resume_run_id = resume_run_id
        self._services_builder = services_builder
        self._worktree = worktree
        self._fast_model = fast_model
        self._deep_model = deep_model
        if isinstance(approval_mode, ApprovalMode):
            self._approval_mode = approval_mode
        elif (
            isinstance(approval_mode, str)
            and approval_mode.lower() in ApprovalMode._value2member_map_
        ):
            self._approval_mode = ApprovalMode(approval_mode.lower())
        else:
            env_mode = os.environ.get("HAVEN_APPROVAL_MODE")
            self._approval_mode = (
                ApprovalMode(env_mode.strip().lower())
                if env_mode and env_mode.strip().lower() in ApprovalMode._value2member_map_
                else ApprovalMode.ASK
            )
        self._services: SessionServices | None = None
        self._broker = QueueApprovalBroker()
        self._sink = QueueSink()
        self._input_history_lock = asyncio.Lock()
        self._state = PresenterState()
        self._run_worker: Worker[None] | None = None
        self._rendered_timeline = 0
        self._child_run_ids: set[str] = set()
        #: 由 /fork 设置；下一次提交会从该运行 ID 分支，而不是继续当前会话。
        self._fork_run_id = ""
        #: 由 /skill use 设置；应用到之后开始的每一次运行，直到 /skill clear。
        #: 它必须在运行开始前解析，因为上下文头部在那一刻被固定（ADR 0008/0031）。
        self._selected_skills: tuple[str, ...] = ()
        self._selected_check_group: str | None = None
        self._selected_model_preset: str | None = None

    # -- 布局 ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        """构建标题、时间线、四个内容页签、输入框和状态栏。"""
        yield Static("Haven — starting…", id="header")
        yield RichLog(id="timeline", wrap=True, markup=False, highlight=False)
        with TabbedContent(id="tabs"):
            with TabPane("Chat", id="tab-chat"), VerticalScroll():
                yield Static("", id="chat")
            with TabPane("Diff", id="tab-diff"), VerticalScroll():
                yield Static("(no diff yet)", id="diff")
            with TabPane("Evidence", id="tab-evidence"), VerticalScroll():
                yield Static("(no evidence yet)", id="evidence")
            with TabPane("Trace", id="tab-trace"), VerticalScroll():
                yield Static("(no trace yet)", id="trace")
        yield PromptInput()
        yield Static("", id="status")

    async def on_mount(self) -> None:
        """挂载后启动事件消费和服务组合两个后台任务。"""
        self._consume_events()
        self._bootstrap()

    # -- 工作线程 -----------------------------------------------------------------

    @work(exclusive=False)
    async def _bootstrap(self) -> None:
        services: SessionServices | None = None
        try:
            if self._services_builder is not None:
                services = await self._services_builder(
                    workspace=self._workspace, approvals=self._broker, sinks=[self._sink]
                )
            else:
                from haven.bootstrap import build_services

                services = await build_services(
                    self._workspace,
                    mode=PermissionMode.INTERACTIVE,
                    approvals=self._broker,
                    sinks=[self._sink],
                    approval_mode=self._approval_mode,
                    worktree=self._worktree,
                    fast_model=self._fast_model,
                    deep_model=self._deep_model,
                )
        except Exception as exc:  # 在 UI 中显示启动问题
            self._log_line("system", f"startup failed: {exc}")
            if hint := error_hint(str(exc)):
                self._log_line("system", f"Next: {hint}")
            self._set_status(f"startup failed: {exc}")
            if bell_enabled():
                self.bell()
            return

        assert services is not None  # 刚刚在上面赋值
        history_warning = ""
        try:
            history = await services.store.load_input_history(self._input_history_workspace)
        except asyncio.CancelledError:
            await services.close()
            raise
        except Exception as exc:  # 历史读取失败不应阻止正常运行
            history = []
            history_warning = f"input history could not be loaded: {sanitize(str(exc), 200)}"
        self.query_one("#prompt", PromptInput).load_history(history)
        self._services = services
        self._state = PresenterState(
            workspace=str(self._workspace),
            branch=services.git_branch,
            model_name=services.model_name,
            mode="interactive",
            approval_mode=self._approval_mode.value,
        )
        if services.lease_warning:
            self._log_line("system", services.lease_warning)
        self._refresh_chrome()
        self._log_line("system", f"workspace: {self._workspace}")
        if history_warning:
            self._log_line("system", history_warning)
        self._log_line("system", "ready — describe a task and press Enter (/help for commands)")
        if self._resume_run_id is not None:
            self._start_resume(self._resume_run_id)

    @property
    def _svc(self) -> SessionServices:
        """bootstrap 完成后的服务。只有提示符可用后调用者才会运行；
        `_on_submit` 会在 `self._services is None` 时阻止调用。"""
        assert self._services is not None
        return self._services

    @work(exclusive=False)
    async def _consume_events(self) -> None:
        while True:
            envelope = await self._sink.queue.get()
            self._apply(envelope)

    @work(exclusive=True, group="run")
    async def _execute_run(
        self,
        goal: str,
        skill_names: tuple[str, ...],
        agent_mode: AgentMode = AgentMode.AGENT,
        check_group: str | None = None,
        model_preset: str | None = None,
    ) -> None:
        try:
            outcome = await self._svc.run_service.run(
                goal,
                skill_names=skill_names,
                approval_mode=self._approval_mode,
                agent_mode=agent_mode,
                require_review=getattr(self._svc, "ephemeral_worktree", None) is not None,
                check_group=check_group,
                model_preset=model_preset,
            )
            await self._reconcile_outcome(outcome)
        except ValueError as exc:
            # 在 turn loop 之前失败，因此没有任何运行需要清理；只要把原因说清楚。
            self._log_line("system", f"cannot start the run: {exc}")

    @work(exclusive=True, group="run")
    async def _execute_continue(
        self,
        previous_run_id: str,
        follow_up: str,
        skill_names: tuple[str, ...],
        check_group: str | None = None,
        model_preset: str | None = None,
    ) -> None:
        try:
            outcome = await self._svc.run_service.continue_run(
                previous_run_id,
                follow_up,
                skill_names=skill_names,
                approval_mode=self._approval_mode,
                check_group=check_group,
                model_preset=model_preset,
            )
            await self._reconcile_outcome(outcome)
        except ValueError as exc:
            self._log_line("system", f"cannot start the run: {exc}")

    @work(exclusive=True, group="run")
    async def _execute_implement(
        self, run_id: str, check_group: str | None = None, model_preset: str | None = None
    ) -> None:
        try:
            outcome = await self._svc.run_service.implement_plan(
                run_id,
                skill_names=self._selected_skills,
                approval_mode=self._approval_mode,
                check_group=check_group,
                model_preset=model_preset,
            )
            await self._reconcile_outcome(outcome)
        except ValueError as exc:
            self._log_line("system", f"cannot implement the plan: {exc}")

    @work(exclusive=True, group="run")
    async def _execute_resume(self, ctx: RunContext) -> None:
        try:
            outcome = await self._svc.run_service.resume(ctx)
            await self._reconcile_outcome(outcome)
        except ValueError as exc:
            self._state = replace(self._state, running=False)
            self._log_line("system", f"cannot resume: {sanitize(str(exc), 300)}")
            self._refresh_chrome()

    @work(exclusive=True, group="run")
    async def _execute_answer(self, run_id: str, response: UserInputResponse) -> None:
        try:
            outcome = await self._svc.run_service.answer_run(run_id, response)
            await self._reconcile_outcome(outcome)
        except ValueError as exc:
            self._log_line("system", f"cannot submit answer: {exc}; use /answer to reopen")

    async def _reconcile_outcome(self, outcome: RunOutcome) -> None:
        if (
            getattr(self._svc, "ephemeral_worktree", None) is not None
            and outcome.status is RunStatus.SUCCEEDED
        ):
            await self._review_worktree(outcome.run_id)
            return
        reconciler = getattr(self._svc, "reconcile_worktree_if_succeeded", None)
        if reconciler is not None:
            paths = await reconciler(outcome)
            if paths:
                self._log_line("system", f"reconciled {len(paths)} file(s) from worktree")

    async def _review_worktree(self, run_id: str) -> None:
        prepare = getattr(self._svc, "prepare_review", None)
        apply = getattr(self._svc, "apply_review", None)
        if (
            prepare is None
            or apply is None
            or getattr(self._svc, "ephemeral_worktree", None) is None
        ):
            self._log_line("system", "no isolated worktree to review; use /diff to inspect changes")
            return
        try:
            snapshot = await prepare(run_id)
            if snapshot.files:
                accepted = await self.push_screen_wait(
                    DiffReviewScreen(
                        snapshot.diff,
                        confirm=True,
                        binding=snapshot.binding,
                        truncated=snapshot.truncated,
                    )
                )
                if not accepted:
                    self._log_line(
                        "system", f"changes kept isolated; use /review or haven review {run_id}"
                    )
                    return
            paths = await apply(run_id, snapshot.binding)
            self._log_line("system", f"applied {len(paths)} reviewed file(s)")
        except ValueError as exc:
            self._log_line("system", f"review could not apply changes: {exc}; worktree retained")

    def _show_diff(self, selected_file: str = "") -> None:
        async def show() -> None:
            diff = await self._svc.run_service.get_diff(self._state.run_id)
            if diff:
                self.push_screen(DiffReviewScreen(diff, selected_file=selected_file))
            else:
                self._log_line("system", "no recorded changes to review")

        if self._services is not None:
            self.run_worker(self._run_query("showing the diff", show()), exclusive=False)

    def _start_resume(self, run_id: str) -> None:
        async def _do() -> None:
            if not await self._load_session(run_id):
                return
            report = await self._svc.recovery.inspect(run_id)
            if not report.can_resume or report.checkpoint is None:
                for blocker in report.blockers:
                    self._log_line("system", f"cannot resume: {blocker}")
                return
            ctx = await self._svc.recovery.build_context(report.checkpoint)
            self._state = replace(self._state, running=True)
            self._log_line("system", f"resuming run {run_id}")
            self._run_worker = self._execute_resume(ctx)

        self.run_worker(self._run_query("resuming", _do()), exclusive=False)

    # -- 事件 -> 状态 -> 控件 ----------------------------------------------------

    def _apply(self, envelope: EventEnvelope) -> None:
        if isinstance(envelope.event, ChildStarted):
            self._child_run_ids.add(envelope.event.child_run_id)
        if getattr(envelope.event, "run_id", "") in self._child_run_ids:
            return
        previous = self._state
        self._state = reduce(previous, envelope)

        for entry in self._state.timeline[self._rendered_timeline :]:
            self._write_timeline(entry)
        self._rendered_timeline = len(self._state.timeline)

        if isinstance(envelope.event, ApprovalRequested):
            self._open_approval(envelope.event)
        if isinstance(envelope.event, UserInputRequested):
            self._open_question(envelope.event)
        if hint := event_hint(envelope.event):
            self._log_line("system", f"Next: {hint}")
        if needs_attention(envelope.event) and bell_enabled():
            self.bell()

        self._refresh_panels()
        self._refresh_chrome()

    def _open_approval(self, request: ApprovalRequested) -> None:
        def _decide(approved: bool | None) -> None:
            decision = ApprovalDecision.APPROVED if approved else ApprovalDecision.REJECTED
            self._broker.resolve(request.approval_id, decision)

        self.push_screen(ApprovalScreen(request), _decide)

    def _open_question(self, event: UserInputRequested) -> None:
        def answered(response: UserInputResponse | None) -> None:
            if response is not None:
                self._run_worker = self._execute_answer(event.run_id, response)

        self.push_screen(QuestionScreen(event.request), answered)

    # -- 渲染 -------------------------------------------------------------------

    def _write_timeline(self, entry: TimelineEntry) -> None:
        self.query_one("#timeline", RichLog).write(timeline_text(entry))

    def _log_line(self, kind: str, text: str) -> None:
        """仅供 UI 使用的系统消息：渲染它，并记录到视图状态中，
        使其与事件驱动的时间线条目保持一致。"""
        from dataclasses import replace

        entry = TimelineEntry(kind, text)
        self._state = replace(self._state, timeline=(*self._state.timeline, entry))
        self._rendered_timeline = len(self._state.timeline)
        self._write_timeline(entry)

    async def _run_query(self, label: str, coro: Awaitable[object]) -> None:
        """跑一个即发即弃的只读查询，把失败变成一行日志而不是进程死亡。

        Textual 对未捕获的 worker 异常会直接退出整个应用（`App._handle_exception`
        的注释原文："Always results in the app exiting"）。而这些查询都是只读且可失败的：
        磁盘满了、SQLite 被锁、工作区一个瞬时的 OSError。为一次 `/diff` 杀掉会话、并
        中止在飞的运行是不可接受的。同一个文件里 `_show_run_stats`、
        `_show_process_output`、`_handoff_command` 早就各自这么做了，这里统一成一处，
        把其余几处补齐。
        """
        try:
            await coro
        except Exception as exc:
            # 消息本身要转义：异常文本里可能带不可信的路径或模型输出。
            self._log_line("system", f"{label} failed: {sanitize(str(exc), 300)}")

    def _refresh_panels(self) -> None:
        panels = panel_text(self._state)
        self.query_one("#chat", Static).update(panels.chat)
        self.query_one("#diff", Static).update(panels.diff)
        self.query_one("#evidence", Static).update(panels.evidence)
        self.query_one("#trace", Static).update(panels.trace)

    def _refresh_chrome(self) -> None:
        self.query_one("#header", Static).update(self._state.header_line())
        self._set_status(self._state.status_line())

    def _set_status(self, text: str) -> None:
        self.query_one("#status", Static).update(text)

    # -- 输入 -------------------------------------------------------------------

    @on(Input.Submitted, "#prompt")
    async def _on_submit(self, message: Input.Submitted) -> None:
        text = message.value.strip()
        if not text:
            return
        prompt = self.query_one("#prompt", PromptInput)
        prompt.value = (
            prompt.navigation_draft
            if text.split()[0].lower() in {"/history", "/memory", "/remember", "/verify", "/model"}
            else ""
        )
        prompt.reset_navigation()
        if text.startswith("/"):
            self._handle_command(text)
            return
        if self._services is None:
            self._log_line("system", "still starting up, try again in a moment")
            return
        if self._svc.run_service.is_reloading_guidance:
            prompt.value = text
            self._log_line("system", "project guidance is reloading; submit again when it finishes")
            return
        if self._state.pending_input is not None:
            self._log_line("system", "answer the pending question with /answer before continuing")
            return
        if self._state.review_pending:
            self._log_line("system", "review the isolated changes with /review before continuing")
            return
        model_preset = self._selected_model_preset
        async with self._input_history_lock:
            prompt.remember(text)
            try:
                await self._svc.store.record_input(self._input_history_workspace, text)
            except Exception as exc:  # 保留内存历史，任务仍可正常执行
                self._log_line(
                    "system", f"input history could not be saved: {sanitize(str(exc), 200)}"
                )
        text = self._expand_mentions(text)
        if self._state.running:
            # Steering：将输入排入活跃运行，而不是拒绝它。输入会在下一轮边界
            # 投递，因此不会打断进行中的操作（ROADMAP2 phase 3）。
            self._queue_steering(text)
            return
        # 明确指定的 /fork 目标会从该运行分支出新会话，而不是继续当前会话
        # （ROADMAP3 第 4 阶段）。
        fork_target = self._fork_run_id
        self._fork_run_id = ""
        if fork_target:
            self._run_worker = self._execute_continue(
                fork_target, text, self._selected_skills, self._selected_check_group, model_preset
            )
            return
        # 已完成运行后的后续提交会继续同一段对话，因此模型保留上一轮上下文，
        # 而不是从空白开始（Phase 2）。会话中的第一次提交会启动新运行。
        if self._state.run_id:
            self._run_worker = self._execute_continue(
                self._state.run_id,
                text,
                self._selected_skills,
                self._selected_check_group,
                model_preset,
            )
        else:
            self._run_worker = self._execute_run(
                text,
                self._selected_skills,
                check_group=self._selected_check_group,
                model_preset=model_preset,
            )

    @work(exclusive=False, group="input-history")
    async def _manage_input_history(self, clear: bool) -> None:
        if self._services is None:
            self._log_line("system", "still starting up, try again in a moment")
            return
        prompt = self.query_one("#prompt", PromptInput)
        if not clear:
            entries = prompt.entries
            self._log_line(
                "system",
                "task input history (newest first):\n"
                + "\n".join(
                    f"  {index}. {sanitize(value, 4000)}"
                    for index, value in enumerate(reversed(entries), start=1)
                )
                if entries
                else "no task input history for this workspace",
            )
            return
        try:
            async with self._input_history_lock:
                await self._svc.store.clear_input_history(self._input_history_workspace)
                prompt.clear_history()
        except Exception as exc:  # 删除失败时保留导航和草稿
            self._log_line(
                "system", f"input history could not be cleared: {sanitize(str(exc), 200)}"
            )
            return
        self._log_line("system", "cleared task input history for this workspace")

    def _can_update_project_memory(self) -> bool:
        if self._services is None:
            self._log_line("system", "still starting up, try again in a moment")
            return False
        if self._state.running or (self._run_worker is not None and self._run_worker.is_running):
            self._log_line(
                "system", "wait for the current task to finish before changing project memory"
            )
            return False
        if self._state.pending_input is not None or self._state.review_pending:
            self._log_line(
                "system", "finish the pending question or review before changing project memory"
            )
            return False
        return True

    @work(exclusive=False, group="project-guidance")
    async def _reload_project_guidance(self) -> None:
        if not self._can_update_project_memory():
            return
        try:
            snapshot = await self._svc.run_service.reload_guidance()
        except Exception as exc:  # 刷新失败保留已有快照
            self._log_line(
                "system", f"project guidance could not be reloaded: {sanitize(str(exc), 200)}"
            )
            return
        self._log_line(
            "system",
            f"reloaded project guidance ({len(snapshot.text)} characters) for the next run; "
            "resuming a saved task keeps its saved guidance",
        )

    @work(exclusive=False, group="project-guidance")
    async def _manage_project_notes(self, action: NoteAction, value: str = "") -> None:
        if not self._can_update_project_memory():
            return
        try:
            snapshot = await self._svc.run_service.update_project_notes(action, value)
        except ValueError as exc:
            self._log_line("system", f"project note update failed: {sanitize(str(exc), 200)}")
            return
        except Exception as exc:  # 存储可能已写入，提示先核对而非盲目重试
            self._log_line(
                "system",
                f"project note update failed: {sanitize(str(exc), 200)}; "
                "use /memory reload to reconcile saved notes before retrying",
            )
            return
        verb = {"remember": "saved", "forget": "deleted", "clear": "cleared"}[action]
        self._log_line(
            "system",
            f"{verb} project notes; {len(snapshot.notes)} now loaded for the next run. "
            "Use /memory to view IDs. Saved tasks keep their own snapshots.",
        )

    def _expand_mentions(self, text: str) -> str:
        """将 @path 提及展开为列出文件的说明，让目标明确指向这些文件。
        代理仍会通过 repo.read 读取文件（来源和 preimage 绑定仍保留在工具通道中）；
        提及内容只是替它省去一次搜索。"""
        return expand_mentions(text, self._workspace)

    def _queue_steering(self, text: str) -> None:
        async def _do() -> None:
            accepted = await self._svc.run_service.steer(text)
            if accepted:
                self._log_line("you (queued)", text)
                self._log_line("system", "queued; it reaches the agent at the next turn")
            else:
                self._log_line("system", "no active run to steer; send again to start one")

        self.run_worker(self._run_query("queueing steering input", _do()), exclusive=False)

    def _handle_command(self, command: str) -> None:
        budget = self._services.config.budget if self._services is not None else None
        skills = self._services.skills if self._services is not None else None
        action = route_command(command, self._state, budget, skills)
        if action.kind == "log":
            self._log_line("system", action.value)
        elif action.kind == "history":
            self._manage_input_history(action.value == "clear")
        elif action.kind == "verification":
            self._verification_command(action.value)
        elif action.kind == "model":
            self._model_command(action.value)
        elif action.kind == "stats":
            if self._services is None or not self._state.run_id:
                self._log_line("system", "no run selected; use /sessions to open one")
            else:
                self._show_run_stats(self._state.run_id)
        elif action.kind == "output":
            self._show_process_output(action.value)
        elif action.kind == "handoff":
            self._handoff_command(action.value)
        elif action.kind == "remember":
            self._manage_project_notes("remember", action.value)
        elif action.kind == "memory":
            if self._services is None:
                self._log_line("system", "still starting up, try again in a moment")
            elif action.value == "reload":
                self._reload_project_guidance()
            elif action.value == "clear":
                self._manage_project_notes("clear")
            elif action.value.startswith("forget "):
                self._manage_project_notes("forget", action.value.partition(" ")[2])
            else:
                self.push_screen(ProjectMemoryScreen(self._svc.run_service.guidance_snapshot))
        elif action.kind == "answer":
            if self._state.pending_input is not None:
                self._open_question(
                    UserInputRequested(run_id=self._state.run_id, request=self._state.pending_input)
                )
            else:
                self._log_line("system", "no pending question")
        elif action.kind in {"plan", "implement"}:
            if self._services is None:
                self._log_line("system", "still starting up, try again in a moment")
            elif self._state.running:
                self._log_line("system", "a run is active; wait for it to finish first")
            elif self._state.pending_input is not None or self._state.review_pending:
                self._log_line(
                    "system", "finish the pending question or review before starting work"
                )
            elif action.kind == "plan":
                self._fork_run_id = ""
                self._run_worker = self._execute_run(
                    self._expand_mentions(action.value),
                    self._selected_skills,
                    AgentMode.PLAN,
                    self._selected_check_group,
                    self._selected_model_preset,
                )
            elif not action.value:
                self._log_line("system", "no plan yet; use /plan GOAL first")
            else:
                self._run_worker = self._execute_implement(
                    action.value, self._selected_check_group, self._selected_model_preset
                )
        elif action.kind == "sessions":
            self._list_sessions(action.value)
        elif action.kind == "new":
            if self._can_switch_session():
                self._reset_session()
                self._log_line("system", "new conversation; enter a task to begin")
        elif action.kind == "skill":
            self._skill_command(action.value)
        elif action.kind == "fork":
            if self._can_switch_session():
                self.run_worker(
                    self._run_query(
                        "forking", self._select_session(SessionSelection(action.value, "fork"))
                    )
                )
        elif action.kind == "diff":
            self.query_one("#tabs", TabbedContent).active = "tab-diff"
            self._show_diff(action.value)
        elif action.kind == "review":
            if self._services is not None and not self._state.running and self._state.run_id:
                self.run_worker(
                    self._review_worktree(self._state.run_id), exclusive=True, group="review"
                )
            else:
                self._log_line("system", "finish a run before reviewing its worktree")
        elif action.kind == "mode":
            new_mode = ApprovalMode(action.value)
            self._approval_mode = new_mode
            if self._services is not None:
                self._services.run_service.set_approval_mode(new_mode)
            self._state = replace(self._state, approval_mode=new_mode.value)
            self.query_one("#header", Static).update(self._state.header_line())
            self._log_line("system", f"approval mode switched to: {new_mode.value}")
        elif action.kind == "rewind":
            self._rewind_command(action.value)
        elif action.kind == "export":
            self._export_run()
        elif action.kind == "quit":
            self.exit()

    def _skill_command(self, value: str) -> None:
        """记录、清除或列出技能选择。

        运行中途选择会被接受，但必须说明它对当前这次运行无效：上下文头部在运行
        开始时就被固定了（ADR 0008），中途改动会移动可缓存前缀。静默接受等于
        悄悄丢掉用户的输入，那是这个界面唯一不能做的事。
        """
        if value == "clear":
            self._selected_skills = ()
            self._state = replace(self._state, selected_skills=())
            with contextlib.suppress(Exception):
                self.query_one("#header", Static).update(self._state.header_line())
            self._log_line("system", "skill selection cleared")
            return
        names = tuple(value.split())
        self._selected_skills = names
        self._state = replace(self._state, selected_skills=names)
        with contextlib.suppress(Exception):
            self.query_one("#header", Static).update(self._state.header_line())
        listed = ", ".join(names)
        if self._state.running:
            self._log_line(
                "system",
                f"skill selection saved for your next run: {listed}\n"
                "(the active run's context was fixed when it started — ADR 0008 — "
                "so it will NOT see it)",
            )
        else:
            self._log_line("system", f"skill selection saved for your next run: {listed}")

    def _rewind_command(self, command: str) -> None:
        """用户级撤销本会话最近一次已完成的运行（ADR 0020）。

        特意设计为两步：`/rewind` 只说明将要发生什么，`/rewind yes` 才执行撤销。
        操作本身采用失败即拒绝策略——RecoveryService.rewind 会拒绝处理运行结束后
        发生过变化的任何文件——因此确认针对的是用户意图，而不是安全性。
        """
        if self._services is None:
            self._log_line("system", "still starting up, try again in a moment")
            return
        if self._state.running:
            self._log_line("system", "a run is active; wait for it to finish before rewinding")
            return
        run_id = self._state.run_id
        if not run_id:
            self._log_line("system", "no run in this session to rewind")
            return
        confirmed = command.split()[1:] == ["yes"]
        if not confirmed:
            self._log_line(
                "system",
                f"rewind restores every file run {run_id} changed to its pre-run "
                "content; anything modified since blocks instead of being "
                "overwritten. Type /rewind yes to proceed.",
            )
            return

        async def _do() -> None:
            self._log_line("system", await rewind_text(self._svc.recovery, run_id))

        self.run_worker(self._run_query("rewinding", _do()), exclusive=False)

    def _verification_command(self, value: str) -> None:
        if self._services is None:
            self._log_line("system", "still starting up, try again in a moment")
            return
        groups = self._svc.run_service.verification_groups
        if not value:
            configured = (
                "\n".join(
                    f"  {sanitize(name, 100)}: {sanitize(', '.join(members), 200)}"
                    for name, members in sorted(groups.items())
                )
                or "  (no configured groups; add [verification_groups] to .haven.toml)"
            )
            choice = (
                "inherit for follow-ups; no group for a new conversation"
                if self._selected_check_group is None
                else self._selected_check_group or "none"
            )
            self._log_line(
                "system",
                f"next checks: {sanitize(choice, 200)}\nConfigured groups:\n{configured}\n"
                + verification_text(self._state),
            )
            return
        if not self._can_switch_session("changing required checks"):
            return
        if value == "clear":
            self._selected_check_group = ""
        else:
            name = value.partition(" ")[2]
            if name not in groups:
                self._log_line("system", "unknown verification group; use /verify to list groups")
                return
            self._selected_check_group = name
        self._state = replace(self._state, next_check_group=self._selected_check_group)
        self._log_line(
            "system",
            "required checks for your next run: " + sanitize(self._selected_check_group, 100)
            if self._selected_check_group
            else "required checks explicitly cleared for your next run",
        )
        self._refresh_chrome()

    def _model_command(self, value: str) -> None:
        if self._services is None:
            self._log_line("system", "still starting up, try again in a moment")
            return
        config = self._svc.config
        if not value:
            name = self._selected_model_preset
            spec = (
                config.model_presets[name]
                if name
                else config.default_model_spec(
                    fast_model=self._fast_model, deep_model=self._deep_model
                )
            )
            lines = [f"Model for new runs: {name or 'default'}", *model_spec_lines(spec, config)]
            lines.append("Presets [user]: " + (", ".join(sorted(config.model_presets)) or "none"))
            if self._state.last_model_call:
                lines.append("Last call: " + model_call_text(self._state.last_model_call))
            self._log_line("system", sanitize("\n".join(lines), 2000))
            return
        name = value.partition(" ")[2] if value != "clear" else None
        if name is not None and name not in config.model_presets:
            self._log_line("system", "unknown model preset; use /model to list presets")
            return
        self._selected_model_preset = name
        self._state = replace(self._state, next_model_preset=name)
        self._log_line("system", f"model for new runs: {sanitize(name or 'default', 48)}")
        self._refresh_chrome()

    @work(exclusive=True, group="run-stats")
    async def _show_run_stats(self, run_id: str) -> None:
        try:
            text = await run_stats_text(self._svc.store, run_id)
        except Exception as exc:  # 保持日志查询错误可见，运行可继续
            text = f"could not summarize run: {exc}"
        if self._state.run_id == run_id:
            self._log_line("system", sanitize(text, 3000))

    def _can_switch_session(self, action: str = "switching sessions") -> bool:
        if self._services is None:
            self._log_line("system", "still starting up, try again in a moment")
            return False
        if self._state.running or (self._run_worker is not None and self._run_worker.is_running):
            self._log_line("system", f"finish the current task before {action}")
            return False
        if self._state.pending_input is not None or self._state.review_pending:
            self._log_line("system", f"finish the pending question or review before {action}")
            return False
        return True

    def _reset_session(self) -> None:
        previous = self._state
        self._state = PresenterState(
            workspace=previous.workspace,
            branch=previous.branch,
            model_name=self._svc.model_name,
            mode=previous.mode,
            approval_mode=self._approval_mode.value,
            selected_skills=self._selected_skills,
            next_check_group=self._selected_check_group,
            next_model_preset=self._selected_model_preset,
        )
        self._fork_run_id = ""
        self._child_run_ids.clear()
        self._rendered_timeline = 0
        self.query_one("#timeline", RichLog).clear()
        self._refresh_panels()
        self._refresh_chrome()

    async def _load_session(self, run_id: str) -> bool:
        checkpoint = await self._svc.store.load_checkpoint(run_id)
        if checkpoint is None or checkpoint.parent_run_id:
            self._log_line("system", "session not found or belongs to a delegated agent")
            return False
        if checkpoint.workspace_digest != self._svc.run_service.workspace_digest:
            self._log_line("system", "session belongs to a different workspace")
            return False
        self._reset_session()
        # 只归约历史事件；旧审批和问题不会被当作实时事件再次执行。
        for envelope in await self._svc.store.load_events(run_id):
            self._state = reduce(self._state, envelope)
        # 历史日志可能领先于最后的检查点。当前必需检查必须与恢复使用的
        # 账本一致；较晚的原始记录仍保留在时间线中。
        ledger = checkpoint.evidence.to_domain()
        review = checkpoint.review
        self._state = replace(
            self._state,
            run_id=run_id,
            running=False,
            pending_input=checkpoint.pending_input,
            verification=checkpoint.verification,
            verification_checks=ledger.checks,
            last_edit_seq=ledger.last_edit_seq,
            review_pending=bool(
                checkpoint.status == RunStatus.SUCCEEDED.value
                and checkpoint.evidence.edits
                and (checkpoint.require_review or review)
                and (review is None or not (review.applied or review.discarded))
            ),
        )
        for entry in self._state.timeline:
            self._write_timeline(entry)
        self._rendered_timeline = len(self._state.timeline)
        self._refresh_panels()
        self._refresh_chrome()
        return True

    async def _select_session(self, selection: SessionSelection) -> None:
        if not self._can_switch_session():
            return
        if selection.action == "resume":
            self._start_resume(selection.run_id)
        elif await self._load_session(selection.run_id):
            if selection.action == "fork":
                if self._state.pending_input is not None or self._state.review_pending:
                    self._log_line(
                        "system", "resolve this session's question or review before forking"
                    )
                    return
                self._fork_run_id = selection.run_id
                self._log_line("system", f"next message will fork from {selection.run_id}")
            else:
                self._log_line("system", f"opened session {selection.run_id}")

    def _list_sessions(self, query: str = "") -> None:
        if not self._can_switch_session():
            return

        async def _do() -> None:
            self._log_line("system", await list_sessions_text(self._svc.store))

            def selected(selection: SessionSelection | None) -> None:
                if selection is not None:
                    self.run_worker(
                        self._run_query("opening the session", self._select_session(selection))
                    )

            self.push_screen(
                SessionPickerScreen(self._svc.store, self._svc.run_service.workspace_digest, query),
                selected,
            )

        self.run_worker(self._run_query("listing sessions", _do()), exclusive=False)

    def _handoff_command(self, value: str) -> None:
        if self._services is None or not self._state.run_id:
            self._log_line("system", "no task checkpoint for a handoff yet")
            return
        run_id = self._state.run_id
        editing = value in {"prepare", "edit", "clear"}
        if editing and self._run_worker is not None and not self._run_worker.is_finished:
            self._log_line(
                "system", "wait for the task to pause or finish before editing its handoff"
            )
            return

        async def show() -> None:
            from haven.application.handoff import (
                draft_from_plan,
                render_handoff_history,
                save_handoff,
                view_handoff,
            )
            from haven.ports.session import HANDOFF_EDITABLE_STATUSES, ArtifactError

            try:
                view = await view_handoff(self._svc.store, run_id)
                if value in {"prepare", "clear"}:
                    await save_handoff(
                        self._svc.store,
                        run_id,
                        draft_from_plan(view) if value == "prepare" else None,
                        view.expected_seq,
                        source="plan_draft" if value == "prepare" else "manual",
                    )
                    view = await view_handoff(self._svc.store, run_id)
                record = await self._svc.store.get_run(run_id)
                editable = bool(record and record.status in HANDOFF_EDITABLE_STATUSES)
                history_text = (
                    await render_handoff_history(self._svc.store, run_id)
                    if value == "history"
                    else None
                )
            except (ValueError, OSError, ArtifactError) as exc:
                self._log_line("system", f"handoff unavailable: {sanitize(str(exc), 500)}")
                return
            if run_id != self._state.run_id:
                return

            async def save_notes(notes: HandoffNotes) -> str:
                try:
                    await save_handoff(self._svc.store, run_id, notes, view.expected_seq)
                except (ValueError, OSError, ArtifactError) as exc:
                    return str(exc)
                return ""

            def chosen(action: HandoffAction | None) -> None:
                if action is None:
                    return
                if action.kind == "save":
                    self._log_line(
                        "system", "handoff notes saved; adopted when this task next runs"
                    )
                else:
                    self._handoff_command(action.kind)

            self.push_screen(
                HandoffScreen(
                    view,
                    editing=value == "edit",
                    editable=editable and value != "history",
                    history_text=history_text,
                    save_notes=save_notes,
                ),
                chosen,
            )

        self.run_worker(show(), exclusive=False)

    def _show_process_output(self, call_id: str) -> None:
        if not call_id:
            self.query_one("#tabs", TabbedContent).active = "tab-evidence"
            return
        if self._services is None or not self._state.run_id:
            self._log_line("system", "no run output to view yet")
            return
        run_id = self._state.run_id

        async def show() -> None:
            from haven.application.process_outputs import read_process_output
            from haven.ports.session import ArtifactError

            try:
                text = await read_process_output(self._svc.store, run_id, call_id)
            except (ValueError, OSError, ArtifactError) as exc:
                self._log_line("system", f"output unavailable: {sanitize(str(exc), 500)}")
                return
            if run_id == self._state.run_id:
                self.push_screen(ProcessOutputScreen(call_id, text))

        self.run_worker(show(), exclusive=False)

    def _export_run(self) -> None:
        if not self._state.run_id:
            self._log_line("system", "no run to export yet")
            return

        async def _do() -> None:
            message = await export_run_text(self._svc.store, self._state.run_id, Path.cwd())
            self._log_line("system", message)

        self.run_worker(self._run_query("exporting the run", _do()), exclusive=False)

    # -- 动作 -------------------------------------------------------------------

    def action_cancel_or_quit(self) -> None:
        """运行中取消当前 worker，否则退出应用。"""
        if self._state.running and self._run_worker is not None:
            self._log_line("system", "cancelling the current run…")
            self._run_worker.cancel()
            return
        self.exit()

    def action_show_tab(self, tab_id: str) -> None:
        """切换到指定的内容页签。"""
        self.query_one("#tabs", TabbedContent).active = tab_id

    async def on_unmount(self) -> None:
        """卸载时关闭已创建的组合服务和外部资源。"""
        # 取消之后仍需写入检查点与取消事件。先等待每个 worker 收尾，
        # 不能让某个 WorkerCancelled 提前终止等待，再把其他任务的存储关掉。
        workers = tuple(self.workers)
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*(worker.wait() for worker in workers), return_exceptions=True)
        if self._services is not None:
            await self._services.close()
