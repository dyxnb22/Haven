"""Comparing two A/B arms by step distribution rather than pass/fail.

The failure this experiment targets is variance-driven, so a binary metric at
this sample size would be unable to distinguish a real effect from noise. Steps
are continuous and are exactly what the intervention is supposed to reduce.
"""

from typing import Any

from evals.nudge_ab import compare, summarize


def _case(case_id: str, steps: int, status: str = "succeeded") -> dict[str, Any]:
    return {"id": case_id, "steps": steps, "status": status, "stop_reason": "final_answer"}


class TestSummarize:
    def test_it_reports_median_and_pass_rate(self) -> None:
        arm = summarize([_case("a", 5), _case("b", 9), _case("c", 20, "stopped")])
        assert arm.n == 3
        assert arm.median_steps == 9
        assert arm.passed == 2

    def test_an_empty_arm_is_not_a_crash(self) -> None:
        assert summarize([]).n == 0


class TestCompare:
    def test_it_pairs_cases_by_id(self) -> None:
        control = [_case("a", 20), _case("b", 10)]
        treatment = [_case("a", 12), _case("b", 10)]
        report = compare(control, treatment)
        assert "a" in report and "-8" in report

    def test_it_states_the_verdict_when_there_is_no_improvement(self) -> None:
        same = [_case("a", 10), _case("b", 12)]
        report = compare(same, list(same))
        assert "no improvement" in report.lower()

    def test_repeated_runs_of_a_case_are_pooled_not_dropped(self) -> None:
        """Three repetitions produce three rows per case id. Pairing on a dict
        keyed by id would silently keep only the last one and throw away
        two-thirds of the sample, which is the whole point of repeating."""
        control = [_case("a", 20), _case("a", 18), _case("a", 22)]
        treatment = [_case("a", 10), _case("a", 12), _case("a", 8)]
        report = compare(control, treatment)
        assert "| control (nudge off) | 3 |" in report
        assert "| treatment (nudge on) | 3 |" in report
