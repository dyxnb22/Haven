"""评估报告必须携带计算自身成本所需的输入。

`CaseResult` 记录 `cached_input_tokens`，但 JSON 写入器曾丢弃它。缓存命中的计费是
未命中的五十分之一（ADR 0011），因此缺少命中数量的报告无法校验自身的 `cost_usd`，
项目的主要缓存命中指标也无法从测量它的构件中重新计算。

尝试根据报告计算实时 A/B 运行成本时发现，42 次运行的缓存 token 都是 0——不是没有
命中缓存，而是字段从未写入。它与过时覆盖率数字属于同一类缺陷：缺失数据被渲染成
看似合理的零（`docs/DEFENSIVE_PATTERNS.md`）。
"""

import json

from haven.evalkit.runner import CaseResult, SuiteReport


def _report() -> SuiteReport:
    return SuiteReport(
        results=[
            CaseResult(
                case_id="c1",
                category="real",
                passed=True,
                input_tokens=1000,
                output_tokens=100,
                cached_input_tokens=800,
                cost_usd=0.001,
            )
        ],
        started_at="2026-08-14T00:00:00+00:00",
        duration_ms=1,
        live=True,
    )


def test_the_json_report_records_cached_input_tokens() -> None:
    case = json.loads(_report().to_json())["cases"][0]
    assert case["cached_input_tokens"] == 800


def test_the_report_still_records_the_totals_it_always_did() -> None:
    case = json.loads(_report().to_json())["cases"][0]
    assert case["input_tokens"] == 1000
    assert case["output_tokens"] == 100
    assert case["cost_usd"] == 0.001
