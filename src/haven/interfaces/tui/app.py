"""Haven Textual TUI.

The TUI is interface-only: it turns user intent into service calls and renders
PresenterState from the shared application event stream. It never executes
tools, never decides policy, and never talks to the provider.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, RichLog, Static, TabbedContent, TabPane
from textual.worker import Worker

from haven.application.approvals import QueueApprovalBroker
from haven.contracts.events import (
    TRANSIENT_KINDS,
    ApprovalRequested,
    EventEnvelope,
)
from haven.domain.enums import ApprovalDecision, PermissionMode
from haven.interfaces.tui.presenter import PresenterState, TimelineEntry, reduce, sanitize

ServicesBuilder = Callable[..., Awaitable[Any]]

HELP_TEXT = """\
Commands:
  /help      show this help
  /budget    show remaining budget
  /context   show what the model saw last turn
  /sessions  list recent runs you can continue or fork
  /fork ID   start a new turn branched from run ID (fork the session)
  /diff      switch to the Diff tab
  /export    write a markdown report of the current run
  /quit      exit Haven
Input:
  @path      mention a file — the agent is pointed at it explicitly (it
             still reads the file itself through repo.read)
Keys:
  Enter      submit a task
  a / r      approve / reject (in the approval dialog)
  F1..F4     switch tabs (Chat, Diff, Evidence, Trace)
  Ctrl+C     cancel the running task; press again to quit\
