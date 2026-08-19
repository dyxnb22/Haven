"""Evalkit 行为测试；不重复承担整套离线用例的通过门禁。"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from haven.evalkit.runner import run_suite

REPO_ROOT = Path(__file__).resolve().parents[2]
CASES_DIR = REPO_ROOT / "evals" / "cases"


def test_category_filter_selects_a_subset(tmp_path: Path) -> None:
    subset = asyncio.run(run_suite(cases_dir=CASES_DIR, out_dir=tmp_path, categories=("security",)))
    assert subset.results
    assert {r.category for r in subset.results} == {"security"}
    assert len(subset.results) < 20


def test_unknown_category_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="matched the selection"):
        asyncio.run(run_suite(cases_dir=CASES_DIR, out_dir=tmp_path, categories=("nope",)))


def test_discover_mode_registers_what_discovery_proposes(tmp_path: Path) -> None:
    """零配置用例注册配方发现提出的测试命令。"""
    from haven.evalkit.runner import _discovered_recipes

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("def test_ok():\n    assert True\n")
    recipes = _discovered_recipes(tmp_path)

    assert "pytest" in recipes
    assert recipes["pytest"].argv == (sys.executable, "-m", "pytest", "-q", "tests")


def test_discover_mode_on_a_bare_repo_registers_nothing(tmp_path: Path) -> None:
    from haven.evalkit.runner import _discovered_recipes

    (tmp_path / "README.md").write_text("nothing to see\n")
    assert _discovered_recipes(tmp_path) == {}


def test_one_crashing_case_does_not_discard_the_rest(tmp_path: Path) -> None:
    """单个实时用例崩溃必须成为用例结果，不能丢弃余下昂贵运行。"""
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
    assert any("RuntimeError" in failure for failure in crashed.failures)


def test_hidden_grader_fails_a_success_that_left_the_tree_broken(tmp_path: Path) -> None:
    """最终目录树仍损坏时，隐藏评估器必须推翻文字上的成功。"""
    from haven.evalkit.runner import EvalCase, run_case

    fixture = tmp_path / "fixtures" / "broken"
    fixture.mkdir(parents=True)
    (fixture / "check.py").write_text("import sys; sys.exit(1)\n")
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
    assert any("hidden grader" in failure for failure in result.failures)


def test_per_case_event_streams_are_persisted(tmp_path: Path) -> None:
    """每个用例留下可读取的取证流，但不持久化瞬态流式块。"""
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
    """注入假的实时提供商，离线覆盖 live 分支而不产生费用。"""
    from haven.adapters.providers.scripted import ScriptedModel
    from haven.contracts.model import StreamFinished, TextDelta

    def factory() -> ScriptedModel:
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
    assert all(result.category == "task" for result in report.results)
    assert report.security_violations == 0
