"""最终答案的有界续写、空回复恢复和证据门禁状态机。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from haven.application.emitter import EventEmitter
from haven.application.run_persistence import RunOutcome
from haven.application.state import RunContext
from haven.contracts.events import Notice
from haven.contracts.model import ModelMessage, ModelResult
from haven.domain.enums import RunStatus, StopReason
from haven.domain.evidence import evaluate_evidence_gate
from haven.ports.workspace import WorkspacePort

MAX_EVIDENCE_NUDGES = 2
MAX_OUTPUT_CONTINUATIONS = 2
MAX_EMPTY_REPLIES = 2

FinishRun = Callable[[RunContext, RunStatus, StopReason, str, str], Awaitable[RunOutcome]]
SaveCheckpoint = Callable[[RunContext], Awaitable[None]]


@dataclass
class AnswerAssembly:
    parts: list[str] = field(default_factory=list)
    continuations: int = 0
    empty_replies: int = 0
    final_text: str = ""


class AnswerResolver:
    """处理没有工具调用的模型回复，直到得到终态或请求下一轮。"""

    def __init__(
        self,
        *,
        emitter: EventEmitter,
        workspace: WorkspacePort,
        verification_available: Callable[[], bool],
        supports_prefix: bool,
        finish: FinishRun,
        checkpoint: SaveCheckpoint,
    ) -> None:
        self._emitter = emitter
        self._workspace = workspace
        self._verification_available = verification_available
        self._supports_prefix = supports_prefix
        self._finish = finish
        self._checkpoint = checkpoint

    async def recover_incomplete_reply(
        self, ctx: RunContext, result: ModelResult, answer: AnswerAssembly
    ) -> RunOutcome | bool:
        if result.finish_reason == "length" and answer.continuations < MAX_OUTPUT_CONTINUATIONS:
            answer.continuations += 1
            answer.parts.append(result.text)
            await self._emitter.emit(
                ctx.run_id,
                Notice(
                    run_id=ctx.run_id,
                    level="warning",
                    message=(
                        "answer hit the output token limit; requesting a "
                        f"continuation ({answer.continuations}/{MAX_OUTPUT_CONTINUATIONS})"
                    ),
                ),
            )
            if self._supports_prefix:
                ctx.transcript.append(
                    ModelMessage(role="assistant", content=result.text, is_prefix=True)
                )
            else:
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
            return True

        if not result.text.strip() and not answer.parts:
            answer.empty_replies += 1
            if answer.empty_replies > MAX_EMPTY_REPLIES:
                await self._emitter.emit(
                    ctx.run_id,
                    Notice(
                        run_id=ctx.run_id,
                        level="error",
                        message="model repeatedly returned no content and no tool calls",
                    ),
                )
                return await self._finish(
                    ctx, RunStatus.STOPPED, StopReason.NO_PROGRESS, answer.final_text, ""
                )
            await self._emitter.emit(
                ctx.run_id,
                Notice(
                    run_id=ctx.run_id,
                    level="warning",
                    message="model returned no content; asking again "
                    f"({answer.empty_replies}/{MAX_EMPTY_REPLIES})",
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
            return True
        return False

    async def finish_with_gate(
        self, ctx: RunContext, result: ModelResult, answer: AnswerAssembly
    ) -> RunOutcome | None:
        answer.final_text = "".join((*answer.parts, result.text))
        answer.parts = []
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
        diff_text = (await self._workspace.run_diff()).diff if ctx.ledger.has_edits else ""
        gate = evaluate_evidence_gate(
            ctx.ledger,
            diff_text,
            verification_available=self._verification_available(),
        )
        if gate.passed:
            reason = (
                StopReason.EVIDENCE_SATISFIED if ctx.ledger.has_edits else StopReason.FINAL_ANSWER
            )
            return await self._finish(
                ctx, RunStatus.SUCCEEDED, reason, answer.final_text, gate.reason_code
            )
        if gate.terminal:
            await self._emitter.emit(
                ctx.run_id,
                Notice(run_id=ctx.run_id, level="error", message=gate.detail),
            )
            return await self._finish(
                ctx,
                RunStatus.STOPPED,
                StopReason.VERIFICATION_UNAVAILABLE,
                answer.final_text,
                gate.reason_code,
            )

        ctx.nudges += 1
        if ctx.nudges > MAX_EVIDENCE_NUDGES:
            return await self._finish(
                ctx,
                RunStatus.STOPPED,
                StopReason.EVIDENCE_MISSING,
                answer.final_text,
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
        return None
