"""用户追问作为持久化暂停点，不轮询模型、不绕过权限。"""

import asyncio
from pathlib import Path

import pytest

from haven.adapters.providers.scripted import ScriptedModel
from haven.application.approvals import AutoApprover
from haven.application.run_service import build_run_context_from_checkpoint
from haven.application.state import RunContext
from haven.bootstrap import build_services
from haven.contracts.user_input import UserInputResponse
from haven.domain.budget import Budget
from haven.domain.enums import AgentMode, PermissionMode, RunStatus, StopReason
from tests.integration.harness import Harness, finish, make_repo, text, tool


def ask_turn():  # type: ignore[no-untyped-def]
    return [
        tool(
            "ask",
            "user.ask",
            questions=[
                {
                    "id": "scope",
                    "question": "Which scope should I inspect?",
                    "options": ["Parser", "CLI"],
                    "allow_custom": False,
                }
            ],
        ),
        finish("tool_calls"),
    ]


async def test_waiting_is_durable_and_does_not_poll_or_finish(tmp_path: Path) -> None:
    h = Harness(make_repo(tmp_path), [ask_turn(), [text("Inspected parser."), finish()]])
    waiting = await h.service.run("Inspect a component")
    assert waiting.status is RunStatus.WAITING_USER
    assert waiting.stop_reason is StopReason.NEEDS_INPUT
    assert waiting.pending_input is not None
    assert len(h.model.requests_seen) == 1
    assert not h.sink.events_of("run.finished")
    checkpoint = await h.store.load_checkpoint(waiting.run_id)
    assert checkpoint is not None
    assert checkpoint.pending_input == waiting.pending_input
    restored = await h.service.resume(build_run_context_from_checkpoint(checkpoint))
    assert restored.pending_input == waiting.pending_input
    assert restored.steps == waiting.steps
    assert len(h.model.requests_seen) == 1

    outcome = await h.service.answer_run(
        waiting.run_id,
        UserInputResponse(request_id=waiting.pending_input.request_id, answers={"scope": "Parser"}),
    )
    assert outcome.run_id == waiting.run_id
    assert outcome.status is RunStatus.SUCCEEDED
    assert outcome.steps == waiting.steps + 1
    assert len(h.model.requests_seen) == 2
    assert any('"Parser"' in message.content for message in h.model.requests_seen[-1].messages)
    with pytest.raises(ValueError, match="no pending question"):
        await h.service.answer_run(
            waiting.run_id,
            UserInputResponse(
                request_id=waiting.pending_input.request_id, answers={"scope": "Parser"}
            ),
        )


@pytest.mark.parametrize("answers", [{}, {"scope": "Invalid"}, {"other": "Parser"}])
async def test_invalid_answers_leave_the_saved_question_unchanged(
    tmp_path: Path, answers: dict[str, str]
) -> None:
    h = Harness(make_repo(tmp_path), [ask_turn()])
    waiting = await h.service.run("Inspect a component")
    assert waiting.pending_input is not None
    saved = await h.store.load_checkpoint(waiting.run_id)
    with pytest.raises(ValueError):
        await h.service.answer_run(
            waiting.run_id,
            UserInputResponse(request_id=waiting.pending_input.request_id, answers=answers),
        )
    assert await h.store.load_checkpoint(waiting.run_id) == saved
    assert len(h.model.requests_seen) == 1


async def test_stale_response_and_ordinary_continue_cannot_consume_the_question(
    tmp_path: Path,
) -> None:
    h = Harness(make_repo(tmp_path), [ask_turn()])
    waiting = await h.service.run("Inspect a component")
    with pytest.raises(ValueError, match="request_id"):
        await h.service.answer_run(
            waiting.run_id,
            UserInputResponse(request_id="old-question", answers={"scope": "Parser"}),
        )
    with pytest.raises(ValueError, match="needs input"):
        await h.service.continue_run(waiting.run_id, "Choose Parser")


async def test_cancel_does_not_call_the_model_again(tmp_path: Path) -> None:
    h = Harness(make_repo(tmp_path), [ask_turn()])
    waiting = await h.service.run("Inspect a component")
    assert await h.service.cancel(waiting.run_id)
    record = await h.store.get_run(waiting.run_id)
    assert record is not None
    assert record.status is RunStatus.CANCELLED
    assert len(h.model.requests_seen) == 1


