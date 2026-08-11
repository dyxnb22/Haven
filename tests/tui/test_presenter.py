"""Presenter reducer unit tests (pure, no Textual required)."""

from haven.contracts.events import (
    ApprovalRequested,
    AssistantDelta,
    DiffPreview,
    EventEnvelope,
    EvidenceRecorded,
    ModelCompleted,
    Notice,
    PolicyDecided,
    RunCreated,
    RunFinished,
    ToolProposed,
)
from haven.interfaces.tui.presenter import PresenterState, reduce, sanitize


def wrap(event, seq: int = 1) -> EventEnvelope:  # type: ignore[no-untyped-def]
    return EventEnvelope(seq=seq, at="2026-01-01T00:00:00+00:00", event=event)


def test_run_created_initializes_header() -> None:
    state = reduce(
        PresenterState(),
        wrap(
            RunCreated(
                run_id="run-1",
                workspace="/tmp/proj",
                workspace_digest="d",
                goal="fix bug",
                mode="interactive",
                model_name="gpt-test",
                git_branch="main",
                max_steps=12,
            )
        ),
    )
    assert state.running
    assert state.goal == "fix bug"
    assert "Haven" in state.header_line()
    assert "proj" in state.header_line()
    assert state.timeline[-1].kind == "user"


def test_streaming_then_completion() -> None:
    state = PresenterState(run_id="run-1")
    state = reduce(state, wrap(AssistantDelta(run_id="run-1", step=1, text="Hel")))
    state = reduce(state, wrap(AssistantDelta(run_id="run-1", step=1, text="lo")))
    assert state.streaming_text == "Hello"
    state = reduce(
        state,
        wrap(
            ModelCompleted(
                run_id="run-1",
                step=1,
                text="Hello done",
                tool_call_count=0,
                input_tokens=10,
                output_tokens=5,
                usage_estimated=False,
                ttft_ms=12,
                duration_ms=100,
                finish_reason="stop",
            )
        ),
    )
    assert state.streaming_text == ""  # cleared on completion
    assert "Hello done" in state.chat_text
    assert state.input_tokens == 10


def test_policy_deny_adds_timeline_entry() -> None:
    state = reduce(
        PresenterState(run_id="run-1"),
        wrap(
            PolicyDecided(
                run_id="run-1",
                call_id="c1",
                decision="deny",
                reason_code="outside_workspace",
                risk="high",
            )
        ),
    )
    assert any(e.kind == "policy" and "outside_workspace" in e.text for e in state.timeline)


def test_diff_and_evidence_panels() -> None:
    state = PresenterState(run_id="run-1")
    state = reduce(
        state,
        wrap(
            DiffPreview(
                run_id="run-1",
                files_changed=1,
                insertions=2,
                deletions=1,
                preview="--- a/x\n+++ b/x\n",
            )
        ),
    )
    assert "a/x" in state.diff_text
    state = reduce(
        state,
        wrap(EvidenceRecorded(run_id="run-1", evidence_kind="check", summary="pytest exit=0")),
    )
    assert state.evidence_rows[-1].startswith("[check]")


def test_run_finished_stops_running() -> None:
    state = reduce(
        PresenterState(run_id="run-1", running=True),
        wrap(
            RunFinished(
                run_id="run-1",
                status="succeeded",
                stop_reason="evidence_satisfied",
                steps=4,
                tool_calls=3,
                cost_usd=0.01,
            )
        ),
    )
    assert not state.running
    assert state.status == "succeeded"
    assert "finished" in state.status_line()


class TestSanitize:
    def test_strips_ansi_and_control_chars(self) -> None:
        assert sanitize("hello\x1b[31mred\x07\x00") == "hello[31mred"

    def test_bounds_length(self) -> None:
        assert sanitize("x" * 5000, limit=100).endswith("…[truncated]")


def test_approval_summary_is_sanitized_in_timeline() -> None:
    state = reduce(
        PresenterState(run_id="run-1"),
        wrap(
            ApprovalRequested(
                run_id="run-1",
                call_id="c1",
                approval_id="a1",
                tool_name="repo.edit",
                summary="edit\x1b[31m src/x.py",
                preview="diff",
                risk="medium",
                request_digest="deadbeef",
            )
        ),
    )
    assert "\x1b" not in state.timeline[-1].text


def test_notice_flows_to_timeline() -> None:
    state = reduce(
        PresenterState(run_id="run-1"),
        wrap(Notice(run_id="run-1", level="warning", message="stuck loop")),
    )
    assert state.timeline[-1].kind == "notice"


def test_reducer_is_pure() -> None:
    original = PresenterState(run_id="run-1")
    reduce(
        original,
        wrap(
            ToolProposed(
                run_id="run-1", step=1, call_id="c1", tool_name="repo.read", args_summary="{}"
            )
        ),
    )
    # original is frozen and untouched
    assert original.tool_calls == 0
    assert original.timeline == ()
