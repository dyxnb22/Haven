"""Generate the tool/policy table in ARCHITECTURE.md from the policy itself.

The table answers the question the whole project exists to answer — "why was
this exact side effect allowed?" — so a transcribed copy that drifts from
`evaluate_policy` is worse than no table. Every decision below is produced by
calling the real policy function; only the constraints column is prose, and it
lives here beside the generator with a completeness guard, so a tool cannot be
added to the code and left undocumented.

    uv run python scripts/gen_tool_table.py            # rewrite the block
    uv run python scripts/gen_tool_table.py --check    # fail if it drifted
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from haven.domain.enums import PermissionMode  # noqa: E402
from haven.domain.exec_policy import ExecClass  # noqa: E402
from haven.domain.policy import (  # noqa: E402
    EFFECT_TOOLS,
    EXEC_TOOLS,
    KNOWN_TOOLS,
    READ_ONLY_TOOLS,
    STATE_TOOLS,
    ToolFacts,
    evaluate_policy,
)

TARGET = ROOT / "docs" / "ARCHITECTURE.md"
BEGIN = "<!-- BEGIN GENERATED TOOL TABLE (scripts/gen_tool_table.py; do not edit by hand) -->"
END = "<!-- END GENERATED TOOL TABLE -->"

#: The one column no code can derive: why each tool is safe to offer at all.
#: Keep each entry to the constraints a reviewer needs, not a description.
CONSTRAINTS: dict[str, str] = {
    "repo.list": "workspace-confined, entry cap",
    "repo.search": (
        "ripgrep when available (honours `.gitignore`), Python fallback; "
        "result, line, and byte caps"
    ),
    "repo.read": (
        "regular UTF-8 files, line and byte caps; records the digest that later binds an edit"
    ),
    "repo.diff": "shows only what *this run* changed, including created files",
    "repo.edit": (
        "existing files only, preimage-bound, unique match unless `occurrence` or "
        "`replace_all` is set"
    ),
    "repo.create": "new paths only — fails on anything that exists, so it can never blank a file",
    "repo.delete": "existing files only, content pinned at approval so a concurrent change fails",
    "repo.move": "rename/move; fails if the destination exists, so it never silently overwrites",
    "repo.apply_patch": (
        "multi-file transaction: simulated first, one approval binds every file's preimage, "
        "applied atomically with journaled rollback (ADR 0019)"
    ),
    "repo.check": "registered recipe ids only, fixed argv, scrubbed env, timeout, bounded output",
    "repo.exec": (
        "argv array only (no shell string), OS sandbox with the workspace read-only, no network, "
        "`$HOME` unreadable; output is never evidence"
    ),
    "task.plan": "touches only run state; no path, no external effect",
}

#: Which set a tool belongs to, for the class column.
_CLASSES = (
    ("read-only", READ_ONLY_TOOLS),
    ("effect", EFFECT_TOOLS),
    ("exec", EXEC_TOOLS),
    ("state", STATE_TOOLS),
)


@dataclass(frozen=True, slots=True)
class Row:
    tool: str
    tool_class: str
    interactive: str
    read_only: str
    constraints: str


def _class_of(tool: str) -> str:
    for name, members in _CLASSES:
        if tool in members:
            return name
    return "unclassified"


def _decide(tool: str, mode: PermissionMode) -> str:
    """The real policy's decision for a representative, well-formed proposal.

    The facts are the benign case (inside the workspace, no protected path, a
    registered recipe, a sandbox present) so the table reports the *baseline*
    friction of each tool rather than a hard deny that any tool would earn.
    """
    facts = ToolFacts(
        tool_name=tool,
        within_workspace=True,
        touches_protected_path=False,
        recipe_registered=True,
        sandbox_available=True,
        exec_class=ExecClass.OTHER.value,
    )
    return str(evaluate_policy(mode, facts).decision.value)


def rows_for_tools(tools: set[str] | None = None) -> list[Row]:
    """One row per tool, decisions computed by the real policy."""
    selected = sorted(KNOWN_TOOLS if tools is None else tools)
    missing = [tool for tool in selected if tool not in CONSTRAINTS]
    if missing:
        raise SystemExit(
            "every tool needs a constraints entry in scripts/gen_tool_table.py; missing: "
            + ", ".join(missing)
        )
    return [
        Row(
            tool=tool,
            tool_class=_class_of(tool),
            interactive=_decide(tool, PermissionMode.INTERACTIVE),
            read_only=_decide(tool, PermissionMode.READ_ONLY),
            constraints=CONSTRAINTS[tool],
        )
        for tool in selected
    ]


def render_table() -> str:
    header = "| Tool | Class | Interactive | Read-only | Key constraints |\n|---|---|---|---|---|\n"
    body = "".join(
        f"| `{row.tool}` | {row.tool_class} | {row.interactive} | "
        f"{row.read_only} | {row.constraints} |\n"
        for row in rows_for_tools()
    )
    return header + body


def _block() -> str:
    note = (
        "\n*Decisions are computed by calling `evaluate_policy` itself, for a benign, "
        "well-formed proposal; a hard deny (outside the workspace, a protected path, no "
        "sandbox) overrides them in every mode.*\n"
    )
    return f"{BEGIN}\n\n{render_table()}{note}\n{END}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the block is stale")
    args = parser.parse_args(argv)

    text = TARGET.read_text(encoding="utf-8")
    pattern = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END), re.DOTALL)
    if not pattern.search(text):
        raise SystemExit(f"{TARGET} has no generated tool-table block; add the marker comments")
    updated = pattern.sub(lambda _: _block(), text)

    if args.check:
        if updated != text:
            print(f"{TARGET} tool table is stale; run `uv run python {Path(__file__).name}`")
            return 1
        print("tool table matches the policy code")
        return 0
    if updated != text:
        TARGET.write_text(updated, encoding="utf-8")
        print(f"updated {TARGET.relative_to(ROOT)}")
    else:
        print("tool table already current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