async def test_user_input_is_a_barrier_to_other_calls_in_the_same_batch(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    h = Harness(
        repo,
        [
            [
                tool("before", "repo.create", path="before.txt", content="must not execute"),
                ask_turn()[0],
                tool("after", "repo.create", path="after.txt", content="must not execute"),
                finish("tool_calls"),
            ],
            [text("Understood."), finish()],
        ],
    )
    waiting = await h.service.run("Inspect a component")
    assert waiting.pending_input is not None
    assert not (repo / "before.txt").exists()
    assert not (repo / "after.txt").exists()
    await h.service.answer_run(
        waiting.run_id,
        UserInputResponse(request_id=waiting.pending_input.request_id, answers={"scope": "CLI"}),
    )
    messages = h.model.requests_seen[-1].messages
    assert {m.tool_call_id for m in messages if m.role == "tool"} == {"before", "after", "ask"}


async def test_waiting_time_is_excluded_from_the_wall_clock_budget(tmp_path: Path) -> None:
    h = Harness(
        make_repo(tmp_path),
        [ask_turn(), [text("Inspected."), finish()]],
        budget=Budget(max_wall_time_seconds=0.5),
    )
    waiting = await h.service.run("Inspect a component")
    assert waiting.pending_input is not None
    await asyncio.sleep(0.65)
    outcome = await h.service.answer_run(
        waiting.run_id,
        UserInputResponse(request_id=waiting.pending_input.request_id, answers={"scope": "Parser"}),
    )
    assert outcome.status is RunStatus.SUCCEEDED


async def test_answering_keeps_planning_read_only(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    h = Harness(
        repo,
        [
            ask_turn(),
            [
                tool("create", "repo.create", path="forbidden.txt", content="forbidden"),
                finish("tool_calls"),
            ],
            [
                tool("plan", "task.plan", steps=[{"title": "Inspect parser", "status": "pending"}]),
                finish("tool_calls"),
            ],
            [text("Plan ready."), finish()],
        ],
    )
    waiting = await h.service.run("Plan an inspection", agent_mode=AgentMode.PLAN)
    assert waiting.pending_input is not None
    outcome = await h.service.answer_run(
        waiting.run_id,
        UserInputResponse(request_id=waiting.pending_input.request_id, answers={"scope": "Parser"}),
    )
    assert outcome.status is RunStatus.SUCCEEDED
    assert not (repo / "forbidden.txt").exists()


async def test_answer_survives_a_crash_before_the_next_model_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo(tmp_path)
    db = tmp_path / "questions.db"
    first = await build_services(
        repo,
        mode=PermissionMode.READ_ONLY,
        sinks=[],
        approvals=AutoApprover("reject_all"),
        model=ScriptedModel([ask_turn()]),
        store_path=db,
    )
    try:
        waiting = await first.runtime.start_run("Inspect a component")
    finally:
        await first.close()
    assert waiting.pending_input is not None
    second = await build_services(
        repo,
        mode=PermissionMode.READ_ONLY,
        sinks=[],
        approvals=AutoApprover("reject_all"),
        model=ScriptedModel([]),
        store_path=db,
    )

    async def crash_before_model(ctx: RunContext) -> None:
        raise RuntimeError("simulated process loss before model execution")

    monkeypatch.setattr(second.run_service, "_drive", crash_before_model)
    try:
        with pytest.raises(RuntimeError, match="simulated process loss"):
            await second.runtime.answer_run(
                waiting.run_id,
                UserInputResponse(
                    request_id=waiting.pending_input.request_id, answers={"scope": "Parser"}
                ),
            )
        checkpoint = await second.store.load_checkpoint(waiting.run_id)
        assert checkpoint is not None
        assert checkpoint.pending_input is None
        assert any('"Parser"' in message.content for message in checkpoint.messages)
    finally:
        await second.close()
    third = await build_services(
        repo,
        mode=PermissionMode.READ_ONLY,
        sinks=[],
        approvals=AutoApprover("reject_all"),
        model=ScriptedModel([[text("Inspected."), finish()]]),
        store_path=db,
    )
    try:
        outcome = await third.runtime.resume_run(waiting.run_id)
        assert outcome.status is RunStatus.SUCCEEDED
    finally:
        await third.close()
