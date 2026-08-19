"""按风险对核心代码强制执行逐文件覆盖率下限。

README.md 中的主要百分比是平均值，而平均值会掩盖最差成员：一个覆盖良好的大型
模块可以补贴一个几乎没有覆盖的模块，因此新文件基本未测试时，总数仍可能保持不变。
这对拥有权限、证据、预算和边界类型的层尤其重要——这些地方的缺口正好意味着本
项目所宣称的保证存在缺口。

权限、执行、证据和恢复路径保持较高下限；核心层的其余文件只保留防崩塌下限。
这样一个简单契约或端口不会为了达到和策略引擎相同的数字而制造测试，同时新的
核心文件也不能在几乎未测试的状态下进入。平台或 UI 表面由自身套件负责。

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

#: 直接承载安全、成功判定或恢复保证的文件下限。
CORE_FLOOR = 85

#: 核心层其余文件的防崩塌下限。
BASE_FLOOR = 70

#: 至少需要防崩塌覆盖率的核心层。
GATED_PREFIXES = (
    "src/haven/domain/",
    "src/haven/application/",
    "src/haven/contracts/",
    "src/haven/ports/",
)

#: 这些文件的缺口会直接削弱权限、执行、成功判定或恢复保证。
HIGH_RISK_FILES = frozenset(
    {
        "src/haven/application/answer_resolution.py",
        "src/haven/application/approval_coordinator.py",
        "src/haven/application/compaction.py",
        "src/haven/application/recovery_service.py",
        "src/haven/application/tool_execution.py",
        "src/haven/application/tool_pipeline.py",
        "src/haven/application/tool_processes.py",
        "src/haven/contracts/checkpoint.py",
        "src/haven/contracts/tools.py",
        "src/haven/domain/approval.py",
        "src/haven/domain/budget.py",
        "src/haven/domain/evidence.py",
        "src/haven/domain/exec_policy.py",
        "src/haven/domain/policy.py",
        "src/haven/domain/review.py",
        "src/haven/domain/transitions.py",
    }
)


def floor_for(path: str) -> int | None:
    """该文件必须达到的下限；不受门禁时返回 None。"""
    normalized = path.replace("\\", "/")
    if normalized in HIGH_RISK_FILES:
        return CORE_FLOOR
    if any(normalized.startswith(prefix) for prefix in GATED_PREFIXES):
        return BASE_FLOOR
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
    """读取覆盖率数据、检查逐文件下限并返回进程退出码。"""
    failures = violations(_measured())
    if not failures:
        print(
            "coverage floor: every gated file meets its risk tier "
            f"({CORE_FLOOR}% high-risk / {BASE_FLOOR}% core)"
        )
        return 0
    print(f"coverage floor: {len(failures)} gated file(s) below their risk-tier floor")
    for path, percent, floor in failures:
        print(f"  {path}: {percent:.0f}% < {floor}%")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
