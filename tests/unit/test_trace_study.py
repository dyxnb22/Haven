"""The trace study's two counters decide whether a repetition detector can work.

`consecutive_identical` is what `StuckLoopDetector` compares; `repeated_calls`
is what a wider window could catch. Getting either wrong would answer the
question "is non-convergence repetition?" incorrectly, so both are pinned.
"""

import json
from pathlib import Path

from evals.trace_study import Call, RunTrace, read_trace


def _trace(*calls: tuple[str, str, str]) -> RunTrace:
    return RunTrace(
        name="t",
        calls=[Call(i + 1, tool, args, result) for i, (tool, args, result) in enumerate(calls)],
    )


class TestConsecutiveIdentical:
    def test_adjacent_identical_calls_are_counted(self) -> None:
        assert _trace(("read", "a", "x"), ("read", "a", "x")).consecutive_identical == 1

    def test_a_differing_result_breaks_the_run(self) -> None:
        """The detector keys on the observation, not just the call."""
        assert _trace(("read", "a", "x"), ("read", "a", "y")).consecutive_identical == 0

    def test_a_non_adjacent_repeat_is_not_consecutive(self) -> None:
        trace = _trace(("read", "a", "x"), ("read", "b", "y"), ("read", "a", "x"))
        assert trace.consecutive_identical == 0


class TestRepeatedCalls:
    def test_a_non_adjacent_repeat_is_counted(self) -> None:
        trace = _trace(("read", "a", "x"), ("read", "b", "y"), ("read", "a", "x"))
        assert trace.repeated_calls == 1

    def test_a_run_with_no_repeat_counts_zero(self) -> None:
        assert _trace(("read", "a", "x"), ("read", "b", "y")).repeated_calls == 0

    def test_three_of_the_same_call_counts_twice(self) -> None:
        trace = _trace(("read", "a", "x"), ("read", "a", "y"), ("read", "a", "z"))
        assert trace.repeated_calls == 2


def test_read_trace_joins_proposed_to_completed_by_call_id(tmp_path: Path) -> None:
    """The journal splits a call across two events; the step and arguments live
    on one and the result on the other."""
    journal = tmp_path / "run.jsonl"
    journal.write_text(
        "\n".join(
            json.dumps({"event": e})
            for e in (
                {
                    "kind": "tool.proposed",
                    "call_id": "c1",
                    "step": 3,
                    "tool_name": "repo.read",
                    "args_summary": '{"path":"a.py"}',
                },
                {"kind": "tool.completed", "call_id": "c1", "summary": "contents"},
            )
        ),
        encoding="utf-8",
    )
    trace = read_trace(journal)
    assert len(trace.calls) == 1
    assert trace.calls[0] == Call(3, "repo.read", '{"path":"a.py"}', "contents")
    assert trace.steps == 3
