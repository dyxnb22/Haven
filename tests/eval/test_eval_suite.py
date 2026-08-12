"""The offline eval suite is itself a CI gate: it must pass and stay clean."""

from pathlib import Path

import pytest

from haven.evalkit.runner import run_suite

REPO_ROOT = Path(__file__).resolve().parents[2]
CASES_DIR = REPO_ROOT / "evals" / "cases"


@pytest.fixture(scope="module")
def report(tmp_path_factory: pytest.TempPathFactory):  # type: ignore[no-untyped-def]
    import asyncio

    out = tmp_path_factory.mktemp("eval_report")
    return asyncio.run(run_suite(cases_dir=CASES_DIR, out_dir=out))


def test_has_at_least_20_cases(report) -> None:  # type: ignore[no-untyped-def]
    assert len(report.results) >= 20


def test_all_cases_pass(report) -> None:  # type: ignore[no-untyped-def]
    failing = [(r.case_id, r.failures) for r in report.results if not r.passed]
    assert not failing, f"eval cases failed: {failing}"


def test_no_security_violations(report) -> None:  # type: ignore[no-untyped-def]
    assert report.security_violations == 0


def test_quality_is_reported_apart_from_safety(report) -> None:  # type: ignore[no-untyped-def]
    """Task success and the safety guarantee are headlined separately, never
    averaged into one number."""
    assert report.quality_total > 0
    assert report.quality_passed == report.quality_total  # offline scripts all pass
    assert "quality" in report.summary_line()


def test_covers_required_categories(report) -> None:  # type: ignore[no-untyped-def]
    categories = {r.category for r in report.results}
    assert {"task", "robustness", "security", "injection", "budget", "recovery"} <= categories


def test_reports_written(report) -> None:  # type: ignore[no-untyped-def]
    assert "cases" in report.to_json()
    assert "Haven offline eval report" in report.to_markdown()


def test_offline_report_is_labelled_offline(report) -> None:  # type: ignore[no-untyped-def]
    assert report.live is False
    assert '"mode": "offline"' in report.to_json()


def test_category_filter_selects_a_subset(tmp_path: Path) -> None:
    import asyncio

    from haven.evalkit.runner import run_suite

    subset = asyncio.run(run_suite(cases_dir=CASES_DIR, out_dir=tmp_path, categories=("security",)))
    assert subset.results
    assert {r.category for r in subset.results} == {"security"}
    assert len(subset.results) < 20


def test_unknown_category_is_an_error(tmp_path: Path) -> None:
    import asyncio

    from haven.evalkit.runner import run_suite

    with pytest.raises(FileNotFoundError, match="matched the selection"):
        asyncio.run(run_suite(cases_dir=CASES_DIR, out_dir=tmp_path, categories=("nope",)))


def test_discover_mode_registers_what_discovery_proposes(tmp_path: Path) -> None:
    """A `discover: true` case simulates the zero-config flow: the harness runs
    recipe discovery on the fixture and registers the suggestions, exactly as a
    user accepting `haven discover` output would."""
    import sys

    from haven.evalkit.runner import _discovered_recipes

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("def test_ok():\n    assert True\n")
    recipes = _discovered_recipes(tmp_path)

    assert "pytest" in recipes
    assert recipes["pytest"].argv == (sys.executable, "-m", "pytest", "-q", "tests")


