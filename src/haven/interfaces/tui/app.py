"""基于 Textual 的 Haven TUI。

TUI 仅负责界面：将用户意图转换为服务调用，并渲染共享应用事件流中的
PresenterState。它不会执行工具、决定策略，也不会直接与提供商通信。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
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
from haven.application.run_service import RunService
from haven.application.state import RunContext
from haven.config import ResolvedConfig
from haven.contracts.events import (
    TRANSIENT_KINDS,
    ApprovalRequested,
    EventEnvelope,
)
from haven.domain.enums import ApprovalDecision, PermissionMode
from haven.interfaces.tui.commands import HELP_TEXT, route_command
from haven.interfaces.tui.input_helpers import expand_mentions
from haven.interfaces.tui.presenter import PresenterState, TimelineEntry, reduce, sanitize
from haven.interfaces.tui.rendering import panel_text, timeline_text
from haven.interfaces.tui.session_actions import export_run_text, list_sessions_text, rewind_text
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
    ) -> None:
        super().__init__()
        self._workspace = workspace
        self._resume_run_id = resume_run_id
        self._services_builder = services_builder
        self._services: SessionServices | None = None
        self._broker = QueueApprovalBroker()
        self._sink = QueueSink()
        self._state = PresenterState()
        self._run_worker: Worker[None] | None = None
        self._rendered_timeline = 0
        #: 由 /fork 设置；下一次提交会从该运行 ID 分支，而不是继续当前会话。
        self._fork_run_id = ""

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
        yield Input(placeholder="Describe a coding task… (/help for commands)", id="prompt")
        yield Static("", id="status")

    async def on_mount(self) -> None:
        """挂载后启动事件消费和服务组合两个后台任务。"""
        self._consume_events()
        self._bootstrap()

    # -- 工作线程 -----------------------------------------------------------------

    @work(exclusive=False)
    async def _bootstrap(self) -> None:
        try:
            if self._services_builder is not None:
                self._services = await self._services_builder(
                    workspace=self._workspace, approvals=self._broker, sinks=[self._sink]
                )
            else:
                from haven.bootstrap import build_services

                self._services = await build_services(
                    self._workspace,
                    mode=PermissionMode.INTERACTIVE,
                    approvals=self._broker,
                    sinks=[self._sink],
                )
        except Exception as exc:  # noqa: BLE001 - 在 UI 中显示启动问题
            self._log_line("system", f"startup failed: {exc}")
            self._set_status(f"startup failed: {exc}")
            return

        services = self._services
        assert services is not None  # 刚刚在上面赋值
        self._state = PresenterState(
            workspace=str(self._workspace),
            branch=services.git_branch,
            model_name=services.model_name,
            mode="interactive",
        )
        if services.lease_warning:
            self._log_line("system", services.lease_warning)
        self._refresh_chrome()
        self._log_line("system", f"workspace: {self._workspace}")
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
    async def _execute_run(self, goal: str) -> None:
        await self._svc.run_service.run(goal)

    @work(exclusive=True, group="run")
    async def _execute_continue(self, previous_run_id: str, follow_up: str) -> None:
        await self._svc.run_service.continue_run(previous_run_id, follow_up)

    @work(exclusive=True, group="run")
    async def _execute_resume(self, ctx: RunContext) -> None:
        await self._svc.run_service.resume(ctx)

    def _start_resume(self, run_id: str) -> None:
        async def _do() -> None:
            report = await self._svc.recovery.inspect(run_id)
            if not report.can_resume or report.checkpoint is None:
                for blocker in report.blockers:
                    self._log_line("system", f"cannot resume: {blocker}")
                return
            ctx = await self._svc.recovery.build_context(report.checkpoint)
            self._log_line("system", f"resuming run {run_id}")
            self._run_worker = self._execute_resume(ctx)

        self.run_worker(_do(), exclusive=False)

    # -- 事件 -> 状态 -> 控件 ----------------------------------------------------

    def _apply(self, envelope: EventEnvelope) -> None:
        previous = self._state
        self._state = reduce(previous, envelope)

        for entry in self._state.timeline[self._rendered_timeline :]:
            self._write_timeline(entry)
        self._rendered_timeline = len(self._state.timeline)

        if isinstance(envelope.event, ApprovalRequested):
            self._open_approval(envelope.event)

        self._refresh_panels()
        self._refresh_chrome()

    def _open_approval(self, request: ApprovalRequested) -> None:
        def _decide(approved: bool | None) -> None:
            decision = ApprovalDecision.APPROVED if approved else ApprovalDecision.REJECTED
            self._broker.resolve(request.approval_id, decision)

        self.push_screen(ApprovalScreen(request), _decide)

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
    def _on_submit(self, message: Input.Submitted) -> None:
        text = message.value.strip()
        if not text:
            return
        self.query_one("#prompt", Input).value = ""
        if text.startswith("/"):
            self._handle_command(text)
            return
        if self._services is None:
            self._log_line("system", "still starting up, try again in a moment")
            return
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
            self._run_worker = self._execute_continue(fork_target, text)
            return
        # 已完成运行后的后续提交会继续同一段对话，因此模型保留上一轮上下文，
        # 而不是从空白开始（Phase 2）。会话中的第一次提交会启动新运行。
        if self._state.run_id:
            self._run_worker = self._execute_continue(self._state.run_id, text)
        else:
            self._run_worker = self._execute_run(text)

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

        self.run_worker(_do(), exclusive=False)

    def _handle_command(self, command: str) -> None:
        budget = self._services.config.budget if self._services is not None else None
        action = route_command(command, self._state, budget)
        if action.kind == "log":
            self._log_line("system", action.value)
        elif action.kind == "sessions":
            self._list_sessions()
        elif action.kind == "fork":
            self._fork_run_id = action.value
            self._log_line(
                "system",
                f"next message will fork from {self._fork_run_id}; type it and press Enter",
            )
        elif action.kind == "diff":
            self.query_one("#tabs", TabbedContent).active = "tab-diff"
        elif action.kind == "rewind":
            self._rewind_command(action.value)
        elif action.kind == "export":
            self._export_run()
        elif action.kind == "quit":
            self.exit()

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

        self.run_worker(_do(), exclusive=False)

    def _list_sessions(self) -> None:
        async def _do() -> None:
            self._log_line("system", await list_sessions_text(self._svc.store))

        self.run_worker(_do(), exclusive=False)

    def _export_run(self) -> None:
        if not self._state.run_id:
            self._log_line("system", "no run to export yet")
            return

        async def _do() -> None:
            message = await export_run_text(self._svc.store, self._state.run_id, Path.cwd())
            self._log_line("system", message)

        self.run_worker(_do(), exclusive=False)

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
        if self._services is not None:
            await self._services.close()
