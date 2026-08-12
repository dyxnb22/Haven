"""Generate the measured-metrics block in README.md and PROJECT_CARD.md.

Every number in those documents that a reader could check against the repo is
generated here from the actual sources — the test collector, `coverage`, the
eval report, git, and the file tree — and written between marker comments. The
prose around the block must not repeat these numbers, so they cannot drift.

    uv run python scripts/refresh_metrics.py            # rewrite the blocks
    uv run python scripts/refresh_metrics.py --check    # fail if stale (CI)

`--check` is the guard: it recomputes and compares, exiting non-zero if a
committed block differs from freshly measured reality.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

BEGIN = "<!-- BEGIN GENERATED METRICS (scripts/refresh_metrics.py; do not edit by hand) -->"
END = "<!-- END GENERATED METRICS -->"

TARGETS = ("README.md", "docs/PROJECT_CARD.md")

#: Eval categories in the order they are reported, so the row is stable.
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


def _coverage_pct() -> int | None:
    """The documented src-only figure, via the same command the docs cite."""
    result = subprocess.run(
        [sys.executable, "-m", "coverage", "report", "--include=src/*"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    match = re.search(r"^TOTAL.*?(\d+)%\s*$", result.stdout, re.MULTILINE)
    return int(match.group(1)) if match else None


def _line_total(paths: list[Path]) -> int:
    return sum(len(p.read_text(encoding="utf-8").splitlines()) for p in paths if p.is_file())


def _src_files() -> list[Path]:
    return sorted(ROOT.glob("src/**/*.py"))


def _test_files() -> list[Path]:
    return [p for p in ROOT.glob("tests/**/*.py") if "__pycache__" not in p.parts]


def _eval_summary() -> dict[str, object]:
    report = ROOT / "eval_report" / "report.json"
    if not report.is_file():
        return {}
    return json.loads(report.read_text(encoding="utf-8"))


def _thousands(n: int) -> str:
    return f"~{n / 1000:.1f}k"


def render_block() -> str:
    tests = _count_tests()
    coverage = _coverage_pct()
    src_files = _src_files()
    src_lines = _line_total(src_files)
    test_lines = _line_total(_test_files())
    adrs = len(list((ROOT / "docs" / "adr").glob("*.md")))
    ev = _eval_summary()

    rows = [
        ("Automated tests", str(tests)),
        ("Line coverage (`src/`)", f"{coverage}%" if coverage is not None else "n/a"),
        ("Source / test size", f"{_thousands(src_lines)} / {_thousands(test_lines)} lines"),
        ("Typed modules (`mypy --strict`)", str(len(src_files))),
        ("Architecture decision records", str(adrs)),
    ]
    if ev:
        by_cat = ev.get("by_category", {})
        cats = " · ".join(
            f"{name} {by_cat[name]['total']}" for name in _CATEGORY_ORDER if name in by_cat
        )
        rows.append(
            (
                "Offline eval",
                f"{ev['passed']}/{ev['total']} passed, "
                f"{ev['security_violations']} security violations",
            )
        )
        rows.append(("Eval categories", cats))

    lines = [BEGIN, "", "| Metric | Value |", "|---|---|"]
    lines += [f"| {label} | {value} |" for label, value in rows]
    lines += ["", END]
    return "\n".join(lines)


def _replace_block(text: str, block: str) -> str:
    pattern = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END), re.DOTALL)
    if not pattern.search(text):
        raise SystemExit(
            "no GENERATED METRICS markers found; add the BEGIN/END comments where the "
            "block should live"
        )
    return pattern.sub(lambda _: block, text)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if any block is stale")
    args = parser.parse_args()

    block = render_block()
    stale: list[str] = []
    for rel in TARGETS:
        path = ROOT / rel
        current = path.read_text(encoding="utf-8")
        updated = _replace_block(current, block)
        if args.check:
            if updated != current:
                stale.append(rel)
        elif updated != current:
            path.write_text(updated, encoding="utf-8")
            print(f"updated {rel}")

    if args.check and stale:
        print("stale metrics in: " + ", ".join(stale))
        print("run: uv run python scripts/refresh_metrics.py")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
