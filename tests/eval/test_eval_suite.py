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
