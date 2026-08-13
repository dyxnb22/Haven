"""Enforce a per-file coverage floor on the layers that decide behavior.

The headline percentage in README.md is an average, and an average hides its
worst member: a large well-covered module subsidises a bare one, so the number
can hold steady while a new file lands essentially untested. That matters most
in the layers that own permission, evidence, budgets, and the boundary types —
a gap there is a gap in exactly the guarantees this project claims.

The floor is deliberately below today's worst gated file rather than at it: the
job is to catch a collapse, not to freeze the current numbers and turn every
refactor into a coverage negotiation. Surfaces whose coverage is platform- or
UI-dependent (the Linux-only sandbox launcher, the CLI and TUI) are not gated
here; they answer to the overall figure and to their own suites.

Usage:
    uv run coverage run -m pytest        # produce the data
    uv run python scripts/check_coverage_floor.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Minimum line coverage for every file in the gated layers.
CORE_FLOOR = 85

#: The layers whose files must each clear `CORE_FLOOR`.
GATED_PREFIXES = (
    "src/haven/domain/",
    "src/haven/application/",
    "src/haven/contracts/",
    "src/haven/ports/",
)


def floor_for(path: str) -> int | None:
    """The floor this file must clear, or None when it is not gated."""
    normalized = path.replace("\\", "/")
    if any(normalized.startswith(prefix) for prefix in GATED_PREFIXES):
        return CORE_FLOOR
    return None


def violations(coverage: Mapping[str, float]) -> list[tuple[str, float, int]]:
    """Every gated file below its floor, as (path, actual, required)."""
    found = []
    for path, percent in sorted(coverage.items()):
        floor = floor_for(path)
        if floor is not None and percent < floor:
            found.append((path, percent, floor))
    return found


def _measured() -> dict[str, float]:
    """Per-file line coverage from the existing `.coverage` data file."""
    if not (ROOT / ".coverage").is_file():
        raise SystemExit("no .coverage data; run `uv run coverage run -m pytest` first")
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "coverage.json"
        subprocess.run(
            [sys.executable, "-m", "coverage", "json", "-o", str(report), "--include=src/*"],
            cwd=ROOT,
            check=True,
            capture_output=True,
        )
        data = json.loads(report.read_text(encoding="utf-8"))
    return {
        path.replace("\\", "/"): float(entry["summary"]["percent_covered"])
        for path, entry in data["files"].items()
    }


def main() -> int:
    failures = violations(_measured())
    if not failures:
        print(f"coverage floor: every gated file is at or above {CORE_FLOOR}%")
        return 0
    print(f"coverage floor: {len(failures)} gated file(s) below {CORE_FLOOR}%")
    for path, percent, floor in failures:
        print(f"  {path}: {percent:.0f}% < {floor}%")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
