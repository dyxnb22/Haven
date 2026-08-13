"""The eval report must carry the input to its own cost calculation.

`CaseResult` records `cached_input_tokens`, and the JSON writer dropped it. A
cache hit bills at one fiftieth of a miss (ADR 0011), so a report that omits the
hit count cannot be used to check its own `cost_usd`, and the project's headline
cache-hit metric cannot be recomputed from the artifact that measured it.

Found while trying to price a live A/B run from its report and getting zero
cached tokens across 42 runs — the field was never written, not the cache
missing. Same defect family as the stale coverage figure: absent data rendering
as a plausible zero (docs/DEFENSIVE_PATTERNS.md).
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
