"""对决定行为的各层强制执行逐文件覆盖率下限。

README.md 中的主要百分比是平均值，而平均值会掩盖最差成员：一个覆盖良好的大型
模块可以补贴一个几乎没有覆盖的模块，因此新文件基本未测试时，总数仍可能保持不变。
这对拥有权限、证据、预算和边界类型的层尤其重要——这些地方的缺口正好意味着本
项目所宣称的保证存在缺口。

下限有意低于当前最差的受门禁文件，而不是贴着它设置：目标是捕获覆盖率崩塌，而
不是冻结当前数字，让每次重构都变成覆盖率谈判。覆盖率依赖平台或 UI 的表面
（仅 Linux 的沙箱启动器、CLI 和 TUI）不在此处设门禁；它们由总覆盖率和自身套件
负责。

用法：
    uv run coverage run -m pytest        # 生成数据
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

#: 门禁层中每个文件的最低行覆盖率。
CORE_FLOOR = 85

#: 其中每个文件都必须达到 `CORE_FLOOR` 的层。
GATED_PREFIXES = (
    "src/haven/domain/",
    "src/haven/application/",
    "src/haven/contracts/",
    "src/haven/ports/",
)


def floor_for(path: str) -> int | None:
    """该文件必须达到的下限；不受门禁时返回 None。"""
    normalized = path.replace("\\", "/")
    if any(normalized.startswith(prefix) for prefix in GATED_PREFIXES):
        return CORE_FLOOR
    return None


def violations(coverage: Mapping[str, float]) -> list[tuple[str, float, int]]:
    """返回所有低于下限的受门禁文件，格式为 `(路径，实际值，要求值)`。"""
    found = []
    for path, percent in sorted(coverage.items()):
        floor = floor_for(path)
        if floor is not None and percent < floor:
            found.append((path, percent, floor))
    return found


def _measured() -> dict[str, float]:
    """从现有 `.coverage` 数据文件读取逐文件行覆盖率。"""
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
