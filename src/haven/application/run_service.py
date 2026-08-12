"""RunService: owns the bounded agent loop for one run.

Loop shape: Model -> Tool(s) -> Observation -> Model ... until the program
decides to stop. Every stop has exactly one reason; success additionally
requires the Evidence Gate to pass.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from haven.application.approvals import ApprovalResponder
from haven.application.context_builder import ContextBuilder
from haven.application.emitter import EventEmitter
from haven.application.profiles import profile_for
from haven.application.registry import ToolRegistry
from haven.application.state import RunContext
from haven.application.tool_pipeline import ToolPipeline
from haven.contracts.checkpoint import (
    BudgetSnapshot,
    CheckpointV1,
    EvidenceSnapshot,
    UsageSnapshot,
)
from haven.contracts.events import (
    AssistantDelta,
    AssistantReasoning,
    ContextBuilt,
    ModelCompleted,
    Notice,
    RunCreated,
    RunFinished,
    SteerQueued,
    StepStarted,
    StreamRestarted,
)
from haven.contracts.model import (
    ModelMessage,
    ModelRequest,
    ModelResult,
    ReasoningDelta,
    StreamFinished,
    TextDelta,
    ToolCallProposal,
    ToolCallReady,
    Usage,
    UsageReport,
)
from haven.contracts.tools import RecipeSpec, tool_schemas
from haven.domain.budget import Budget, check_budget
from haven.domain.digest import digest_of
from haven.domain.enums import PermissionMode, RunStatus, StopReason
from haven.domain.evidence import evaluate_evidence_gate
from haven.domain.ids import RunId, new_run_id
from haven.domain.pricing import Pricing
from haven.domain.stuck import StuckLoopDetector
from haven.ports.executor import ExecutorPort
from haven.ports.model import ModelPort, ProviderError
from haven.ports.sandbox import SandboxLauncher
from haven.ports.session import SessionStorePort
from haven.ports.workspace import WorkspacePort

MAX_EVIDENCE_NUDGES = 2

#: An answer the provider cut at its output-token limit is continued at most
#: this many times before Haven proceeds with the partial text (with a warning),
#: so a model that truncates forever cannot spend the whole budget.
MAX_OUTPUT_CONTINUATIONS = 2

#: A reply with neither text nor tool calls (e.g. a reasoning-only response) is
#: re-prompted this many times before the run stops for lack of progress.
MAX_EMPTY_REPLIES = 2

#: Transient provider failures are common enough on real networks that losing a
#: whole run to one is the wrong default. Measured: 3 of 8 live runs hit a
#: ConnectError before any token arrived, and 2 of 31 real-repo cases later died
#: on one the adapter was still classifying as non-retryable — this loop is only
#: as good as that classification.
MODEL_RETRY_ATTEMPTS = 2
MODEL_RETRY_BASE_DELAY = 1.0


@dataclass(slots=True)
class _StreamProgress:
    """Whether a stream produced anything before failing; gates retry safety."""

    started: bool = False


@dataclass(frozen=True, slots=True)
class RunOutcome:
    run_id: str
    status: RunStatus
    stop_reason: StopReason
    gate_reason: str
    steps: int
    tool_calls: int
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    cost_usd: float
    usage_estimated: bool
    final_text: str


class RunService:
    def __init__(
        self,
        *,
        model: ModelPort,
        workspace: WorkspacePort,
        executor: ExecutorPort,
        store: SessionStorePort,
        emitter: EventEmitter,
        approvals: ApprovalResponder,
        recipes: dict[str, RecipeSpec],
        mode: PermissionMode,
        budget: Budget,
        pricing: Pricing | None = None,
        git_branch: str = "",
        git_commit: str = "",
        project_guidance: str = "",
        launcher: SandboxLauncher | None = None,
    ) -> None:
        self._model = model
        self._workspace = workspace
        self._store = store
        self._emitter = emitter
        self._mode = mode
        self._budget = budget
        # Per-model defaults; an unknown model inherits Haven's historical
        # behavior rather than numbers guessed from a similar name.
        self._profile = profile_for(model.model_name)
        # Configured rates win; otherwise fall back to the model's published
        # rate card. Reporting a documented price for the model actually in use
        # beats reporting $0.00, but it is a published figure and not an
        # invoice — see the dated comment on the profile.
        self._pricing = pricing if pricing is not None else self._profile.pricing
        self._git_branch = git_branch
        self._git_commit = git_commit
        self._project_guidance = project_guidance
        self._recipes = recipes
        self._registry = ToolRegistry()
        self._launcher = launcher
        # Steering: user input accepted while a run is active, delivered only
        # at a turn boundary so the tool channel is never interrupted
        # mid-effect. Journaled on arrival (durable), drained by the loop.
        self._steer_queue: deque[str] = deque()
        self._active_run_id: str | None = None
        # One scratch directory per service, removed when a run finishes. It
        # exists so sandboxed tools that must write somewhere do not need write
        # access outside the workspace.
        self._scratch_dir = Path(tempfile.mkdtemp(prefix="haven-scratch-"))
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
        )

    # -- entry points -------------------------------------------------------

    async def run(self, goal: str) -> RunOutcome:
        goal = goal.strip()
        if not (3 <= len(goal) <= 4000):
            raise ValueError("goal must be between 3 and 4000 characters")

        ctx = RunContext(
            run_id=new_run_id(),
            goal=goal,
            mode=self._mode,
            budget=self._budget,
        )
        await self._announce_run(ctx, goal)
        return await self._drive(ctx)

    async def continue_run(self, previous_run_id: str, follow_up: str) -> RunOutcome:
        """Start a follow-up turn that inherits the prior run's transcript.

        The prior conversation is carried forward so the model has context, but
        this is a fresh Run: a new id, a fresh budget, and a fresh evidence
        ledger, so the follow-up's success is judged on its own edits. The
        session's head goal stays stable (good for the prompt cache) and the
        follow-up is threaded in as a user message.
        """
        follow_up = follow_up.strip()
        if not (3 <= len(follow_up) <= 4000):
            raise ValueError("follow-up must be between 3 and 4000 characters")

        checkpoint = await self._store.load_checkpoint(previous_run_id)
        if checkpoint is None:
            raise ValueError(f"no checkpoint for run {previous_run_id!r}; cannot continue it")

        # Refuse to graft a run's transcript and file state onto a different
        # repository: recovery makes this check, and a follow-up must too.
        if checkpoint.workspace_digest != self._workspace.workspace_digest:
            raise ValueError(
                "workspace identity changed since the run being continued; "
                "continue it from the same workspace"
            )

        # A follow-up is a new turn: its run diff must show only its own
        # changes, so the run-scoped originals from the prior turn are reset
        # (harmless in a fresh process, load-bearing in the long-lived TUI
        # workspace that a session reuses).
        self._workspace.restore_originals({})

        transcript = list(checkpoint.messages)
        transcript.append(ModelMessage(role="user", content=f"Follow-up request: {follow_up}"))
        ctx = RunContext(
            run_id=new_run_id(),
            goal=checkpoint.goal,
            mode=self._mode,
            budget=self._budget,
            transcript=transcript,
            plan=checkpoint.plan,
            files_read=dict(checkpoint.files_read),
        )
        await self._announce_run(ctx, checkpoint.goal, parent_run_id=previous_run_id)
        await self._emitter.emit(
            ctx.run_id,
            Notice(run_id=ctx.run_id, level="info", message=f"continuing session: {follow_up}"),
        )
        return await self._drive(ctx)

    async def _announce_run(self, ctx: RunContext, goal: str, *, parent_run_id: str = "") -> None:
        await self._store.create_run(
            ctx.run_id,
            str(self._workspace.root),
            self._workspace.workspace_digest,
            goal,
            self._mode.value,
        )
        await self._emitter.emit(
            ctx.run_id,
            RunCreated(
                run_id=ctx.run_id,
                workspace=str(self._workspace.root),
                workspace_digest=self._workspace.workspace_digest,
                goal=goal,
                mode=self._mode.value,
                model_name=self._model.model_name,
                git_branch=self._git_branch,
                git_commit=self._git_commit,
                max_steps=self._budget.max_steps,
                sandbox_backend=(self._launcher.backend if self._launcher is not None else "none"),
                parent_run_id=parent_run_id,
            ),
        )

    async def resume(self, ctx: RunContext) -> RunOutcome:
        """Continue a recovered run context (built by RecoveryService)."""
        await self._emitter.emit(
            ctx.run_id,
            Notice(run_id=ctx.run_id, level="info", message="run resumed from checkpoint"),
        )
        return await self._drive(ctx)

    @property
    def active_run_id(self) -> str | None:
        """The run currently being driven, if any."""
        return self._active_run_id

    async def steer(self, text: str) -> bool:
        """Queue user input for the active run, delivered at the next turn
        boundary.

        Nothing is interrupted: the current model call and any in-flight tool
        execution finish untouched; the text becomes a user message before the
        next model request. Returns False when no run is active (the caller
        should start a run or a follow-up instead). The queued text is
        journaled immediately, so it survives a crash.
        """
        text = text.strip()
        if not text or self._active_run_id is None:
            return False
        self._steer_queue.append(text)
        await self._emitter.emit(
            self._active_run_id,
            SteerQueued(run_id=self._active_run_id, text=text),
        )
        return True

    def _drain_steering(self, ctx: RunContext) -> list[str]:
        drained: list[str] = []
        while self._steer_queue:
            drained.append(self._steer_queue.popleft())
        return drained

    # -- the loop -------------------------------------------------------------

    async def _drive(self, ctx: RunContext) -> RunOutcome:
        builder = ContextBuilder(
            goal=ctx.goal,
            tools=tool_schemas(),
            budget=ctx.budget,
            recipe_ids=tuple(self._recipes),
            project_guidance=self._project_guidance,
            sandbox_backend=self._launcher.backend if self._launcher is not None else "",
            max_context_chars=self._profile.max_context_chars,
            reasoning_effort=self._profile.reasoning_effort,
        )
        stuck = StuckLoopDetector()
        started = time.monotonic()
        elapsed_base = ctx.usage.wall_time_seconds
        final_text = ""
        # Output-truncation recovery: parts of an answer the provider cut off
        # at its output-token limit, stitched back together across bounded
        # continuation requests (a truncated answer must never be silently
        # accepted as a complete one).
        answer_parts: list[str] = []
        continuations = 0
        empty_replies = 0

        self._active_run_id = ctx.run_id
        self._steer_queue.clear()
        try:
            while True:
                ctx.usage = ctx.usage.with_wall_time(elapsed_base + (time.monotonic() - started))
                if stop := check_budget(ctx.budget, ctx.usage):
                    return await self._finish(ctx, RunStatus.STOPPED, stop, final_text)

                ctx.usage = ctx.usage.charge_step()
                step = ctx.usage.steps
                if ctx.status is RunStatus.CREATED:
                    ctx.move_to(RunStatus.RUNNING_MODEL)
                await self._emitter.emit(ctx.run_id, StepStarted(run_id=ctx.run_id, step=step))

                # Turn boundary: queued steering becomes ordinary user
                # messages before the next model request — never mid-stream,
                # never mid-tool-call.
                for steered in self._drain_steering(ctx):
                    ctx.transcript.append(
                        ModelMessage(role="user", content=f"User update: {steered}")
                    )
                    await self._emitter.emit(
                        ctx.run_id,
                        Notice(
                            run_id=ctx.run_id,
                            level="info",
                            message=f"steering delivered: {steered[:160]}",
                        ),
                    )

                request, segments = builder.build(ctx.transcript, ctx.usage, ctx.plan)
                await self._emitter.emit(
                    ctx.run_id,
                    ContextBuilt(
                        run_id=ctx.run_id,
                        step=step,
                        segments=segments,
                        total_bytes=sum(s.size_bytes for s in segments),
                    ),
                )

                try:
                    result = await self._stream_model(ctx, step, request)
                except ProviderError as exc:
                    await self._emitter.emit(
                        ctx.run_id,
                        Notice(
                            run_id=ctx.run_id,
                            level="error",
                            message=f"provider error ({exc.code}): {exc}",
                        ),
                    )
                    return await self._finish(
                        ctx, RunStatus.FAILED, StopReason.PROVIDER_ERROR, final_text
                    )

                self._charge_usage(ctx, request, result)
                await self._emitter.emit(
                    ctx.run_id,
                    ModelCompleted(
                        run_id=ctx.run_id,
                        step=step,
                        text=result.text,
                        tool_call_count=len(result.tool_calls),
                        input_tokens=result.usage.input_tokens,
                        output_tokens=result.usage.output_tokens,
                        usage_estimated=result.usage.estimated,
                        ttft_ms=result.ttft_ms,
                        duration_ms=result.duration_ms,
                        finish_reason=result.finish_reason,
                        reasoning_tokens=result.usage.reasoning_tokens,
                        cached_input_tokens=result.usage.cached_input_tokens,
                    ),
                )
                ctx.transcript.append(
                    ModelMessage(
                        role="assistant",
                        content=result.text,
                        tool_calls=result.tool_calls,
                        provider_reasoning=result.provider_reasoning,
                    )
                )

                if result.tool_calls:
                    stopped = await self._handle_tool_calls(ctx, step, result.tool_calls, stuck)
                    await self._checkpoint(ctx)
                    if stopped is not None:
                        return stopped
                    continue

                # An answer cut off at the provider's output-token limit is not
                # a final answer. Ask for the rest — bounded, because a model
                # that truncates forever must not be able to spend the budget.
                if result.finish_reason == "length" and continuations < MAX_OUTPUT_CONTINUATIONS:
                    continuations += 1
                    answer_parts.append(result.text)
                    await self._emitter.emit(
                        ctx.run_id,
                        Notice(
                            run_id=ctx.run_id,
                            level="warning",
                            message=(
                                "answer hit the output token limit; requesting a "
                                f"continuation ({continuations}/{MAX_OUTPUT_CONTINUATIONS})"
                            ),
                        ),
                    )
                    if self._profile.supports_assistant_prefix:
                        # Native prefix continuation (ADR 0022): re-issue with
                        # the partial answer as an assistant *prefix* the model
                        # extends in place — no seam duplication, no extra user
                        # turn. The adapter recognises a trailing assistant
                        # message and sends it with the provider's prefix flag.
                        ctx.transcript.append(
                            ModelMessage(role="assistant", content=result.text, is_prefix=True)
                        )
                    else:
                        # Conversational shim: ask the next turn to continue.
                        # Can duplicate at the seam and costs a full extra
                        # request, but needs no provider-specific support.
                        ctx.transcript.append(
                            ModelMessage(
                                role="user",
                                content=(
                                    "Your previous message was cut off at the output token "
                                    "limit. Continue exactly from where it stopped, without "
                                    "repeating anything. If no answer text was produced yet, "
                                    "give the answer directly and concisely."
                                ),
                            )
                        )
                    continue

                # A reply with neither text nor tool calls (a reasoning-only
                # response) would sail through the no-edit gate as an empty
                # answer. Re-prompt, bounded.
                if not result.text.strip() and not answer_parts:
                    empty_replies += 1
                    if empty_replies > MAX_EMPTY_REPLIES:
                        await self._emitter.emit(
                            ctx.run_id,
                            Notice(
                                run_id=ctx.run_id,
                                level="error",
                                message="model repeatedly returned no content and no tool calls",
                            ),
                        )
                        return await self._finish(
                            ctx, RunStatus.STOPPED, StopReason.NO_PROGRESS, final_text
                        )
                    await self._emitter.emit(
                        ctx.run_id,
                        Notice(
                            run_id=ctx.run_id,
                            level="warning",
                            message="model returned no content; asking again "
                            f"({empty_replies}/{MAX_EMPTY_REPLIES})",
                        ),
                    )
                    ctx.transcript.append(
                        ModelMessage(
                            role="user",
                            content=(
                                "Your reply contained no answer text and no tool calls. "
                                "Reply with either a tool call or your answer."
                            ),
                        )
                    )
                    continue

                # Final answer: the Evidence Gate decides, not the model.
                final_text = "".join((*answer_parts, result.text))
                answer_parts = []
                if result.finish_reason == "length":
                    await self._emitter.emit(
                        ctx.run_id,
                        Notice(
                            run_id=ctx.run_id,
                            level="warning",
                            message=(
                                "answer still truncated after "
                                f"{MAX_OUTPUT_CONTINUATIONS} continuations; "
                                "proceeding with the partial answer"
                            ),
                        ),
                    )
                ctx.move_to(RunStatus.VERIFYING)
                # Re-read the accumulated diff so the review sees what is on
                # disk now, not what a stale event recorded.
                diff_text = (await self._workspace.run_diff()).diff if ctx.ledger.has_edits else ""
                gate = evaluate_evidence_gate(
                    ctx.ledger, diff_text, verification_available=bool(self._recipes)
                )
                if gate.passed:
                    reason = (
                        StopReason.EVIDENCE_SATISFIED
                        if ctx.ledger.has_edits
                        else StopReason.FINAL_ANSWER
                    )
                    return await self._finish(
                        ctx, RunStatus.SUCCEEDED, reason, final_text, gate.reason_code
                    )

                if gate.terminal:
                    # Nudging here would loop until the budget dies without any
                    # possibility of success, and report the wrong reason.
                    await self._emitter.emit(
                        ctx.run_id,
                        Notice(run_id=ctx.run_id, level="error", message=gate.detail),
                    )
                    return await self._finish(
                        ctx,
                        RunStatus.STOPPED,
                        StopReason.VERIFICATION_UNAVAILABLE,
                        final_text,
                        gate.reason_code,
                    )

                ctx.nudges += 1
                if ctx.nudges > MAX_EVIDENCE_NUDGES:
                    return await self._finish(
                        ctx,
                        RunStatus.STOPPED,
                        StopReason.EVIDENCE_MISSING,
                        final_text,
                        gate.reason_code,
                    )
                await self._emitter.emit(
                    ctx.run_id,
                    Notice(
                        run_id=ctx.run_id,
                        level="warning",
                        message=f"final answer rejected by evidence gate: {gate.detail}",
                    ),
                )
                ctx.transcript.append(
                    ModelMessage(
                        role="user",
                        content=(
                            "Your answer was NOT accepted as success: "
                            f"{gate.detail} Run repo.diff and a repo.check recipe to "
                            "produce fresh evidence, then answer again."
                        ),
                    )
                )
                ctx.move_to(RunStatus.RUNNING_MODEL)
                await self._checkpoint(ctx)
        except asyncio.CancelledError:
            await self._finish(ctx, RunStatus.CANCELLED, StopReason.CANCELLED, final_text)
            raise
        finally:
            self._active_run_id = None
            if self._steer_queue:
                # Undelivered steering must not leak into a later run; the
                # queued events stay in the journal for the record.
                self._steer_queue.clear()
            shutil.rmtree(self._scratch_dir, ignore_errors=True)

    async def _handle_tool_calls(
        self,
        ctx: RunContext,
        step: int,
        calls: tuple[ToolCallProposal, ...],
        stuck: StuckLoopDetector,
    ) -> RunOutcome | None:
        for call in calls:
            if ctx.usage.tool_calls >= ctx.budget.max_tool_calls:
                return await self._finish(
                    ctx, RunStatus.STOPPED, StopReason.TOOL_BUDGET_EXHAUSTED, ""
                )
            ctx.usage = ctx.usage.charge_tool_call()
            ctx.move_to(RunStatus.VALIDATING_TOOL)

            execution = await self._pipeline.execute(ctx, step, call)
            result = execution.result
            ctx.transcript.append(
                ModelMessage(
                    role="tool",
                    content=(
                        f'<tool_output tool="{call.tool_name}">\n'
                        f"{result.to_model_text()}\n</tool_output>"
                    ),
                    tool_call_id=call.call_id,
                )
            )

            if execution.effect_unknown:
                ctx.move_to(RunStatus.EFFECT_UNKNOWN)
                return await self._finish(
                    ctx, RunStatus.EFFECT_UNKNOWN, StopReason.EFFECT_UNKNOWN, ""
                )

            if ctx.status is not RunStatus.RUNNING_MODEL:
                ctx.move_to(RunStatus.RUNNING_MODEL)

            fingerprint = digest_of([call.tool_name, call.arguments_json, result.to_model_text()])
            if stuck.observe(fingerprint):
                await self._emitter.emit(
                    ctx.run_id,
                    Notice(
                        run_id=ctx.run_id,
                        level="warning",
                        message=(
                            f"stuck loop: {call.tool_name} repeated with identical "
                            "arguments and results"
                        ),
                    ),
                )
                return await self._finish(ctx, RunStatus.STOPPED, StopReason.NO_PROGRESS, "")
        return None

    # -- model streaming ---------------------------------------------------------

    async def _stream_model(self, ctx: RunContext, step: int, request: ModelRequest) -> ModelResult:
        """Stream one model turn, retrying only when it is provably safe.

        A model call has no side effects, so retrying a connection failure
        cannot double-apply anything — unlike a tool call, which is never
        retried. A partially streamed turn is also safe to retry: the assembled
        text and tool calls are local to the attempt and discarded, and nothing
        reaches the transcript or the tool pipeline until the turn completes.
        Only the already-displayed text is stale, so the UI is told to reset.
        """
        for attempt in range(MODEL_RETRY_ATTEMPTS + 1):
            progress = _StreamProgress()
            try:
                return await self._stream_once(ctx, step, request, progress)
            except ProviderError as exc:
                exhausted = attempt == MODEL_RETRY_ATTEMPTS
                if not exc.retryable or exhausted:
                    raise
                if progress.started:
                    await self._emitter.emit(
                        ctx.run_id, StreamRestarted(run_id=ctx.run_id, step=step)
                    )
                delay = MODEL_RETRY_BASE_DELAY * (2**attempt)
                await self._emitter.emit(
                    ctx.run_id,
                    Notice(
                        run_id=ctx.run_id,
                        level="warning",
                        message=(
                            f"provider error ({exc.code}); retrying in {delay:.1f}s "
                            f"({attempt + 1}/{MODEL_RETRY_ATTEMPTS})"
                        ),
                    ),
                )
                await asyncio.sleep(delay)
        raise ProviderError("server", "model retry loop exhausted")

    async def _stream_once(
        self,
        ctx: RunContext,
        step: int,
        request: ModelRequest,
        progress: _StreamProgress,
    ) -> ModelResult:
        started = time.monotonic()
        ttft_ms = 0
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls: list[ToolCallProposal] = []
        usage = Usage()
        finish: Literal["stop", "tool_calls", "length", "error"] = "stop"

        async for event in self._model.generate_stream(request):
            progress.started = True
            if ttft_ms == 0:
                ttft_ms = max(1, int((time.monotonic() - started) * 1000))
            if isinstance(event, TextDelta):
                text_parts.append(event.text)
                await self._emitter.emit(
                    ctx.run_id,
                    AssistantDelta(run_id=ctx.run_id, step=step, text=event.text),
                )
            elif isinstance(event, ReasoningDelta):
                # Shown so a long think is visible, and captured for wire replay
                # (ADR 0014) — but deliberately kept out of `text`: reasoning is
                # not the answer and must never re-enter the transcript as
                # assistant content.
                reasoning_parts.append(event.text)
                await self._emitter.emit(
                    ctx.run_id,
                    AssistantReasoning(run_id=ctx.run_id, step=step, text=event.text),
                )
            elif isinstance(event, ToolCallReady):
                tool_calls.append(event.call)
            elif isinstance(event, UsageReport):
                usage = event.usage
            elif isinstance(event, StreamFinished):
                finish = event.finish_reason

        return ModelResult(
            text="".join(text_parts),
            tool_calls=tuple(tool_calls),
            usage=usage,
            finish_reason="tool_calls" if tool_calls else finish,
            ttft_ms=ttft_ms,
            duration_ms=int((time.monotonic() - started) * 1000),
            provider_reasoning="".join(reasoning_parts),
        )

    def _charge_usage(self, ctx: RunContext, request: ModelRequest, result: ModelResult) -> None:
        usage = result.usage
        estimated = False
        input_tokens = usage.input_tokens
        output_tokens = usage.output_tokens
        if input_tokens == 0 and output_tokens == 0:
            # Provider gave no usage: estimate conservatively and say so.
            estimated = True
            input_tokens = sum(len(m.content) for m in request.messages) // 4
            output_tokens = max(1, len(result.text) // 4)
        cost = self._pricing.cost(input_tokens, output_tokens, usage.cached_input_tokens)
        ctx.usage = ctx.usage.charge_tokens(
            input_tokens,
            output_tokens,
            cost,
            estimated=estimated,
            cached_input_tokens=usage.cached_input_tokens,
        )

    # -- persistence -------------------------------------------------------------

    async def _checkpoint(self, ctx: RunContext) -> None:
        ctx.last_seq = self._emitter.last_seq(ctx.run_id)
        original_artifacts: dict[str, str] = {}
        for path, content in self._workspace.original_contents().items():
            original_artifacts[path] = await self._store.put_artifact(content.encode("utf-8"))
        checkpoint = CheckpointV1(
            run_id=ctx.run_id,
            workspace_digest=self._workspace.workspace_digest,
            goal=ctx.goal,
            mode=ctx.mode.value,
            status=ctx.status.value,
            last_seq=ctx.last_seq,
            budget=BudgetSnapshot.from_domain(ctx.budget),
            usage=UsageSnapshot.from_domain(ctx.usage),
            messages=tuple(ctx.transcript),
            evidence=EvidenceSnapshot.from_domain(ctx.ledger),
            files_read=dict(ctx.files_read),
            plan=ctx.plan,
            original_artifacts=original_artifacts,
        )
        await self._store.save_checkpoint(checkpoint)

    async def _finish(
        self,
        ctx: RunContext,
        status: RunStatus,
        stop_reason: StopReason,
        final_text: str,
        gate_reason: str = "",
    ) -> RunOutcome:
        if ctx.status is not status:
            ctx.status = status  # direct set: _finish targets are always terminal-ish
        await self._store.update_run_status(ctx.run_id, status, stop_reason.value)
        await self._checkpoint(ctx)
        await self._emitter.emit(
            ctx.run_id,
            RunFinished(
                run_id=ctx.run_id,
                status=status.value,
                stop_reason=stop_reason.value,
                gate_reason=gate_reason,
                steps=ctx.usage.steps,
                tool_calls=ctx.usage.tool_calls,
                input_tokens=ctx.usage.input_tokens,
                output_tokens=ctx.usage.output_tokens,
                cached_input_tokens=ctx.usage.cached_input_tokens,
                cost_usd=round(ctx.usage.cost_usd, 6),
                usage_estimated=ctx.usage.usage_estimated,
                duration_ms=int(ctx.usage.wall_time_seconds * 1000),
            ),
        )
        return RunOutcome(
            run_id=ctx.run_id,
            status=status,
            stop_reason=stop_reason,
            gate_reason=gate_reason,
            steps=ctx.usage.steps,
            tool_calls=ctx.usage.tool_calls,
            input_tokens=ctx.usage.input_tokens,
            output_tokens=ctx.usage.output_tokens,
            cached_input_tokens=ctx.usage.cached_input_tokens,
            cost_usd=round(ctx.usage.cost_usd, 6),
            usage_estimated=ctx.usage.usage_estimated,
            final_text=final_text,
        )


def build_run_context_from_checkpoint(
    checkpoint: CheckpointV1,
) -> RunContext:
    """Rebuild working state from a checkpoint (used by recovery)."""
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
        last_seq=checkpoint.last_seq,
    )
    # Resumed runs continue from the model turn.
    ctx.status = RunStatus.RUNNING_MODEL
    return ctx
