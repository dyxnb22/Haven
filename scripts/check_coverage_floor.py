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
import os
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


def gated_paths_on_disk() -> set[str]:
    """磁盘上所有受门禁约束的源文件。

    门禁原先只看覆盖率报告里出现了哪些文件，而报告只包含**被执行过**的模块：一个
    新增的、还没有任何代码路径 import 的源文件根本不在报告里，于是 `floor_for` 永远不会
    被咨询，门禁打印"每个被把关的文件都达标"并退出 0——恰好是本模块 docstring 承诺要
    阻止的那件事（"新的核心文件也不能在几乎未测试的状态下进入"）。已实测：放一个没人
    import 的 `src/haven/domain/_probe.py` 进去，报告里 152 个文件不含它，门禁全绿。
    """
    # 必须相对 ROOT：`GATED_PREFIXES` 用的是 `src/haven/...` 这种形式，而 glob 出来的是
    # 绝对路径。用绝对路径去比，每一个文件都会落空，这个集合会恒为空。
    return {
        path.relative_to(ROOT).as_posix()
        for path in sorted(ROOT.glob("src/**/*.py"))
        if floor_for(path.relative_to(ROOT).as_posix()) is not None
    }


def unreported(coverage: Mapping[str, float]) -> list[str]:
    """磁盘上受门禁、却没能出现在覆盖率报告里的文件。"""
    return sorted(gated_paths_on_disk() - {p.replace("\\", "/") for p in coverage})


def _coverage_data_file() -> Path:
    """本次测量的覆盖率数据文件：默认 `.coverage`，或 `COVERAGE_FILE` 指定的路径。

    `coverage run` 本身就用 `COVERAGE_FILE` 决定数据落点，而 `refresh_metrics.py`
    的提示也正是这样建议的。若这里不读同一个变量，照提示把数据写到自定义路径后，
    本门禁会报一句莫名其妙的 "no .coverage data"——提示变成无法执行的空话。
    """
    env_path = os.environ.get("COVERAGE_FILE")
    return Path(env_path) if env_path else ROOT / ".coverage"


def _measured() -> dict[str, float]:
    """从覆盖率数据文件读取逐文件行覆盖率。"""
    data = _coverage_data_file().resolve()
    if not data.is_file():
        raise SystemExit(
            f"no coverage data at {data}; run `uv run coverage run -m pytest` first "
            "(use COVERAGE_FILE for a custom data path)."
        )
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "coverage.json"
        subprocess.run(
            [
                sys.executable,
                "-m",
                "coverage",
                "json",
                "--data-file",
                str(data),
                "-o",
                str(report),
                "--include=src/*",
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
        )
        payload = json.loads(report.read_text(encoding="utf-8"))
    return {
        path.replace("\\", "/"): float(entry["summary"]["percent_covered"])
        for path, entry in payload["files"].items()
    }


def main() -> int:
    """读取覆盖率数据、检查逐文件下限并返回进程退出码。"""
    measured = _measured()
    missing = unreported(measured)
    if missing:
        # 缺席不等于达标：一个从没被执行过的模块是 0% 覆盖，不是"没问题"。
        print(f"coverage floor: {len(missing)} gated file(s) never executed (0% covered)")
        for path in missing:
            print(f"  {path}: not in the coverage report at all")
        return 1
    failures = violations(measured)
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
