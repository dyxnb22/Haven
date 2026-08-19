"""质量门禁只声明一次，并在本地和 CI 中以相同方式运行。

CI 步骤和 README 的开发命令列表原本是同一序列的两份手工维护副本。副本会漂移：
加入 CI 的门禁在本地可能直到有人注意才出现，而只在本地运行的检查永远保护不了
`main`。现在门禁是一份带显式依赖的列表；CI 选择模式，开发者选择模式，二者都
遍历同一张图。

    uv run python scripts/gates.py            # 全部（默认：full）
    uv run python scripts/gates.py --mode fast    # 只做静态检查，不测试
    uv run python scripts/gates.py --list         # 显示图
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True, slots=True)
class Gate:
    """一项质量检查：包含 id、命令、运行时机和依赖。"""

    #: 门禁在图中的稳定标识。
    id: str
    #: 要执行的 shell 风格命令文本。
    command: str
    #: 此门禁所属的模式。只作为依赖被触达的门禁不需要单独的模式。
    modes: tuple[str, ...] = ()
    #: 必须先成功的门禁。即使依赖自身的模式不包含当前模式，也会将其拉入本次
    #: 运行，因此门禁不会在缺少前置条件的情况下执行（覆盖率报告需要测试套件
    #: 先产出数据）。
    needs: tuple[str, ...] = ()
    #: 面向人类的门禁用途说明。
    description: str = ""


#: 发布的门禁图。`--mode fast` 是提交前检查（不运行测试）；`full` 是 CI
#: 强制执行的模式。
GATES: list[Gate] = [
    Gate(
        "format",
        "uv run ruff format --check .",
        modes=("fast", "full"),
        description="formatting is canonical",
    ),
    Gate(
        "lint",
        "uv run ruff check .",
        modes=("fast", "full"),
        description="lint rules",
    ),
    Gate(
        "types",
        "uv run mypy src",
        modes=("fast", "full"),
        description="mypy --strict over src",
    ),
    Gate(
        "imports",
        "uv run lint-imports",
        modes=("fast", "full"),
        description="layering contracts (domain/application/interfaces)",
    ),
    Gate(
        "tool-table",
        "uv run python scripts/gen_tool_table.py --check",
        modes=("fast", "full"),
        description="the generated tool/policy table matches the code",
    ),
    Gate(
        "notes",
        "uv run python scripts/check_notes.py",
        modes=("fast", "full"),
        description="decision notes carry the required sections",
    ),
    Gate(
        "adr-links",
        "uv run python scripts/check_adr_links.py",
        modes=("fast", "full"),
        description="an overturned ADR points at the ADR that overturned it",
    ),
    Gate(
        "docs",
        "uv run python scripts/check_docs.py",
        modes=("fast", "full"),
        description="local links, commands, version, and historical labels agree",
    ),
    Gate(
        "tests",
        "uv run coverage run -m pytest --ignore=tests/eval/test_eval_suite.py",
        modes=("full",),
        description="tests under coverage; the aggregate offline eval runs in its own gate",
    ),
    Gate(
        "coverage-floor",
        "uv run python scripts/check_coverage_floor.py",
        modes=("full",),
        needs=("eval",),
        description="no gated file collapsed below its per-file floor",
    ),
    Gate(
        "eval",
        "uv run coverage run --append -m haven.interfaces.cli eval --offline",
        modes=("full",),
        needs=("tests",),
        description="offline eval once, appending its paths to coverage and writing the report",
    ),
    Gate(
        "metrics",
        "uv run python scripts/refresh_metrics.py --check",
        modes=("full",),
        needs=("coverage-floor",),
        description="published numbers still match reality",
    ),
]


def validate(gates: list[Gate]) -> None:
    """在任何内容运行前明确拒绝格式错误的图。"""
    seen: set[str] = set()
    for gate in gates:
        if gate.id in seen:
            raise ValueError(f"duplicate gate id: {gate.id}")
        seen.add(gate.id)
    for gate in gates:
        for need in gate.needs:
            if need not in seen:
                raise ValueError(f"gate {gate.id} needs unknown gate: {need}")
    if cycle_in(gates):
        raise ValueError("gate dependency cycle")


def cycle_in(gates: list[Gate]) -> bool:
    """判断 `needs` 边中是否存在环。"""
    by_id = {gate.id: gate for gate in gates}
    state: dict[str, int] = {}  # 0 = 正在访问，1 = 已完成

    def visit(node: str) -> bool:
        """深度优先访问节点，并报告当前路径是否形成环。"""
        if state.get(node) == 1:
            return False
        if node in state:
            return True
        state[node] = 0
        for need in by_id[node].needs if node in by_id else ():
            if visit(need):
                return True
        state[node] = 1
        return False

    return any(visit(gate.id) for gate in gates)


def order_for(gates: list[Gate], ids: list[str]) -> list[str]:
    """按依赖顺序排列 `ids`：门禁所需的一切都排在该门禁之前。"""
    by_id = {gate.id: gate for gate in gates}
    wanted = set(ids)
    ordered: list[str] = []
    placed: set[str] = set()

    def place(node: str) -> None:
        """递归放置依赖，并保证每个门禁只加入一次。"""
        if node in placed or node not in by_id:
            return
        placed.add(node)
        for need in by_id[node].needs:
            place(need)
        if node in wanted:
            ordered.append(node)

    for node in ids:
        place(node)
    return ordered


def select(gates: list[Gate], mode: str) -> list[Gate]:
    """返回 `mode` 要运行的门禁，包含依赖，并按运行顺序排列。"""
    by_id = {gate.id: gate for gate in gates}
    chosen: set[str] = {gate.id for gate in gates if mode in gate.modes}
    # 传递性地纳入依赖：门禁绝不能在缺少依赖的情况下运行。
    frontier = list(chosen)
    while frontier:
        for need in by_id[frontier.pop()].needs:
            if need not in chosen:
                chosen.add(need)
                frontier.append(need)
    return [by_id[gate_id] for gate_id in order_for(gates, sorted(chosen))]


def _run(gate: Gate) -> tuple[bool, float]:
    started = time.monotonic()
    result = subprocess.run(shlex.split(gate.command), cwd=ROOT)
    return result.returncode == 0, time.monotonic() - started


def main(argv: list[str] | None = None) -> int:
    """解析门禁模式、按依赖顺序执行检查并返回汇总退出码。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", default="full", help="which gate set to run (fast|full)")
    parser.add_argument("--list", action="store_true", help="print the graph and exit")
    args = parser.parse_args(argv)

    validate(GATES)
    planned = select(GATES, args.mode)

    if args.list:
        for gate in GATES:
            needs = f" (needs {', '.join(gate.needs)})" if gate.needs else ""
            print(f"{gate.id:16} [{'|'.join(gate.modes) or '-':10}] {gate.description}{needs}")
        return 0

    if not planned:
        raise SystemExit(f"no gates for mode {args.mode!r}")

    failed: list[str] = []
    for gate in planned:
        print(f"\n=== {gate.id}: {gate.command}", flush=True)
        ok, seconds = _run(gate)
        print(f"--- {gate.id}: {'ok' if ok else 'FAILED'} in {seconds:.1f}s", flush=True)
        if not ok:
            failed.append(gate.id)

    print(f"\n{len(planned) - len(failed)}/{len(planned)} gates passed ({args.mode})")
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
