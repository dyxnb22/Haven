"""评估套件编排器；案例执行、夹具和报告位于相邻模块。"""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from haven.evalkit.case_runner import run_case as run_case
from haven.evalkit.fixtures import discovered_recipes
from haven.evalkit.models import (
    CaseResult,
    EvalCase,
    ExpectSpec,
    ModelFactory,
    RecipeDef,
)
from haven.evalkit.report import SuiteReport

__all__ = [
    "CaseResult",
    "EvalCase",
    "ExpectSpec",
    "RecipeDef",
    "SuiteReport",
    "_discovered_recipes",
    "load_cases",
    "run_case",
    "run_suite",
]

_discovered_recipes = discovered_recipes


def load_cases(cases_dir: Path) -> list[EvalCase]:
    """读取目录下按文件名排序的 JSON 案例；目录为空时抛出错误。"""
    case_files = sorted(cases_dir.glob("*.json"))
    if not case_files:
        raise FileNotFoundError(f"no eval cases found in {cases_dir}")
    return [EvalCase.model_validate_json(path.read_text(encoding="utf-8")) for path in case_files]


async def run_suite(
    cases_dir: Path,
    out_dir: Path,
    *,
    model_factory: ModelFactory | None = None,
    categories: tuple[str, ...] = (),
    report_name: str = "report",
) -> SuiteReport:
    """运行选择的案例，并持续写入进度、事件和最终报告。"""
    started_at = datetime.now(UTC).isoformat()
    started = time.monotonic()
    fixtures_dir = cases_dir.parent / "fixtures"

    cases = load_cases(cases_dir)
    if categories:
        cases = [case for case in cases if case.category in categories]
    if model_factory is not None:
        cases = [case for case in cases if not case.scenario]
    if not cases:
        raise FileNotFoundError(f"no eval cases in {cases_dir} matched the selection")

    out_dir.mkdir(parents=True, exist_ok=True)
    progress_path = out_dir / f"{report_name}-progress.jsonl"
    progress_path.write_text("", encoding="utf-8")
    events_dir = out_dir / f"{report_name}-events"
    events_dir.mkdir(parents=True, exist_ok=True)

    results: list[CaseResult] = []
    for index, case in enumerate(cases, start=1):
        try:
            result = await run_case(
                case,
                fixtures_dir,
                model_factory,
                events_path=events_dir / f"{case.id}.jsonl",
            )
        except Exception as exc:  # noqa: BLE001 — 单个案例失败不能中止整个套件
            result = CaseResult(
                case_id=case.id,
                category=case.category,
                passed=False,
                failures=[f"case raised {type(exc).__name__}: {exc}"],
            )
        results.append(result)
        with progress_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(result), separators=(",", ":")) + "\n")
        print(
            f"[{index}/{len(cases)}] {case.id}: "
            f"{'PASS' if result.passed else 'FAIL'} "
            f"({result.duration_ms} ms, {result.steps} steps, ${result.cost_usd:.4f})",
            flush=True,
        )

    report = SuiteReport(
        results=results,
        started_at=started_at,
        duration_ms=int((time.monotonic() - started) * 1000),
        live=model_factory is not None,
    )
    (out_dir / f"{report_name}.json").write_text(report.to_json(), encoding="utf-8")
    (out_dir / f"{report_name}.md").write_text(report.to_markdown(), encoding="utf-8")
    return report