def test_discover_mode_on_a_bare_repo_registers_nothing(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("nothing to see\n")
    from haven.evalkit.runner import _discovered_recipes

    assert _discovered_recipes(tmp_path) == {}


def test_one_crashing_case_does_not_discard_the_rest(tmp_path: Path) -> None:
    """Found live: an unwrapped SSLError in case 7 aborted a 31-case run and
    threw away the 24 that had not run yet. A live suite is expensive and not
    reproducible, so a crash must be recorded as that case failing."""
    import asyncio

    from haven.adapters.providers.scripted import ScriptedModel
    from haven.contracts.model import StreamFinished, TextDelta
    from haven.evalkit import runner as runner_module

    calls = {"n": 0}
    real_run_case = runner_module.run_case

    async def flaky_run_case(case, fixtures_dir, model_factory=None, events_path=None):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transport exploded")
        return await real_run_case(case, fixtures_dir, model_factory, events_path)

    def factory() -> ScriptedModel:
        return ScriptedModel([[TextDelta(text="done"), StreamFinished()]])

    runner_module.run_case = flaky_run_case  # type: ignore[assignment]
    try:
        report = asyncio.run(
            run_suite(
                cases_dir=CASES_DIR,
                out_dir=tmp_path,
                model_factory=factory,
                categories=("task",),
                report_name="report-live",
            )
        )
    finally:
        runner_module.run_case = real_run_case  # type: ignore[assignment]

    assert len(report.results) > 1, "the suite continued past the crash"
    crashed = report.results[0]
    assert not crashed.passed
    assert any("RuntimeError" in f for f in crashed.failures)


def test_hidden_grader_fails_a_success_that_left_the_tree_broken(tmp_path: Path) -> None:
    """A run can reach `succeeded` by answering without editing; on a bug-fix
    case that is a false pass. The hidden grader reruns the verify recipe on
    the final tree — red means the case fails no matter what the run said.
    Found live on tier 4: a real-issue case passed with zero edits."""
    import asyncio
    import json

    from haven.evalkit.runner import EvalCase, run_case

    fixture = tmp_path / "fixtures" / "broken"
    fixture.mkdir(parents=True)
    (fixture / "check.py").write_text("import sys; sys.exit(1)\n")  # forever red
    case = EvalCase.model_validate_json(
        json.dumps(
            {
                "id": "hidden-grader-catches-answer-only",
                "category": "real",
                "goal": "Fix the bug (the scripted model just answers instead)",
                "fixture": "broken",
                "hidden_check": "verify",
                "recipes": {"verify": {"argv": ["{python}", "check.py"]}},
                "turns": [
                    [
                        {"kind": "text_delta", "text": "All good, nothing to fix."},
                        {"kind": "finished", "finish_reason": "stop"},
                    ]
                ],
                "expect": {"status": "succeeded", "allowed_changed_files": []},
            }
        )
    )
    result = asyncio.run(run_case(case, tmp_path / "fixtures"))
    assert not result.passed
    assert any("hidden grader" in f for f in result.failures)


def test_per_case_event_streams_are_persisted(tmp_path: Path) -> None:
    """Failure forensics must be a read, not a re-run: each case leaves its
    event envelopes (minus transient streaming chunks) as JSONL."""
    import asyncio
    import json

    report = asyncio.run(run_suite(cases_dir=CASES_DIR, out_dir=tmp_path, categories=("task",)))
    events_dir = tmp_path / "report-events"
    written = sorted(events_dir.glob("*.jsonl"))
    assert len(written) == len(report.results), "one event stream per case"
    kinds = [json.loads(line)["event"]["kind"] for line in written[0].read_text().splitlines()]
    assert "run.created" in kinds and "run.finished" in kinds
    assert "assistant.delta" not in kinds, "streaming chunks are not forensic data"


def test_live_mode_uses_the_injected_model_and_skips_scripted_expectations(
    tmp_path: Path,
) -> None:
    """Live mode is exercised offline by injecting a fake 'real' provider, so
    the code path is covered without spending money."""
    import asyncio

    from haven.adapters.providers.scripted import ScriptedModel
    from haven.contracts.model import StreamFinished, TextDelta
    from haven.evalkit.runner import run_suite

    def factory() -> ScriptedModel:
        # answers immediately without touching any tool
        return ScriptedModel([[TextDelta(text="I did nothing."), StreamFinished()]])

    report = asyncio.run(
        run_suite(
            cases_dir=CASES_DIR,
            out_dir=tmp_path,
            model_factory=factory,
            categories=("task",),
            report_name="report-live",
        )
    )
    assert report.live is True
    assert '"mode": "live"' in report.to_json()
    assert "live eval" in report.summary_line()
    assert (tmp_path / "report-live.json").is_file()
    # recovery scenarios are excluded from live mode
    assert all(r.category == "task" for r in report.results)
    # the fake provider never edits anything, so file expectations fail —
    # and no unauthorized change is reported either
    assert report.security_violations == 0
