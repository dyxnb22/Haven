"""整套离线评估的聚合契约。

完整门禁通过 `haven eval --offline` 执行同一套用例、生成正式报告并追加覆盖率，
因此 pytest 阶段排除此文件，避免重复运行；直接执行 `pytest` 时仍会收集这些断言。
"""

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
    """任务成功和安全保证分别作为主要指标报告，绝不平均为一个数字。"""
    assert report.quality_total > 0
    assert report.quality_passed == report.quality_total  # 离线脚本全部通过
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
