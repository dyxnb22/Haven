"""Scoring Java localization from the tool trace.

Grading the final prose would be a keyword probe: an agent that lists ten
candidate files scores like one that knew. The trace answers the question
directly — how much work did localization take — which is the quantity an index
would reduce.

The event shapes here are the real ones: `tool.proposed` carries the step and
the arguments, `tool.completed` carries only the call id and the status, so the
scorer has to join them. A scorer written against a guessed schema would report
"never found" for every run and look like a devastating result.
"""

import json
from typing import Any

from evals.java.score import (
    load_events,
    render,
    score_run,
    steps_to_first_correct_read,
)


def _proposed(step: int, call_id: str, path: str, tool: str = "repo.read") -> dict[str, Any]:
    return {
        "kind": "tool.proposed",
        "run_id": "r1",
        "step": step,
        "call_id": call_id,
        "tool_name": tool,
        "args_summary": json.dumps({"path": path}),
    }


def _completed(call_id: str, status: str = "ok", tool: str = "repo.read") -> dict[str, Any]:
    return {
        "kind": "tool.completed",
        "run_id": "r1",
        "call_id": call_id,
        "tool_name": tool,
        "status": status,
    }


def _read(step: int, call_id: str, path: str) -> list[dict[str, Any]]:
    return [_proposed(step, call_id, path), _completed(call_id)]


class TestStepsToFirstCorrectRead:
    def test_it_counts_the_step_of_the_hit(self) -> None:
        events = [
            *_read(1, "c1", "src/main/java/Wrong.java"),
            *_read(2, "c2", "src/main/java/Also.java"),
            *_read(3, "c3", "src/main/java/Right.java"),
        ]
        assert steps_to_first_correct_read(events, ("src/main/java/Right.java",)) == 3

    def test_a_run_that_never_reads_the_answer_scores_none(self) -> None:
        events = _read(1, "c1", "src/main/java/Wrong.java")
        assert steps_to_first_correct_read(events, ("src/main/java/Right.java",)) is None

    def test_a_failed_read_is_not_a_hit(self) -> None:
        """A denied or not-found read never showed the agent the file, so
        counting it would credit localization that did not happen."""
        events = [
            _proposed(1, "c1", "src/main/java/Right.java"),
            _completed("c1", status="error"),
        ]
        assert steps_to_first_correct_read(events, ("src/main/java/Right.java",)) is None

    def test_an_absolute_path_still_matches_the_repo_relative_answer(self) -> None:
        """The agent may read through a path the workspace resolved; the answer
        key is repo-relative, and a suffix match is what makes them comparable."""
        events = _read(2, "c1", "/tmp/bigmarket-bench/src/main/java/Right.java")
        assert steps_to_first_correct_read(events, ("src/main/java/Right.java",)) == 2


class TestScoreRun:
    def test_it_counts_searches_and_reads_separately(self) -> None:
        events = [
            _proposed(1, "s1", "", tool="repo.search"),
            _completed("s1", tool="repo.search"),
            *_read(2, "c1", "a/Wrong.java"),
            *_read(3, "c2", "a/Right.java"),
        ]
        score = score_run("t1", "unique-name", events, ("a/Right.java",))

        assert score.found is True
        assert score.steps_to_hit == 3
        assert score.files_read == 2
        assert score.searches == 1
        assert score.total_steps == 3


class TestLoadEvents:
    def test_it_unwraps_the_journal_envelope(self, tmp_path: Any) -> None:
        """`--events` writes `{"seq":…, "at":…, "event":{…}}` per line, not the
        bare event."""
        path = tmp_path / "run.jsonl"
        path.write_text(
            json.dumps({"seq": 1, "at": "now", "event": _proposed(1, "c1", "a/Right.java")}) + "\n",
            encoding="utf-8",
        )
        events = load_events(path)

        assert events[0]["kind"] == "tool.proposed"


class TestRender:
    def test_it_groups_by_kind(self) -> None:
        scores = [
            score_run("t1", "unique-name", _read(1, "c1", "a/R.java"), ("a/R.java",)),
            score_run("t2", "di-wiring", _read(9, "c1", "a/R.java"), ("a/R.java",)),
        ]
        report = render(scores)

        assert "unique-name" in report and "di-wiring" in report
