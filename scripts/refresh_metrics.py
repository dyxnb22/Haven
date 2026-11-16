"""生成当前摘要文档中的实测指标区块。

文档中重复出现的仓库总数在这里根据真实来源生成——测试收集器、`coverage`、评估
报告、git 和文件树——并写入标记注释之间。区块周围的正文不得重复这些总数。独立
版本化的实时分析仍可以带来源地陈述自身时间点的测量结果。

    uv run python scripts/refresh_metrics.py            # 重写区块
    uv run python scripts/refresh_metrics.py --check    # 过时时失败（CI）

可用 --coverage-data 与 --offline-report 指定本次测量输入；
--preserve-historical-live 保留已有表格的历史实时成绩，不读取真实评测目录。

`--check` 是守卫：它重新计算并比较，如果已提交区块与新测量的事实不同就以非零码
退出。
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

BEGIN = "<!-- BEGIN GENERATED METRICS (scripts/refresh_metrics.py; do not edit by hand) -->"
END = "<!-- END GENERATED METRICS -->"

TARGETS = {
    "README.md": "en",
    "README.zh-CN.md": "zh",
    "docs/PROJECT_CARD.md": "en",
    "docs/DESIGN_QA.md": "en",
}

#: 评估类别的报告顺序，以保证行的顺序稳定。
_CATEGORY_ORDER = ("security", "task", "robustness", "injection", "budget", "recovery")


def _count_tests() -> int:
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    ).stdout
    match = re.search(r"(\d+) tests? collected", out)
    if match is None:
        raise SystemExit("could not parse the test count from pytest --collect-only")
    return int(match.group(1))


def _coverage_data_is_stale(data: Path, inputs: list[Path]) -> bool:
    """判断 `.coverage` 是否早于它声称测量的任意文件。

    数据文件缺失也算过时：没有可报告的内容。
    """
    if not data.is_file():
        return True
    newest = max((p.stat().st_mtime for p in inputs if p.is_file()), default=0.0)
    return data.stat().st_mtime < newest


def _coverage_pct(data_file: Path | None = None) -> int | None:
    """通过文档所引用的相同命令，获取文档中的仅 src 覆盖率数字。

    `coverage report` 读取 `.coverage` 当前已有的内容，不会重新运行套件。如果从
    过时的数据文件报告，错误数字（上次运行后新增的行会被当作未覆盖）会悄悄写入
    CI 随后强制执行的表格，因此这里会明确因过时而失败。
    """
    data = data_file.resolve() if data_file is not None else ROOT / ".coverage"
    if _coverage_data_is_stale(data, [*_src_files(), *_test_files()]):
        raise SystemExit(
            "coverage data is missing or older than the newest source/test file, so the "
            "reported figure would be wrong. Run `uv run coverage run -m pytest` first "
            "(use COVERAGE_FILE for a custom data path)."
        )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "coverage",
            "report",
            "--data-file",
            str(data),
            "--include=src/*",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise SystemExit(f"could not read coverage data: {data}")
    match = re.search(r"^TOTAL.*?(\d+)%\s*$", result.stdout, re.MULTILINE)
    return int(match.group(1)) if match else None


def _line_total(paths: list[Path]) -> int:
    return sum(len(p.read_text(encoding="utf-8").splitlines()) for p in paths if p.is_file())


def _src_files() -> list[Path]:
    return sorted(ROOT.glob("src/**/*.py"))


def _test_files() -> list[Path]:
    return [p for p in ROOT.glob("tests/**/*.py") if "__pycache__" not in p.parts]


def _eval_summary(report_path: Path | None = None) -> dict[str, object]:
    report = report_path if report_path is not None else ROOT / "eval_report" / "report.json"
    if not report.is_file() and report_path is None:
        return {}
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"could not read offline report: {report}") from exc
    if report_path is not None and (
        not isinstance(data, dict)
        or any(type(data.get(key)) is not int for key in ("passed", "total", "security_violations"))
        or not 0 <= data["passed"] <= data["total"]
        or data["security_violations"] < 0
    ):
        raise SystemExit(f"invalid offline report: {report}")
    return data if isinstance(data, dict) else {}


def _live_summary() -> dict[str, object]:
    """实时真实仓库套件（evals/real）的已提交记录。

    实时报告本身是被 git 忽略的运行构件；此文件只会由实际测量运行更新，因此渲染
    出的数字不会偏离测量结果——这里与其他每一行遵循相同的纪律。
    """
    results = ROOT / "evals" / "real" / "results.json"
    if not results.is_file():
        return {}
    data = json.loads(results.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _thousands(n: int) -> str:
    return f"~{n / 1000:.1f}k"


def render_block(
    language: str = "en",
    *,
    coverage_data: Path | None = None,
    offline_report: Path | None = None,
    historical_live_rows: tuple[str, ...] | None = None,
) -> str:
    """从测试、覆盖率、评估和文档来源生成指标 Markdown 区块。"""
    tests = _count_tests()
    coverage = _coverage_pct(coverage_data)
    src_files = _src_files()
    src_lines = _line_total(src_files)
    test_lines = _line_total(_test_files())
    adrs = len(list((ROOT / "docs" / "adr").glob("*.md")))
    ev = _eval_summary(offline_report)

    if language == "zh":
        rows = [
            ("自动化测试", str(tests)),
            ("行覆盖率（`src/`）", f"{coverage}%" if coverage is not None else "不可用"),
            (
                "源码 / 测试规模",
                f"约 {_thousands(src_lines)[1:]} / 约 {_thousands(test_lines)[1:]} 行",
            ),
            ("类型检查模块（`mypy --strict`）", str(len(src_files))),
            ("架构决策记录", str(adrs)),
        ]
    else:
        rows = [
            ("Automated tests", str(tests)),
            ("Line coverage (`src/`)", f"{coverage}%" if coverage is not None else "n/a"),
            (
                "Source / test size",
                f"{_thousands(src_lines)} / {_thousands(test_lines)} lines",
            ),
            ("Typed modules (`mypy --strict`)", str(len(src_files))),
            ("Architecture decision records", str(adrs)),
        ]
    if ev:
        by_cat_raw = ev.get("by_category")
        by_cat = by_cat_raw if isinstance(by_cat_raw, dict) else {}
        cats = " · ".join(
            f"{name} {by_cat[name]['total']}"
            for name in _CATEGORY_ORDER
            if name in by_cat and isinstance(by_cat[name], dict)
        )
        if language == "zh":
            rows.append(
                (
                    "离线评估",
                    f"{ev['passed']}/{ev['total']} 通过，{ev['security_violations']} 个安全违规",
                )
            )
            rows.append(("评估类别", cats))
        else:
            rows.append(
                (
                    "Offline eval",
                    f"{ev['passed']}/{ev['total']} passed, "
                    f"{ev['security_violations']} security violations",
                )
            )
            rows.append(("Eval categories", cats))

    live = _live_summary() if historical_live_rows is None else {}
    if live:
        tiers = live.get("tiers", [])
        assert isinstance(tiers, list)
        passed = sum(int(t["passed"]) for t in tiers)
        total = sum(int(t["total"]) for t in tiers)
        parts = " + ".join(f"{t['passed']}/{t['total']}" for t in tiers)
        if language == "zh":
            rows.append(
                (
                    f"历史实时真实仓库套件（{live['model']}）",
                    f"修复后 {passed}/{total}（{parts}）；"
                    f"{live['security_violations']} 个安全违规——原始运行和根因见 "
                    "docs/EVAL_LIVE.md",
                )
            )
        else:
            rows.append(
                (
                    f"Historical live real-repo suite ({live['model']})",
                    f"{passed}/{total} after fixes ({parts}); "
                    f"{live['security_violations']} security violations — "
                    "as-found runs and root causes in docs/EVAL_LIVE.md",
                )
            )
        rerun = live.get("single_version_rerun")
        if isinstance(rerun, dict):
            if language == "zh":
                rows.append(
                    (
                        "历史同版本完整重跑",
                        f"一次不间断运行中 {rerun['passed']}/{rerun['total']}"
                        "（失败归因见 docs/EVAL_LIVE.md）",
                    )
                )
            else:
                rows.append(
                    (
                        "Historical same-version full rerun",
                        f"{rerun['passed']}/{rerun['total']} in one uninterrupted run "
                        "(failure attribution in docs/EVAL_LIVE.md)",
                    )
                )

    header = "| 指标 | 数值 |" if language == "zh" else "| Metric | Value |"
    lines = [BEGIN, "", header, "|---|---|"]
    lines += [f"| {label} | {value} |" for label, value in rows]
    if historical_live_rows is not None:
        lines.extend(historical_live_rows)
    lines += ["", END]
    return "\n".join(lines)


def _historical_live_rows(text: str) -> tuple[str, ...]:
    """只保留已有生成区块中的实时成绩，并显式标注为历史数据。"""
    block = text.partition(BEGIN)[2].partition(END)[0]
    labels = (
        ("| Live real-repo suite ", "| Historical live real-repo suite "),
        ("| Same-version full rerun |", "| Historical same-version full rerun |"),
        ("| 实时真实仓库套件", "| 历史实时真实仓库套件"),
        ("| 同版本完整重跑 |", "| 历史同版本完整重跑 |"),
    )
    rows = []
    for line in block.splitlines():
        for original, historical in labels:
            if line.startswith(original):
                rows.append(historical + line[len(original) :])
                break
            if line.startswith(historical):
                rows.append(line)
                break
    return tuple(rows)


def _replace_block(text: str, block: str) -> str:
    pattern = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END), re.DOTALL)
    if not pattern.search(text):
        raise SystemExit(
            "no GENERATED METRICS markers found; add the BEGIN/END comments where the "
            "block should live"
        )
    return pattern.sub(lambda _: block, text)


def main() -> int:
    """生成或校验各摘要文档中的指标区块，并返回退出码。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if any block is stale")
    parser.add_argument("--coverage-data", type=Path, help="use this fresh coverage data file")
    parser.add_argument("--offline-report", type=Path, help="use this offline report JSON")
    parser.add_argument(
        "--preserve-historical-live",
        action="store_true",
        help="keep historical live rows already in each document without reading live results",
    )
    args = parser.parse_args()

    stale: list[str] = []
    for rel, language in TARGETS.items():
        path = ROOT / rel
        current = path.read_text(encoding="utf-8")
        block = render_block(
            language,
            coverage_data=args.coverage_data,
            offline_report=args.offline_report,
            historical_live_rows=_historical_live_rows(current)
            if args.preserve_historical_live
            else None,
        )
        updated = _replace_block(current, block)
        if args.check:
            if updated != current:
                stale.append(rel)
        elif updated != current:
            path.write_text(updated, encoding="utf-8")
            print(f"updated {rel}")

    if args.check and stale:
        print("stale metrics in: " + ", ".join(stale))
        print(
            "run: "
            + shlex.join(
                [
                    "uv",
                    "run",
                    "python",
                    "scripts/refresh_metrics.py",
                    *(arg for arg in sys.argv[1:] if arg != "--check"),
                ]
            )
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
