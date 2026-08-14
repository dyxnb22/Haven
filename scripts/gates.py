"""The quality gates, declared once, run the same way locally and in CI.

CI steps and the README's development command list were two hand-maintained
copies of one sequence. Copies drift: a gate added to CI is absent locally until
someone notices, and a local-only check never protects `main`. Here the gates
are one list with explicit dependencies; CI picks a mode, a developer picks a
mode, and both traverse the same graph.

    uv run python scripts/gates.py            # everything (default: full)
    uv run python scripts/gates.py --mode fast    # static checks only, no tests
    uv run python scripts/gates.py --list         # show the graph
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
    """One quality check: an id, the command, when it runs, what it needs."""

    id: str
    command: str
    #: Modes this gate belongs to. A gate reachable only as a dependency needs
    #: no mode of its own.
    modes: tuple[str, ...] = ()
    #: Gates that must succeed first. A dependency is pulled into the run even
    #: when its own modes exclude it, so a gate never executes without what it
    #: assumes (coverage reporting needs the suite to have produced data).
    needs: tuple[str, ...] = ()
    description: str = ""


#: The shipped graph. `--mode fast` is the pre-commit sweep (no test run);
#: `full` is what CI enforces.
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
        "uv run coverage run -m pytest",
        modes=("full",),
        description="the suite, under coverage so the gates below have data",
    ),
    Gate(
        "coverage-floor",
        "uv run python scripts/check_coverage_floor.py",
        modes=("full",),
        needs=("tests",),
        description="no gated file collapsed below its per-file floor",
    ),
    Gate(
        "eval",
        "uv run haven eval --offline",
        modes=("full",),
        description="offline eval suite and its security gate",
    ),
    Gate(
        "metrics",
        "uv run python scripts/refresh_metrics.py --check",
        modes=("full",),
        needs=("tests", "eval"),
        description="published numbers still match reality",
    ),
]


def validate(gates: list[Gate]) -> None:
    """Reject a malformed graph loudly, before anything runs."""
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
    """Whether the `needs` edges contain a cycle."""
    by_id = {gate.id: gate for gate in gates}
    state: dict[str, int] = {}  # 0 = visiting, 1 = done

    def visit(node: str) -> bool:
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
    """`ids` in dependency order: everything a gate needs precedes it."""
    by_id = {gate.id: gate for gate in gates}
    wanted = set(ids)
    ordered: list[str] = []
    placed: set[str] = set()

    def place(node: str) -> None:
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
    """The gates to run for `mode`, dependencies included, in run order."""
    by_id = {gate.id: gate for gate in gates}
    chosen: set[str] = {gate.id for gate in gates if mode in gate.modes}
    # Pull in dependencies transitively: a gate must never run without them.
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
