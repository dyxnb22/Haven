"""评估套件聚合指标及 JSON/Markdown 渲染。"""

from __future__ import annotations

import json
from dataclasses import dataclass

from haven.evalkit.models import CaseResult


@dataclass(slots=True)
class SuiteReport:
    #: 每个案例的执行结果和断言结果。
    results: list[CaseResult]
    #: 套件开始时的 ISO-8601 时间戳。
    started_at: str
    #: 套件总耗时，单位为毫秒。
    duration_ms: int
    #: 报告是否来自在线/实时评估。
    live: bool = False

    QUALITY_CATEGORIES = frozenset({"task", "robustness", "budget", "real"})
    SAFETY_CATEGORIES = frozenset({"security", "injection", "recovery"})

    @property
    def all_passed(self) -> bool:
        """是否所有案例均通过。"""
        return all(result.passed for result in self.results)

    @property
    def security_violations(self) -> int:
        """累计受保护路径越界次数。"""
        return sum(result.unauthorized_changes for result in self.results)

    @property
    def quality_total(self) -> int:
        """质量类案例总数。"""
        return sum(1 for result in self.results if result.category in self.QUALITY_CATEGORIES)

    @property
    def quality_passed(self) -> int:
        """通过的质量类案例数。"""
        return sum(
            1
            for result in self.results
            if result.category in self.QUALITY_CATEGORIES and result.passed
        )

    @property
    def quality_pass_rate(self) -> float:
        """质量类案例通过率；没有质量案例时为 0。"""
        return self.quality_passed / self.quality_total if self.quality_total else 0.0

    @property
    def out_of_scope_changes(self) -> int:
        """累计超出案例允许范围的文件变更数。"""
        return sum(result.out_of_scope_changes for result in self.results)

    @property
    def total_cost_usd(self) -> float:
        """返回所有案例累计费用，保留六位小数。"""
        return round(sum(result.cost_usd for result in self.results), 6)

    @property
    def total_input_tokens(self) -> int:
        """返回所有案例的输入 token 总数。"""
        return sum(result.input_tokens for result in self.results)

    @property
    def total_cached_input_tokens(self) -> int:
        """返回所有案例中命中缓存的输入 token 总数。"""
        return sum(result.cached_input_tokens for result in self.results)

    @property
    def cache_hit_rate(self) -> float:
        """返回套件级缓存命中率；没有输入 token 时为 0。"""
        return (
            self.total_cached_input_tokens / self.total_input_tokens
            if self.total_input_tokens
            else 0.0
        )

    def summary_line(self) -> str:
        """生成适合控制台单行展示的套件摘要。"""
        passed = sum(1 for result in self.results if result.passed)
        mode = "live eval" if self.live else "eval"
        line = (
            f"{mode}: {passed}/{len(self.results)} cases passed "
            f"(quality {self.quality_passed}/{self.quality_total} task-shaped), "
            f"security violations: {self.security_violations}, "
            f"out-of-scope changes: {self.out_of_scope_changes}"
        )
        if self.live:
            line += (
                f", est. cost ${self.total_cost_usd:.4f}"
                f", cache hit {self.cache_hit_rate:.0%} "
                f"({self.total_cached_input_tokens}/{self.total_input_tokens})"
            )
        return line

    def to_json(self) -> str:
        """将聚合指标和逐案例结果序列化为稳定、可读的 JSON。"""
        by_category: dict[str, dict[str, int]] = {}
        for result in self.results:
            bucket = by_category.setdefault(result.category, {"total": 0, "passed": 0})
            bucket["total"] += 1
            bucket["passed"] += int(result.passed)
        return json.dumps(
            {
                "mode": "live" if self.live else "offline",
                "started_at": self.started_at,
                "duration_ms": self.duration_ms,
                "total": len(self.results),
                "passed": sum(1 for result in self.results if result.passed),
                "security_violations": self.security_violations,
                "out_of_scope_changes": self.out_of_scope_changes,
                "total_input_tokens": self.total_input_tokens,
                "total_cached_input_tokens": self.total_cached_input_tokens,
                "cache_hit_rate": round(self.cache_hit_rate, 4),
                "by_category": by_category,
                "cases": [
                    {
                        "id": result.case_id,
                        "category": result.category,
                        "passed": result.passed,
                        "failures": result.failures,
                        "status": result.status,
                        "stop_reason": result.stop_reason,
                        "steps": result.steps,
                        "tool_calls": result.tool_calls,
                        "input_tokens": result.input_tokens,
                        "output_tokens": result.output_tokens,
                        "cached_input_tokens": result.cached_input_tokens,
                        "cost_usd": result.cost_usd,
                        "duration_ms": result.duration_ms,
                        "unauthorized_changes": result.unauthorized_changes,
                        "out_of_scope_changes": result.out_of_scope_changes,
                    }
                    for result in self.results
                ],
            },
            indent=2,
            ensure_ascii=False,
        )

    def to_markdown(self) -> str:
        """渲染包含摘要、案例表和失败详情的 Markdown 报告。"""
        title = "live" if self.live else "offline"
        lines = [
            f"# Haven {title} eval report",
            "",
            f"- started: {self.started_at}",
            f"- duration: {self.duration_ms}ms",
            f"- result: **{self.summary_line()}**",
        ]
        if self.live:
            lines += [
                "",
                "> Live run against a real provider: numbers are **not** reproducible "
                "and cost real money. Only the outcome and the security invariants are "
                "asserted; trajectory expectations are scripted-only and were skipped.",
            ]
        lines += [
            "",
            "| case | category | passed | status | stop reason | steps | tools | tokens | ms |",
            "|---|---|---|---|---|---:|---:|---:|---:|",
        ]
        for result in self.results:
            mark = "yes" if result.passed else "**NO**"
            lines.append(
                f"| {result.case_id} | {result.category} | {mark} | {result.status} | "
                f"{result.stop_reason} | {result.steps} | {result.tool_calls} | "
                f"{result.input_tokens}/{result.output_tokens} | {result.duration_ms} |"
            )
        failures = [result for result in self.results if not result.passed]
        if failures:
            lines += ["", "## Failures", ""]
            for result in failures:
                lines.append(f"- **{result.case_id}**: " + "; ".join(result.failures))
        return "\n".join(lines) + "\n"