"""


class QueueSink:
    """Bounded bridge from the runtime to the UI. Transient deltas are dropped
    under pressure; authoritative events apply backpressure instead."""

    def __init__(self, maxsize: int = 256) -> None:
        self.queue: asyncio.Queue[EventEnvelope] = asyncio.Queue(maxsize=maxsize)

    async def emit(self, envelope: EventEnvelope) -> None:
        if envelope.event.kind in TRANSIENT_KINDS and self.queue.full():
            return
        await self.queue.put(envelope)


class ApprovalScreen(ModalScreen[bool]):
    """Digest-bound approval dialog: shows exactly what will run, once."""

    BINDINGS = [
        Binding("a", "approve", "Approve"),
        Binding("r", "reject", "Reject"),
        Binding("escape", "reject", "Reject"),
    ]

    def __init__(self, request: ApprovalRequested) -> None:
        super().__init__()
        self._request = request

    def compose(self) -> ComposeResult:
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
        self.dismiss(True)

    def action_reject(self) -> None:
        self.dismiss(False)


class HavenApp(App[None]):
    """Main Haven TUI application.

    Data flows one way: user input -> service call -> application events ->
    presenter.reduce -> PresenterState -> widgets. The app never mutates run
    state directly; it only renders what the event stream says happened.

    Input handling (`_on_submit`): `/commands` are dispatched locally; plain
    text starts a run, continues the last one, forks (after `/fork RUN_ID`),
    or - while a run is active - is queued as steering for the next turn
    boundary. `@path` mentions are expanded into an explicit note for the
    agent. Approvals arrive as `approval.requested` events and are answered
    through a modal (`ApprovalScreen`) wired to QueueApprovalBroker, so the
    human decision travels the same channel an auto-approver would use.
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
        self._services: Any = None
        self._broker = QueueApprovalBroker()
        self._sink = QueueSink()
        self._state = PresenterState()
        self._run_worker: Worker[None] | None = None
        self._rendered_timeline = 0
        #: Set by /fork; the next submit branches from this run id instead of
        #: continuing the current session.
        self._fork_run_id = ""

    # -- layout ---------------------------------------------------------------

    def compose(self) -> ComposeResult:
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
        self._consume_events()
        self._bootstrap()

    # -- workers ---------------------------------------------------------------

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
        except Exception as exc:  # noqa: BLE001 - surface startup problems in the UI
            self._log_line("system", f"startup failed: {exc}")
            self._set_status(f"startup failed: {exc}")
            return

        self._state = PresenterState(
            workspace=str(self._workspace),
            branch=getattr(self._services, "git_branch", ""),
            model_name=getattr(self._services, "model_name", ""),
            mode="interactive",
        )
        lease_warning = getattr(self._services, "lease_warning", "")
        if lease_warning:
            self._log_line("system", lease_warning)
        self._refresh_chrome()
        self._log_line("system", f"workspace: {self._workspace}")
        self._log_line("system", "ready — describe a task and press Enter (/help for commands)")
        if self._resume_run_id is not None:
            self._start_resume(self._resume_run_id)

    @work(exclusive=False)
    async def _consume_events(self) -> None:
        while True:
            envelope = await self._sink.queue.get()
            self._apply(envelope)

    @work(exclusive=True, group="run")
    async def _execute_run(self, goal: str) -> None:
        await self._services.run_service.run(goal)

    @work(exclusive=True, group="run")
    async def _execute_continue(self, previous_run_id: str, follow_up: str) -> None:
        await self._services.run_service.continue_run(previous_run_id, follow_up)

    @work(exclusive=True, group="run")
    async def _execute_resume(self, ctx: Any) -> None:
        await self._services.run_service.resume(ctx)

    def _start_resume(self, run_id: str) -> None:
        async def _do() -> None:
            report = await self._services.recovery.inspect(run_id)
            if not report.can_resume or report.checkpoint is None:
                for blocker in report.blockers:
                    self._log_line("system", f"cannot resume: {blocker}")
                return
            ctx = await self._services.recovery.build_context(report.checkpoint)
            self._log_line("system", f"resuming run {run_id}")
            self._run_worker = self._execute_resume(ctx)

        self.run_worker(_do(), exclusive=False)

    # -- events -> state -> widgets ------------------------------------------------

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

    # -- rendering ---------------------------------------------------------------

    _ICONS = {
        "user": ">",
        "agent": "●",
        "tool": "⚙",
        "policy": "✋",
        "approval": "?",
        "plan": "☰",
        "notice": "!",
        "system": "◆",
    }

    def _write_timeline(self, entry: TimelineEntry) -> None:
        icon = self._ICONS.get(entry.kind, "·")
        self.query_one("#timeline", RichLog).write(f"{icon} {entry.text}")

    def _log_line(self, kind: str, text: str) -> None:
        """UI-local system message: render it and record it in view state so it
        stays consistent with event-driven timeline entries."""
        from dataclasses import replace

        entry = TimelineEntry(kind, text)
        self._state = replace(self._state, timeline=(*self._state.timeline, entry))
        self._rendered_timeline = len(self._state.timeline)
        self._write_timeline(entry)

    def _refresh_panels(self) -> None:
        state = self._state
        chat = state.chat_text
        if state.reasoning_text:
            chat += f"\n[dim]thinking… {state.reasoning_text[-800:]}[/dim]"
        if state.streaming_text:
            chat += f"\n● {state.streaming_text}▌"
        if state.plan_lines:
            plan = "\n".join(state.plan_lines)
            chat = f"[b]Plan[/b]\n{plan}\n\n{chat}"
        self.query_one("#chat", Static).update(chat or "(no conversation yet)")
        self.query_one("#diff", Static).update(state.diff_text or "(no diff yet)")
        self.query_one("#evidence", Static).update(
            "\n".join(state.evidence_rows) or "(no evidence yet)"
        )
        self.query_one("#trace", Static).update("\n".join(state.trace_rows) or "(no trace yet)")

    def _refresh_chrome(self) -> None:
        self.query_one("#header", Static).update(self._state.header_line())
        self._set_status(self._state.status_line())

    def _set_status(self, text: str) -> None:
        self.query_one("#status", Static).update(text)

    # -- input ---------------------------------------------------------------------

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
            # Steering: queue the input for the active run instead of refusing
            # it. Delivery happens at the next turn boundary, so nothing
            # in-flight is interrupted (ROADMAP2 phase 3).
            self._queue_steering(text)
            return
        # An explicit /fork target branches a new session from that run instead
        # of continuing the current one (ROADMAP3 phase 4).
        fork_target = self._fork_run_id
        self._fork_run_id = ""
        if fork_target:
            self._run_worker = self._execute_continue(fork_target, text)
            return
        # A follow-up after a finished run continues the same conversation, so
        # the model keeps the prior turn's context instead of starting blank
        # (Phase 2). The first submit of the session starts a fresh run.
        if self._state.run_id:
            self._run_worker = self._execute_continue(self._state.run_id, text)
        else:
            self._run_worker = self._execute_run(text)

    def _expand_mentions(self, text: str) -> str:
        """Expand @path mentions into a note naming the file(s), so the goal
        points the agent at them explicitly. The agent still reads through
        repo.read (provenance and preimage binding stay in the tool channel);
        the mention only saves it a search."""
        import re

        mentions = re.findall(r"(?:^|\s)@([\w./-]+)", text)
        existing = [m for m in mentions if (self._workspace / m).is_file()]
        if not existing:
            return text
        note = " (mentioned files, read them first: " + ", ".join(dict.fromkeys(existing)) + ")"
        return text + note

    def _queue_steering(self, text: str) -> None:
        async def _do() -> None:
            accepted = await self._services.run_service.steer(text)
            if accepted:
                self._log_line("you (queued)", text)
                self._log_line("system", "queued; it reaches the agent at the next turn")
            else:
                self._log_line("system", "no active run to steer; send again to start one")

        self.run_worker(_do(), exclusive=False)

    def _handle_command(self, command: str) -> None:
        name = command.split()[0].lower()
        if name == "/help":
            self._log_line("system", HELP_TEXT)
        elif name == "/budget":
            state = self._state
            budget = getattr(getattr(self._services, "config", None), "budget", None)
            if budget is None:
                self._log_line("system", "budget unavailable before startup completes")
                return
            self._log_line(
                "system",
                f"budget: step {state.step}/{budget.max_steps}, "
                f"tools {state.tool_calls}/{budget.max_tool_calls}, "
                f"tokens {state.input_tokens}/{state.output_tokens}, "
                f"cost ${state.cost_usd:.4f}/{budget.max_cost_usd:.2f}"
                + (" (estimated)" if state.usage_estimated else ""),
            )
        elif name == "/context":
            self._log_line("system", self._state.context_summary or "no context recorded yet")
        elif name == "/sessions":
            self._list_sessions()
        elif name == "/fork":
            parts = command.split(maxsplit=1)
            if len(parts) < 2:
                self._log_line("system", "usage: /fork RUN_ID (see /sessions)")
            else:
                self._fork_run_id = parts[1].strip()
                self._log_line(
                    "system",
                    f"next message will fork from {self._fork_run_id}; type it and press Enter",
                )
        elif name == "/diff":
            self.query_one("#tabs", TabbedContent).active = "tab-diff"
        elif name == "/export":
            self._export_run()
        elif name == "/quit":
            self.exit()
        else:
            self._log_line("system", f"unknown command {name}; try /help")

    def _list_sessions(self) -> None:
        async def _do() -> None:
            runs = await self._services.store.list_runs(10)
            if not runs:
                self._log_line("system", "no recorded runs yet")
                return
            lines = ["recent runs (use /fork RUN_ID to branch from one):"]
            for r in runs:
                lines.append(f"  {r.run_id}  [{r.status.value}]  {r.goal[:60]}")
            self._log_line("system", "\n".join(lines))

        self.run_worker(_do(), exclusive=False)

    def _export_run(self) -> None:
        if not self._state.run_id:
            self._log_line("system", "no run to export yet")
            return

        async def _do() -> None:
            from haven.interfaces.export import render_markdown

            run = await self._services.store.get_run(self._state.run_id)
            envelopes = await self._services.store.load_events(self._state.run_id)
            if run is None:
                self._log_line("system", "run not found in the store")
                return
            target = Path.cwd() / f"haven-{self._state.run_id}.md"
            target.write_text(render_markdown(run, envelopes), encoding="utf-8")
            self._log_line("system", f"exported to {target}")

        self.run_worker(_do(), exclusive=False)

    # -- actions ----------------------------------------------------------------------

    def action_cancel_or_quit(self) -> None:
        if self._state.running and self._run_worker is not None:
            self._log_line("system", "cancelling the current run…")
            self._run_worker.cancel()
            return
        self.exit()

    def action_show_tab(self, tab_id: str) -> None:
        self.query_one("#tabs", TabbedContent).active = tab_id

    async def on_unmount(self) -> None:
        if self._services is not None:
            await self._services.close()
