"""根据工具追踪记录评估 Java 定位。

评分最终 prose 会变成关键词测试：列出十个候选文件的代理，得分可能和真正找到答案
的代理一样。追踪记录直接回答“定位花了多少工作”，这是索引要减少的量。

这里的事件形状是真实形状：`tool.proposed` 携带步骤和参数，`tool.completed` 只携带
调用 id 和状态，因此评分器必须将二者连接起来。针对猜测模式编写的评分器会把每次
运行都报告为“从未找到”，造成看似灾难性的结果。
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
        """被拒绝或未找到的读取从未将文件展示给代理，因此计入它会把未发生的定位算
        成成功。"""
        events = [
            _proposed(1, "c1", "src/main/java/Right.java"),
            _completed("c1", status="error"),
        ]
        assert steps_to_first_correct_read(events, ("src/main/java/Right.java",)) is None

    def test_an_absolute_path_still_matches_the_repo_relative_answer(self) -> None:
        """代理可能通过工作区解析后的路径读取；答案键是相对于仓库的路径，后缀匹配
        使二者可比较。"""
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
        """`--events` 每行写入 `{"seq":…, "at":…, "event":{…}}`，而不是裸事件。"""
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
